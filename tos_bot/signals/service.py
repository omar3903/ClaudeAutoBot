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
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from ..core.eventbus import BUS
from ..data.sec_edgar import sec_ticker
from ..data.sec_http import SEC, SecHttp
from ..util import clock
from .book import SignalBook
from .edgar import (LATEST_URL, SUBMISSIONS_URL, TICKERS_URL, FilingRef, company_filings, company_tickers,
                    daily_index, daily_index_url, latest_filings)
from .finnhub import FinnhubNews
from .form4 import InsiderTrade, parse_form4
from .insiders import InsiderSettings, insider_signals
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
        self.report: Dict[str, Any] = {"insiders": {}, "news": {}, "errors": {}}

    # ---- the loop ------------------------------------------------------------ #
    def run(self, stop: threading.Event) -> None:
        self._stop = stop
        stop.wait(20.0)                                  # let the app connect first
        insiders_at = news_at = float("-inf")
        while not stop.is_set():
            if time.monotonic() - insiders_at >= self.cfg.insider_poll_minutes * 60:
                self._safely("insider filings", self.poll_insiders)
                insiders_at = time.monotonic()
            if time.monotonic() - news_at >= self.cfg.news_poll_minutes * 60:
                self._safely("news", self.poll_news)
                news_at = time.monotonic()
            stop.wait(15.0)

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
        items = self._ibkr_news(symbols) + self._filings(symbols, since) + self._finnhub(symbols, since)
        new = self.store.save_news(items)
        scored = self._score_headlines()
        for signals in self.book.all():
            if signals.symbol not in symbols and (signals.news or signals.filings):
                self.book.set_news(signals.symbol, None, [])
        for symbol in symbols:
            rows = self.store.news([symbol], since)
            filings = [r for r in rows if r["kind"] == "filing" and is_material(r["items"])]
            self.book.set_news(symbol, symbol_sentiment(rows, now), filings[:5])
        self.report["news"] = {"checked_at": _now_iso(), "watched": len(symbols), "new_stories": new,
                               "scored": scored, "finnhub": bool(self._finnhub_key())}
        self.bus.publish("signals.updated", report=self.report["news"])

    def _ibkr_news(self, symbols: List[str]) -> List[NewsItem]:
        source = self._news_source()
        if source is None or not hasattr(source, "news_headlines") or not symbols:
            return []
        items: List[NewsItem] = []
        for symbol, rows in source.news_headlines(self._con_ids(symbols), days=self.cfg.news_lookback_days).items():
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
        signals = sorted(self.book.all(), key=lambda s: -max(s.buying.score if s.buying else 0.0,
                                                            s.selling.score if s.selling else 0.0))
        return {"enabled": bool(self.cfg.enabled), "report": self.report, "sentiment": self.sentiment.status(),
                "finnhub": bool(self._finnhub_key()), "signals": [s.as_dict() for s in signals[:100]]}


def _utc(when: dt.datetime) -> dt.datetime:
    """IBKR reports news times in UTC without saying so."""
    return when.replace(tzinfo=dt.timezone.utc) if when.tzinfo is None else when.astimezone(dt.timezone.utc)


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()
