"""Keeps the signals current in the background.

Insider filings: SEC's live feed of Form 4 filings is read every
``insider_poll_minutes``. On the first start, and after the app was off, the daily
indexes of the trading days missed are read too - only the filings about listed
companies. Each filing is read once, a few at a time within SEC's request limit.
Every stock with recent insider buying gets its insiders' filings of the past year
read as well (and again weekly), so whether they rarely buy is known; until then
it isn't claimed.

News: every ``news_poll_minutes`` the watched stocks - the hot list, the stocks
held, and those with unusual insider buying - get their recent IBKR headlines,
their 8-K filings from SEC and, with a key, Finnhub's stories. FinBERT scores new
headlines when it is installed.

After each pass the signal book is rebuilt and ``signals.updated`` published.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from ..core.enums import Side
from ..core.eventbus import BUS
from ..data.sec_edgar import sec_ticker
from ..data.sec_http import SEC, SecHttp
from ..util import clock
from .book import SignalBook, nudge
from .calendar import EarningsCalendarFile, next_report, sessions_until
from .edgar import (LATEST_URL, SUBMISSIONS_URL, TICKERS_URL, FilingRef, company_filings, company_tickers,
                    daily_index, daily_index_url, latest_filings)
from .finnhub import FinnhubNews
from .form4 import InsiderTrade, parse_form4
from .insiders import InsiderSettings, filing_delays, insider_signals
from .news import NewsItem, eight_k_news, ibkr_headline, is_material
from .sentiment import HeadlineSentiment, symbol_sentiment
from .store import SignalStore

log = logging.getLogger(__name__)

FEED_PAGE = 100                  # entries per page of the live feed (about 50 filings)
MAX_FEED_PAGES = 10
MAX_HISTORY_FILINGS = 40         # of one company's past Form 4 filings, newest first
HISTORY_PER_PASS = 25            # companies whose past filings are read in one pass
HISTORY_REFRESH_DAYS = 7
MAX_WATCHED = 40                 # stocks whose news is followed
READERS = 6                      # filings downloaded at once; the shared pacer keeps SEC under 10 a second
MOVER_HEADLINES = 50             # IBKR headlines per stock for a session's movers
PAGE_DAYS = 30                   # the insider filings and headline counts the Signals page shows
PAGE_HEADLINES = 80
CALENDAR_DAYS_BACK = 7           # earlier reports re-read, so their actual numbers are filled in


def ibkr_symbol(ticker: str) -> str:
    """Filers write class shares as BRK.B or BRK-B; IBKR writes BRK B."""
    return ticker.strip().upper().replace(".", " ").replace("-", " ")


class SignalService:
    def __init__(self, cfg: Any, store: SignalStore, book: SignalBook, state_path: Path, *,
                 watched: Callable[[], Iterable[str]], news_source: Callable[[], Any],
                 con_ids: Callable[[Sequence[str]], Dict[str, int]],
                 finnhub_key: Callable[[], str] = lambda: os.environ.get("FINNHUB_API_KEY", ""),
                 sec: SecHttp = SEC, sentiment: Optional[HeadlineSentiment] = None,
                 today: Callable[[], dt.date] = lambda: clock.now_ny().date(), bus: Any = BUS) -> None:
        self.cfg, self.store, self.book, self.sec, self.bus = cfg, store, book, sec, bus
        self.state_path = state_path
        self.sentiment = sentiment or HeadlineSentiment()
        self.settings = InsiderSettings(
            window_days=cfg.insider_window_days, history_days=cfg.insider_history_days, buy_floor=cfg.buy_floor,
            buy_full=cfg.buy_full, sell_floor=cfg.sell_floor, sell_full=cfg.sell_full,
            unusual_buying=cfg.unusual_buying, unusual_selling=cfg.unusual_selling)
        self._watched, self._news_source, self._con_ids = watched, news_source, con_ids
        self._finnhub_key, self._today = finnhub_key, today
        self._ciks: Dict[str, int] = {}
        self._ciks_on: Optional[dt.date] = None
        self._stop: Optional[threading.Event] = None
        self._checking = threading.Lock()
        self._insiders_at = self._news_at = self._calendar_at = float("-inf")
        #: every earnings report seen (signals/calendar.py), kept next to the signals' own state
        self.calendar = EarningsCalendarFile(state_path.parent / "earnings_calendar.json")
        self.book.set_earnings(self.calendar.by_symbol())
        self.report: Dict[str, Any] = {"insiders": {}, "news": {}, "calendar": {}, "errors": {}}

    # ---- the loop ------------------------------------------------------------ #
    def run(self, stop: threading.Event) -> None:
        self._stop = stop
        stop.wait(20.0)                                  # let the app connect first
        while not stop.is_set():
            with self._checking:
                if time.monotonic() - self._insiders_at >= self.cfg.insider_poll_minutes * 60:
                    self._safely("insider filings", self.poll_insiders)
                    self._insiders_at = time.monotonic()
                if time.monotonic() - self._news_at >= self.cfg.news_poll_minutes * 60:
                    self._safely("news", self.poll_news)
                    self._news_at = time.monotonic()
                if self._finnhub_key() and time.monotonic() - self._calendar_at >= self.cfg.calendar_hours * 3600:
                    self._safely("earnings calendar", self.poll_calendar)
                    self._calendar_at = time.monotonic()
            stop.wait(15.0)

    def check_now(self) -> Dict[str, Any]:
        """Read the filings and the news straight away, in the background - after adding a Finnhub key,
        say. The loop's own passes wait for it."""
        if not self.cfg.enabled:
            return {"ok": False, "reason": "The signals are off (signals.enabled in config.yaml)."}
        if self._checking.locked():
            return {"ok": True, "note": "A check is already running."}

        def check() -> None:
            with self._checking:
                self._safely("insider filings", self.poll_insiders)
                self._safely("news", self.poll_news)
                self._insiders_at = self._news_at = time.monotonic()
                if self._finnhub_key():
                    self._safely("earnings calendar", self.poll_calendar)
                    self._calendar_at = time.monotonic()
        threading.Thread(target=check, name="signals-check", daemon=True).start()
        return {"ok": True, "note": "Checking the insider filings and the news now - the page updates when it's done."}

    def _safely(self, what: str, step: Callable[[], Any]) -> None:
        try:
            step()
            self.report["errors"].pop(what, None)
        except Exception as e:  # noqa: BLE001
            log.warning("signals: the %s check failed: %s", what, e)
            self.report["errors"][what] = f"{type(e).__name__}: {e}"

    def _stopping(self) -> bool:
        return self._stop is not None and self._stop.is_set()

    # ---- insider filings ------------------------------------------------------- #
    def poll_insiders(self) -> None:
        today = self._today()
        state = self._state()
        read = sum(1 for _ in self._read_all(self.store.unread(self._missed_days(today, state) + self._latest())))
        read += self._read_histories(today, state)
        self._save_state(state)
        self.rebuild_insiders(today, state)
        self.report["insiders"] = {"checked_at": _now_iso(), "filings_read": read,
                                   "unusual_buying": self.book.unusual_buying_symbols()[:20]}
        self.bus.publish("signals.updated", report=self.report["insiders"])

    def _latest(self) -> List[FilingRef]:
        """The live feed, page by page, until it reaches filings read before."""
        refs: List[FilingRef] = []
        for page in range(MAX_FEED_PAGES):
            try:
                batch = latest_filings(self.sec.content(LATEST_URL.format(form="4", start=page * FEED_PAGE)))
            except Exception as e:  # noqa: BLE001
                log.debug("SEC live feed page %d: %s", page, e)
                break
            refs += batch
            if not batch or len(self.store.unread(batch)) < len(batch):
                break
        return refs

    def _missed_days(self, today: dt.date, state: Dict[str, Any]) -> List[FilingRef]:
        """Form 4 filings about listed companies from the daily indexes of recent trading
        days not read yet. Today's index is only published at night, so today comes from
        the live feed."""
        done = set(state["indexes_read"])
        day, days = today, []
        for _ in range(max(0, int(self.cfg.backfill_days))):
            day = clock.prev_trading_day(day)
            if day.isoformat() not in done:
                days.append(day)
        listed = set(self._company_ciks().values()) if days else set()
        refs: List[FilingRef] = []
        for day in days:
            if self._stopping():
                break
            try:
                refs += daily_index(self.sec.text(daily_index_url(day)), keep_cik=listed.__contains__ if listed else None)
                done.add(day.isoformat())
            except Exception as e:  # noqa: BLE001
                log.debug("SEC daily index %s: %s", day, e)
        state["indexes_read"] = sorted(done)[-60:]
        return refs

    def _read_histories(self, today: dt.date, state: Dict[str, Any]) -> int:
        """Past filings for the stocks with recent insider buying whose history is missing
        or a week old - a few companies per pass. Returns the filings read."""
        history: Dict[str, str] = state["history_read"]
        stale = (today - dt.timedelta(days=HISTORY_REFRESH_DAYS)).isoformat()
        buyers = self.store.recent_buyers(today - dt.timedelta(days=self.cfg.insider_window_days))
        due = [(s, cik) for s, cik in sorted(buyers.items()) if cik and history.get(s, "") <= stale]
        read = 0
        for symbol, cik in due[:HISTORY_PER_PASS]:
            if self._stopping():
                break
            got = self._read_history(cik, today)
            if got is not None:
                read += got
                history[symbol] = today.isoformat()
        state["history_read"] = {s: d for s, d in history.items()
                                 if d >= (today - dt.timedelta(days=self.cfg.insider_history_days)).isoformat()}
        return read

    def _read_history(self, cik: int, today: dt.date) -> Optional[int]:
        """Reads the company's Form 4 filings of the past year not read yet; returns how many,
        or None when its filing list couldn't be fetched."""
        try:
            doc = self.sec.json(SUBMISSIONS_URL.format(cik=cik))
        except Exception as e:  # noqa: BLE001
            log.debug("SEC submissions for CIK %s: %s", cik, e)
            return None
        since = today - dt.timedelta(days=self.cfg.insider_history_days)
        refs = self.store.unread(company_filings(doc, ("4",), since))[:MAX_HISTORY_FILINGS]
        return sum(1 for _ in self._read_all(refs))

    def _read_all(self, refs: Sequence[FilingRef]) -> Iterator[List[InsiderTrade]]:
        """Downloads ``refs`` a few at a time and stores each on this thread; yields each
        filing's trades. A filing that couldn't be downloaded is tried again next pass."""
        if not refs:
            return
        with ThreadPoolExecutor(max_workers=READERS, thread_name_prefix="sec-filings") as pool:
            for ref, trades in pool.map(self._download, refs):
                if trades is not None:
                    self.store.save_filing(ref, trades)
                    yield trades

    def _download(self, ref: FilingRef) -> Tuple[FilingRef, Optional[List[InsiderTrade]]]:
        if self._stopping():
            return ref, None
        try:
            trades = parse_form4(self.sec.content(ref.url), ref.accession)
        except Exception as e:  # noqa: BLE001
            log.debug("SEC filing %s: %s", ref.accession, e)
            return ref, None
        for t in trades:
            t.symbol = ibkr_symbol(t.symbol)
        return ref, trades

    def rebuild_insiders(self, today: dt.date, state: Optional[Dict[str, Any]] = None) -> None:
        history = (state or self._state())["history_read"]
        recent = self.store.symbols_traded(today - dt.timedelta(days=self.cfg.insider_window_days))
        by_symbol: Dict[str, List[InsiderTrade]] = defaultdict(list)
        for t in self.store.insider_trades(today - dt.timedelta(days=self.cfg.insider_history_days), recent):
            by_symbol[t.symbol].append(t)
        found = {}
        for symbol, trades in by_symbol.items():
            signals = {s.direction: s for s in insider_signals(symbol, trades, today, self.settings,
                                                               history_known=symbol in history)}
            if signals:
                found[symbol] = (signals.get("buying"), signals.get("selling"))
        self.book.set_insiders(found)

    def _state(self) -> Dict[str, Any]:
        try:
            doc = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {}
        return {"indexes_read": list(doc.get("indexes_read", [])), "history_read": dict(doc.get("history_read", {}))}

    def _save_state(self, state: Dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")

    # ---- news ------------------------------------------------------------------ #
    def poll_news(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        since = now - dt.timedelta(days=self.cfg.news_lookback_days)
        symbols = list(dict.fromkeys([*self._watched(), *self.book.unusual_buying_symbols()]))[:MAX_WATCHED]
        items = self._ibkr_news(symbols, since) + self._filings(symbols, since) + self._finnhub(symbols, since)
        new = self.store.save_news(items)
        scored = self._score_headlines()
        for signals in self.book.all():
            if signals.symbol not in symbols and (signals.news or signals.filings):
                self.book.set_news(signals.symbol, None, [])
        stories: Dict[str, List[Dict[str, Any]]] = {}
        for symbol in symbols:
            rows = self.store.news([symbol], since)
            filings = [r for r in rows if r["kind"] == "filing" and is_material(r["items"])]
            self.book.set_news(symbol, symbol_sentiment(rows, now), filings[:5])
            stories[symbol] = [{"at": r["published_at"], "kind": r["kind"], "headline": r["headline"],
                                "source": r["provider"] or r["source"]}
                               for r in rows if r["kind"] != "filing" or is_material(r["items"])]
        self.book.set_stories(stories, now)
        self.report["news"] = {"checked_at": _now_iso(), "watched": len(symbols), "new_stories": new,
                               "scored": scored, "finnhub": bool(self._finnhub_key())}
        self.bus.publish("signals.updated", report=self.report["news"])

    def stories_between(self, symbols: Sequence[str], start: dt.datetime,
                        end: dt.datetime) -> Dict[str, List[Dict[str, Any]]]:
        """Every source's stories about ``symbols`` published from ``start`` to ``end`` (UTC), oldest first
        per stock - fetched, kept with the rest of the news and scored. For the report on a session's movers."""
        symbols = list(dict.fromkeys(symbols))
        if symbols:
            self.store.save_news(self._ibkr_news(symbols, start, end, MOVER_HEADLINES) + self._filings(symbols, start)
                                 + self._finnhub(symbols, start))
            self._score_headlines()
        out: Dict[str, List[Dict[str, Any]]] = {s: [] for s in symbols}
        for row in reversed(self.store.news(symbols, start) if symbols else []):
            if dt.datetime.fromisoformat(row["published_at"]) <= end:
                out[row["symbol"]].append(row)
        return out

    def poll_calendar(self) -> None:
        """Every company's earnings reports from a week back to ``calendar_days_ahead`` ahead - one Finnhub request."""
        feed = FinnhubNews(self._finnhub_key())
        if not feed.configured:
            return
        today = self._today()
        events = feed.earnings_calendar(today - dt.timedelta(days=CALENDAR_DAYS_BACK),
                                        today + dt.timedelta(days=self.cfg.calendar_days_ahead))
        kept = self.calendar.merge(events, today)
        self.calendar.save()
        self.book.set_earnings(self.calendar.by_symbol())
        self.report["calendar"] = {"checked_at": _now_iso(), "read": len(events), "kept": kept,
                                   "upcoming": sum(1 for e in events if e.date >= today.isoformat())}
        self.bus.publish("signals.updated", report=self.report["calendar"])

    def _ibkr_news(self, symbols: List[str], since: dt.datetime, until: Optional[dt.datetime] = None,
                   per_symbol: int = 10) -> List[NewsItem]:
        source = self._news_source()
        if source is None or not hasattr(source, "news_headlines") or not symbols:
            return []
        items: List[NewsItem] = []
        found = source.news_headlines(self._con_ids(symbols), since=since, until=until, per_symbol=per_symbol)
        for symbol, rows in found.items():
            for when, provider, article, raw in rows:
                headline, kind = ibkr_headline(raw)
                if headline:
                    items.append(NewsItem(symbol=symbol, source="ibkr", provider=provider, headline=headline,
                                          published_at=_utc(when), kind=kind, ref=article))
        return items

    def _filings(self, symbols: List[str], since: dt.datetime) -> List[NewsItem]:
        ciks = self._company_ciks()
        items: List[NewsItem] = []
        for symbol in symbols:
            cik = ciks.get(sec_ticker(symbol))
            if not cik or self._stopping():
                continue
            try:
                items += eight_k_news(symbol, self.sec.json(SUBMISSIONS_URL.format(cik=cik)), since)
            except Exception as e:  # noqa: BLE001
                log.debug("SEC 8-K filings for %s: %s", symbol, e)
        return items

    def _finnhub(self, symbols: List[str], since: dt.datetime) -> List[NewsItem]:
        feed = FinnhubNews(self._finnhub_key())
        items: List[NewsItem] = []
        for symbol in symbols if feed.configured else []:
            if self._stopping():
                break
            try:
                items += feed.company_news(symbol, since.date(), self._today())
            except Exception as e:  # noqa: BLE001
                log.debug("Finnhub news for %s: %s", symbol, e)
        return items

    def company_ciks(self) -> Dict[str, int]:
        """SEC's ticker -> CIK map, read at most once a day."""
        return self._company_ciks()

    def _company_ciks(self) -> Dict[str, int]:
        today = self._today()
        if self._ciks_on != today:
            try:
                self._ciks, self._ciks_on = company_tickers(self.sec.json(TICKERS_URL)), today
            except Exception as e:  # noqa: BLE001
                log.debug("SEC ticker list: %s", e)
        return self._ciks

    def _score_headlines(self) -> int:
        if not self.cfg.sentiment or not self.sentiment.installed():
            return 0
        rows = self.store.unscored_news()
        scores = self.sentiment.score([headline for _, headline in rows])
        self.store.set_sentiment({key: score for (key, _), score in zip(rows, scores)})
        return len(scores)

    # ---- for the dashboard ----------------------------------------------------- #
    def state(self) -> Dict[str, Any]:
        signals = sorted(self.book.all(), key=lambda s: (-max(s.buying.score if s.buying else 0.0,
                                                             s.selling.score if s.selling else 0.0),
                                                         -abs(float((s.news or {}).get("score") or 0.0))))
        now, today = dt.datetime.now(dt.timezone.utc), self._today()
        filings = self.store.insider_filings(today - dt.timedelta(days=PAGE_DAYS))
        return {
            "enabled": bool(self.cfg.enabled), "report": self.report, "checking": self._checking.locked(),
            "sentiment": {**self.sentiment.status(), "wanted": bool(self.cfg.sentiment),
                          **self.store.sentiment_counts(now - dt.timedelta(days=PAGE_DAYS))},
            "finnhub": bool(self._finnhub_key()), "boosts": asdict(self.book.boosts),
            "settings": {"insider_poll_minutes": self.cfg.insider_poll_minutes,
                         "news_poll_minutes": self.cfg.news_poll_minutes, "news_lookback_days": self.cfg.news_lookback_days},
            "signals": [self._signal_row(s) for s in signals[:100]],
            "filings": filings[:150], "filing_delays": filing_delays(filings), "days": PAGE_DAYS,
            "headlines": self.store.news(None, now - dt.timedelta(days=self.cfg.news_lookback_days), PAGE_HEADLINES),
            "calendar": {"fetched_at": self.calendar.fetched_at, "reports": len(self.calendar),
                         "hours": self.cfg.calendar_hours},
        }

    def symbol_state(self, symbol: str) -> Dict[str, Any]:
        """One stock's signals, its insiders' filings over the past year and its recent news."""
        signals = self.book.get(symbol)
        filings = self.store.insider_filings(self._today() - dt.timedelta(days=self.cfg.insider_history_days), [symbol])
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=PAGE_DAYS)
        reports = self.book.earnings_for(symbol)
        upcoming = next_report(reports, clock.now_ny())
        return {"symbol": symbol, "signals": self._signal_row(signals) if signals else None, "filings": filings,
                "filing_delays": filing_delays(filings), "headlines": self.store.news([symbol], since, PAGE_HEADLINES),
                "earnings": reports[-6:],
                "next_earnings": {**upcoming, "sessions": sessions_until(upcoming, clock.now_ny())} if upcoming else None}

    def _signal_row(self, signals) -> Dict[str, Any]:
        effect = {}
        for side in (Side.LONG, Side.SHORT):
            delta, why = nudge(side, signals, self.book.boosts)
            effect[side.value] = {"delta": delta, "why": why}
        return {**signals.as_dict(), "effect": effect}


def _utc(when: dt.datetime) -> dt.datetime:
    """IBKR reports news times in UTC without saying so."""
    return when.replace(tzinfo=dt.timezone.utc) if when.tzinfo is None else when.astimezone(dt.timezone.utc)


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()
