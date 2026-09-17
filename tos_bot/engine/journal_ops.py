"""The daily journal and the session's movers report: the review after the close, the warning about
reports due, the movers' stories and charts, and the Reports page's state.

A mixin of TradingEngine (engine.py): it works on the engine's own attributes and is kept apart
only so each concern reads on its own. Nothing here is instantiated by itself.
"""

from __future__ import annotations

import logging
import datetime as dt
import time
from typing import Any, Dict, List, Mapping, Optional

from ..research.replay import ReplaySettings
from ..research.journal import ROLLING_SESSIONS, build_review, first_sightings, review_day
from ..research.movers import build_movers, read_session, rolling_capture, session_bounds
from ..signals.calendar import describe as describe_report, next_report, sessions_until
from .chart import candles
from .market_regime import BENCHMARK
from ..scanner.watchlist import DayWatchlist
from ..strategies.registry import REGISTRY
from ..util import clock
from .support import movers_built

log = logging.getLogger(__name__)


class JournalOps:
    # ------------------------------------------------------------------ #
    #  The journal                                                       #
    # ------------------------------------------------------------------ #
    JOURNAL_POLL_S = 60.0

    #: how long after starting the review waits for IB Gateway, to follow the plays not taken
    JOURNAL_GATEWAY_WAIT_S = 600.0

    #: how soon a session's movers are tried again when they couldn't be built (IB Gateway away)
    MOVERS_RETRY_S = 900.0

    #: a mover's chart: daily candles before the session, and after it once they exist
    MOVER_CHART_BEFORE, MOVER_CHART_AFTER = 60, 10

    def _journal_loop(self) -> None:
        self._stop.wait(20.0)
        while not self._stop.is_set():
            try:
                self._warn_earnings_ahead()
            except Exception:  # noqa: BLE001
                log.exception("the earnings check on open positions failed")
            try:
                self._review_if_due()
            except Exception:  # noqa: BLE001
                log.exception("the daily review failed")
            self._stop.wait(self.JOURNAL_POLL_S)

    def _review_if_due(self) -> None:
        cfg = self.settings.config.journal
        if not cfg.enabled:
            return
        day = review_day(clock.now_ny(), cfg.review_at)
        if self._journal_checked == day or time.monotonic() < self._movers_retry_at:
            return
        review = self.journal.get(day)
        wants_movers = cfg.movers > 0 and not movers_built(review)
        if review is not None and not wants_movers:
            self._journal_checked = day
            return
        if not self.md.attached and time.monotonic() - self._started_at < self.JOURNAL_GATEWAY_WAIT_S:
            return
        if wants_movers and self.md.attached:
            try:
                self.scanner.update_market_daily(day)
            except Exception:  # noqa: BLE001
                log.warning("could not download the session's daily candles for the movers", exc_info=True)
        out = self.review_session(day) if review is None else self.add_movers(day)
        if wants_movers and not movers_built(out.get("review")):
            self._movers_retry_at = time.monotonic() + self.MOVERS_RETRY_S
        else:
            self._journal_checked = day

    def _warn_earnings_ahead(self) -> None:
        """Say once when a swing position is held into an earnings report due by the next session:
        the report can gap the price straight through its stop (signals/calendar.py)."""
        if not self.settings.config.signals.enabled:
            return
        now = clock.now_ny()
        for t in self._open_trades():
            if t.get("timeframe") != "SWING" or t.get("pair_id"):
                continue
            upcoming = next_report(self.signal_book.earnings_for(t["symbol"]), now)
            if upcoming is None or sessions_until(upcoming, now) > 1 or (t["id"], upcoming["date"]) in self._earnings_warned:
                continue
            self._earnings_warned.add((t["id"], upcoming["date"]))
            note = (f"{t['symbol']} reports earnings {describe_report(upcoming)} and a swing position is open - "
                    "a report can gap the price straight through its stop.")
            log.warning(note)
            self._publish("position.earnings_ahead", symbol=t["symbol"], trade_id=t["id"], report=dict(upcoming), note=note)

    def review_session(self, day: Optional[dt.date] = None) -> Dict[str, Any]:
        """Build one session's review (again, if it exists) and keep it. Its movers are rebuilt once every
        stock's candles for the session are on disk; until then the ones built before are kept."""
        cfg = self.settings.config
        day = day or review_day(clock.now_ny(), cfg.journal.review_at)
        trades = [t for t in self.repo.closed_trades_between(day, day) if not t.get("pair_id")]
        plays = self.repo.plays_on(day)
        pair_trades = self.repo.pair_trades_closed_between(day, day)
        if not trades and not plays and not pair_trades and not self._movers_ready(day):
            return {"ok": False, "reason": f"Nothing was offered or traded on {day.isoformat()}."}
        first = min(clock.last_n_sessions(day, ROLLING_SESSIONS))
        ap = self.autopilot
        skipped = ap.skipped_noise()
        review = build_review(
            day, trades=trades, plays=plays, rolling=self.repo.closed_trades_between(first, day),
            replay_records=self.replay.records(*self._record_terms()), evidence=self.evidence_state(),
            regime=self.regime.reading(), bars=self._session_bars(day, plays),
            settings=ReplaySettings.from_exit_rules(cfg.exit_manager, cfg.replay, cfg.risk.min_reward_risk),
            skip_noise=skipped,
            min_confirmations=ap.min_confirmations, passes=self._passes_checks,
            styles={k: c.style for k, c in REGISTRY.items()}, titles={k: c.title for k, c in REGISTRY.items()},
            breakeven_at_r=float(cfg.exit_manager.breakeven_at_r))
        if pair_trades:
            review["pairs"] = [{k: r.get(k) for k in ("id", "pair", "side", "opened_at", "closed_at", "entry_z",
                                                   "exit_z_at", "exit_reason", "realized_pl", "r_multiple", "by")}
                              for r in pair_trades]
            total = sum(float(r.get("r_multiple") or 0.0) for r in pair_trades)
            review["lessons"].append(f"{len(pair_trades)} pair trade{'s' if len(pair_trades) != 1 else ''} "
                                     f"closed: {total:+.2f}R in all.")
        if cfg.journal.movers > 0:
            earlier = (self.journal.get(day) or {}).get("movers")
            review["movers"] = self._movers(day, review, plays) or (earlier if movers_built({"movers": earlier})
                                                                    else self._movers_pending())
            if not trades and not plays and not pair_trades and not movers_built(review):
                return {"ok": False, "reason": f"Nothing was offered or traded on {day.isoformat()}."}
            if not movers_built(review):
                self._journal_checked, self._movers_retry_at = None, 0.0     # the journal loop adds them
        self.journal.save(review)
        try:
            kept = self.repo.save_shadow_trades(day, review["shadows"].get("plays") or [])
            if kept:
                log.info("%d plays not taken on %s followed and kept for learning", kept, day.isoformat())
        except Exception:  # noqa: BLE001
            log.warning("the shadow trades couldn't be kept", exc_info=True)
        self._live_stats_at = float("-inf")
        self._publish("journal.updated", session=review["session"], mistakes=len(review["mistakes"]),
                    lessons=len(review["lessons"]))
        shadows = review["shadows"]
        return {"ok": True, "review": review,
                "note": (f"Reviewed {review['session']}: {review['day'].get('trades', 0)} closed trades, "
                         f"{len(review['mistakes'])} things to learn from, {shadows.get('filled', 0)} plays not taken "
                         "followed to their outcome.")}

    def add_movers(self, day: dt.date) -> Dict[str, Any]:
        """Add the movers to a session's review written without them."""
        review = self.journal.get(day)
        if review is None:
            return self.review_session(day)
        movers = self._movers(day, review, self.repo.plays_on(day))
        if movers is None:
            return {"ok": False, "reason": "The session's movers can't be built yet."}
        review["movers"] = movers
        self.journal.save(review)
        self._publish("journal.updated", session=review["session"], mistakes=len(review["mistakes"]),
                    lessons=len(review["lessons"]))
        return {"ok": True, "review": review}

    def _movers_ready(self, day: dt.date) -> bool:
        have = self.scanner.market_daily
        return self.settings.config.journal.movers > 0 and have is not None and have[0] >= day

    def _movers_pending(self) -> Dict[str, Any]:
        return {"ok": False, "note": "The market's biggest movers are added once every stock's candles for the session "
                                     "are in - a few minutes after the close with IB Gateway connected."
                                     if self.md.attached else
                                     "IB Gateway isn't connected - the market's biggest movers are added once it is."}

    def _movers(self, day: dt.date, review: Mapping[str, Any], plays: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """The session's biggest movers across the market (research/movers.py) - None until every stock's
        candles for the session are on disk."""
        if not self._movers_ready(day):
            return None
        cfg = self.settings.config
        _, tradable = self.scanner.market_daily
        try:
            universe = read_session(((s, self.md.daily_frame(s)) for s in tradable + [BENCHMARK]), day,
                                    sector_of=self.scanner.symbols.sector, prefilter=cfg.scanner.prefilter,
                                    sectors_allowed=self.filters.sectors, benchmark=BENCHMARK)
            ranked = sorted(universe.moves, key=lambda m: -abs(m.change_pct))
            symbols = [m.symbol for m in ranked if m.change_pct > 0][:cfg.journal.movers] + \
                      [m.symbol for m in ranked if m.change_pct < 0][:cfg.journal.movers]
            news, note = self._mover_news(day, symbols)
            if symbols and self.md.attached:                    # the session's 5-minute candles, for the charts
                try:
                    self.replay.history.load(self.md.source, symbols, 1, self.scanner.con_ids(symbols),
                                             today=clock.next_trading_day(day))
                except Exception:  # noqa: BLE001
                    log.debug("could not download the movers' 5-minute candles", exc_info=True)
            return build_movers(universe, per_side=cfg.journal.movers, sector_of=self.scanner.symbols.sector, news=news,
                                trades=self.repo.trades_on(day), plays=plays,
                                shadows=(review.get("shadows") or {}).get("plays") or [],
                                saved_watchlist=DayWatchlist.saved(self.scanner.watchlist_dir, day),
                                hot_size=self.scan_settings.hot_list_size, queue_size=self.scan_settings.sector_queue_size,
                                prefilter=cfg.scanner.prefilter, news_note=note)
        except Exception:  # noqa: BLE001
            log.exception("could not build the movers for %s", day)
            return None

    def _mover_news(self, day: dt.date, symbols: List[str]):
        if not self.settings.config.signals.enabled:
            return None, "News isn't read while the signals are off (signals.enabled in config.yaml)."
        start, _, end = session_bounds(day)
        try:
            news = self.signals.stories_between(symbols, start, end)
        except Exception:  # noqa: BLE001
            log.warning("could not read the movers' news", exc_info=True)
            return None, "The news couldn't be read."
        return news, "" if self.md.attached else "IB Gateway wasn't connected, so IBKR's news feeds weren't read."

    def mover_chart(self, day: dt.date, symbol: str) -> Dict[str, Any]:
        """A mover's daily candles around the session and the session's 5-minute candles, with what the bot
        did and the news - drawn by the Reports page."""
        movers = (self.journal.get(day) or {}).get("movers") or {}
        row = next((r for r in movers.get("gainers", []) + movers.get("losers", []) if r["symbol"] == symbol), None)
        if row is None:
            return {"ok": False, "reason": f"{symbol} isn't among that session's movers."}
        daily = self.md.daily_frame(symbol)
        if daily is not None and len(daily):
            at = int((daily.index.date <= day).sum())
            daily = daily.iloc[max(0, at - self.MOVER_CHART_BEFORE):at + self.MOVER_CHART_AFTER]
        frame = self.replay.history.stored(symbol)
        if (frame is None or not (frame.index.date == day).any()) and self.md.attached:
            try:
                frame = self.replay.history.load(self.md.source, [symbol], 1, self.scanner.con_ids([symbol]),
                                                 today=clock.next_trading_day(day)).get(symbol)
            except Exception:  # noqa: BLE001
                log.debug("chart candles for %s on %s failed", symbol, day, exc_info=True)
        session = frame[frame.index.date == day] if frame is not None else None
        return {"ok": True, "symbol": symbol, "session": day.isoformat(), "row": row,
                "daily": candles(daily, self.MOVER_CHART_BEFORE + self.MOVER_CHART_AFTER),
                "intraday": candles(session, 200)}

    def _session_bars(self, day: dt.date, plays: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """The session's 5-minute candles for the plays not taken (kept with the replay's candles)."""
        symbols = list(dict.fromkeys(p["symbol"] for p in first_sightings(plays)))
        if not symbols:
            return {}
        if not self.md.attached:
            return None
        try:
            return self.replay.history.load(self.md.source, symbols, 1, self.scanner.con_ids(symbols),
                                            today=clock.next_trading_day(day))
        except Exception:  # noqa: BLE001
            log.warning("could not download the session's candles for the review", exc_info=True)
            return None

    def _passes_checks(self, row: Mapping[str, Any]) -> bool:
        """Whether a recorded play clears Autopilot's own checks on it (not the account's caps)."""
        ap = self.autopilot
        return (row.get("timeframe") in ap.play_types() and float(row.get("confidence") or 0) >= ap.min_confidence
                and float(row.get("reward_risk") or 0) >= ap.min_reward_risk
                and not set(ap.skipped_noise()).intersection(row.get("noise") or [])
                and (row.get("timeframe") != "INTRADAY" or int(row.get("confirmations") or 1) >= ap.min_confirmations))

    def journal_state(self, limit: int = 60) -> Dict[str, Any]:
        cfg = self.settings.config.journal
        return {"enabled": cfg.enabled, "review_at": cfg.review_at, "days": self.journal.days(limit)}

    def journal_review(self, day: dt.date) -> Optional[Dict[str, Any]]:
        review = self.journal.get(day)
        if movers_built(review):
            sessions = [d["session"] for d in self.journal.days(3 * ROLLING_SESSIONS) if d["session"] <= day.isoformat()]
            review["capture"] = rolling_capture(r for s in sessions if (r := self.journal.get(dt.date.fromisoformat(s))))
        return review
