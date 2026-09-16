"""Pairs trading from the engine's side: the pairs loop, refreshing the watch list, entering and closing
pairs, Autopilot's pair entries, and the Pairs tab's state.

A mixin of TradingEngine (engine.py): it works on the engine's own attributes and is kept apart
only so each concern reads on its own. Nothing here is instantiated by itself.
"""

from __future__ import annotations

import logging
import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Mapping, Optional

from ..research.history import replay_symbols
from ..pairs.desk import decision_window
from ..pairs.finder import FinderSettings
from ..pairs.model import KEY as PAIRS_KEY, PairRules
from ..scanner import schedule
from ..util import clock
from . import capital
from .support import attempt

log = logging.getLogger(__name__)


class PairsOps:
    # ------------------------------------------------------------------ #
    #  Pairs trading (pairs/)                                            #
    # ------------------------------------------------------------------ #
    PAIRS_POLL_S = 30.0

    #: the watch list's live prices are read for this long after the Pairs tab asks
    PAIRS_LIVE_S = 120.0

    #: while the stored candles lag the last session, the pairs are looked for again this often
    PAIRS_REFRESH_RETRY_S = 300.0

    def _pairs_loop(self) -> None:
        self._stop.wait(15.0)
        while not self._stop.is_set():
            try:
                self._pairs_pass()
            except Exception:  # noqa: BLE001
                log.exception("pairs pass failed")
            self._stop.wait(self.PAIRS_POLL_S)

    def _pairs_pass(self) -> None:
        cfg = self.settings.config.pairs
        if not cfg.enabled or self.executor is None:
            return
        self._refresh_pairs()
        self.pairs.sync(self.executor)
        now = clock.now_ny()
        window = decision_window(now, cfg)
        watch = (window or time.monotonic() < self._pairs_live_until
                 or clock.minutes_to_close(now) <= float(cfg.window_minutes[0]) + 10)
        prices = self._pair_prices(watch)
        self.pairs.manage(self.executor, prices, self.md.daily_frame, in_window=window, now=now)
        if window and not self.quit_state:
            self._autopilot_pairs(prices)
        view = self.pairs_state(prices=prices)
        signature = json.dumps(view, sort_keys=True, default=str)
        if signature != self._pairs_published:                 # only when something on the Pairs tab changed
            self._pairs_published = signature
            self._publish("pairs.updated", **view)

    def _pair_groups(self) -> Dict[str, str]:
        """The stocks pairs are looked for among - the watchlist's, grouped by IBKR industry -
        plus the ETF pairs in config.yaml."""
        groups: Dict[str, str] = {}
        for symbol in replay_symbols(self.scanner.watchlist)["swing"]:
            info = self.scanner.symbols.get(symbol)
            group = (info.industry or info.sector) if info is not None else ""
            if group:
                groups[symbol] = group
        for pair in self.settings.config.pairs.etf_pairs or []:
            if len(pair) == 2:
                for symbol in pair:
                    groups.setdefault(str(symbol).upper(), "ETF " + "/".join(str(s).upper() for s in pair))
        return groups

    def _refresh_pairs(self) -> None:
        if self.scanner.watchlist is None:
            return
        through = schedule.last_completed_session(clock.now_ny())
        if self.pairs.refreshed_for == through.isoformat() or time.monotonic() < self._pairs_next_refresh:
            return                                     # already fitted on the latest session, or tried a moment ago
        self._pairs_next_refresh = time.monotonic() + self.PAIRS_REFRESH_RETRY_S
        groups = self._pair_groups()
        etfs = [s for s, g in groups.items() if g.startswith("ETF ")]
        if etfs and self.md.attached and self._pair_etfs_on != through:
            try:
                self.md.update_daily(etfs, through)
            except Exception:  # noqa: BLE001
                log.debug("could not bring the ETF pairs' candles up to date", exc_info=True)
            self._pair_etfs_on = through
        frames = {s: f for s in groups if (f := self.md.daily_frame(s)) is not None}
        if frames:
            self.pairs.refresh(frames, groups)

    def _pair_replay_inputs(self) -> Optional[Dict[str, Any]]:
        groups = self._pair_groups()
        if not groups:
            return None
        cfg = self.settings.config
        return {"groups": groups, "finder": FinderSettings.from_config(cfg.pairs),
                "rules": PairRules.from_config(cfg.pairs, cost_bps=cfg.replay.slippage_bps + cfg.replay.commission_bps)}

    def _quotes(self, symbols: List[str]) -> Dict[str, float]:
        """The latest price of each symbol that has one."""
        if not symbols or not self.md.attached:
            return {}

        def one(symbol: str):
            try:
                q = self.md.quote(symbol)
                px = getattr(q, "last", 0.0) or getattr(q, "mid", 0.0)
                return symbol, float(px) if px else None
            except Exception:  # noqa: BLE001
                return symbol, None

        with ThreadPoolExecutor(max_workers=min(8, len(symbols))) as pool:
            return {s: px for s, px in pool.map(one, symbols) if px}

    def _pair_prices(self, watch: bool) -> Dict[str, float]:
        """Live prices for the legs of the pair trades on, and for the watched pairs when asked -
        in the regular session only; outside it the desk works from the closes."""
        if clock.current_session() is not clock.Session.REGULAR:
            return {}
        symbols = {t["symbol"] for t in self._open_trades() if t.get("pair_id")}
        if watch:
            symbols |= {s for m in self.pairs.models for s in (m.first, m.second)}
        return self._quotes(sorted(symbols))

    def pairs_state(self, prices: Optional[Mapping[str, float]] = None, live: bool = False) -> Dict[str, Any]:
        cfg = self.settings.config.pairs
        if live:
            self._pairs_live_until = time.monotonic() + self.PAIRS_LIVE_S
            if prices is None:
                prices = self._pair_prices(watch=True)
        prices = prices or {}
        now = clock.now_ny()
        ap = self.autopilot
        return {
            "enabled": bool(cfg.enabled), "refreshed_for": self.pairs.refreshed_for,
            "window": {"open": decision_window(now, cfg), "minutes": list(cfg.window_minutes),
                       "regular": clock.current_session(now) is clock.Session.REGULAR},
            "watch": self.pairs.watch(self.md.daily_frame, prices),
            "trades": self.pairs.trade_rows(prices, self.md.daily_frame),
            "recent": self.pairs.trade_rows({}, self.md.daily_frame, statuses=["CLOSED", "FAILED"], limit=20),
            "record": self.strategy_record(PAIRS_KEY),
            "autopilot": {"on": bool(ap.enabled and "PAIRS" in ap.trade_types), "proof": ap.proof_missing(PAIRS_KEY)},
            "limits": {"max_open_pairs": cfg.max_open_pairs, "max_new_per_day": cfg.max_new_per_day,
                       "emergency_loss_r": cfg.emergency_loss_r},
        }

    def enter_pair(self, pair_id: str, operator: str = "operator") -> Dict[str, Any]:
        """Put a watched pair on: both legs at the market, sized off the distance to its stop."""
        cfg = self.settings.config
        with self._switch_lock:
            locked = self._locked()
            if locked:
                return {"ok": False, "reason": locked}
            if not cfg.pairs.enabled:
                return {"ok": False, "reason": "Pairs trading is off (pairs.enabled in config.yaml)."}
            if self.executor is None:
                return {"ok": False, "reason": "No broker to send the orders to."}
            self._refresh_account()
            acc = self._account
            if acc is None:
                return {"ok": False, "reason": "no account data"}
            if self.mode == "live":
                if not self._armed:
                    return {"ok": False, "reason": f"engine not armed - live equity below "
                                                   f"${cfg.account.min_start_equity:,.0f} floor"}
                if cfg.account.cash_account:
                    return {"ok": False, "reason": "A pair shorts one of its stocks, which needs a margin account."}
                if acc.equity < cfg.account.pdt_equity_threshold:
                    return {"ok": False, "reason": (
                        f"Live pairs need ${cfg.account.pdt_equity_threshold:,.0f} in a margin account: both legs can "
                        "close the same day, and under that the pattern-day-trader rule would block them.")}
            if not acc.usd_per_base:
                return {"ok": False, "reason": f"no {acc.base_currency}->USD exchange rate yet, so the pair can't be sized"}
            model = self.pairs.model(pair_id)
            if model is None:
                return {"ok": False, "reason": f"{pair_id} isn't on the pairs watch list."}
            sizing = self.sizing_account(capital.SWING) or acc
            holding = ({t["symbol"] for t in self._open_trades()} | {w["symbol"] for w in self.working_entries()}
                       | {p.symbol for p in acc.positions if abs(p.quantity) > 1e-9})
            cap = float(cfg.risk.max_risk_per_trade_pct)
            kelly = self.strategy_risk_pct(PAIRS_KEY)
            pct = min(cap, kelly) if kelly is not None else cap
            buying_power = float(sizing.buying_power or sizing.equity)
            room = (getattr(sizing, "raw", None) or {}).get("capital_room")
            if room is not None:
                buying_power = min(buying_power, max(0.0, float(room)))
            out = attempt(self.pairs.enter,
                pair_id, executor=self.executor, account=acc, prices=self._quotes([model.first, model.second]),
                closes=self.md.daily_frame, risk_dollars=sizing.equity * pct / 100.0,
                max_leg_value=sizing.equity * float(cfg.risk.max_position_pct_of_equity) / 100.0,
                buying_power=buying_power, holding=holding, venue=self._venue, by=operator)
        self._refresh_account()
        self._publish("pairs.updated", **self.pairs_state(prices=self._pair_prices(watch=True)))
        return out

    def close_pair(self, pair_trade_id: str, reason: str = "manual") -> Dict[str, Any]:
        """Exit both legs of a pair at the market - allowed while quitting too."""
        if self.executor is None:
            return {"ok": False, "reason": "No broker to send the orders to."}
        out = self.pairs.close(pair_trade_id, self.executor, reason=reason)
        self._refresh_account()
        self._publish("account.snapshot", state=self.snapshot())
        return out

    def pair_chart(self, pair_id: str) -> Dict[str, Any]:
        model = self.pairs.model(pair_id)
        prices = self._pair_prices(watch=False)
        if model is not None and clock.current_session() is clock.Session.REGULAR:
            prices = {**prices, **self._quotes([model.first, model.second])}
        return self.pairs.chart(pair_id, self.md.daily_frame, prices)

    def _autopilot_pairs(self, prices: Mapping[str, float]) -> None:
        """Autopilot's pairs: only with PAIRS among its trade types, only once the replay has proven
        the pair rules, a few a day, inside its exposure cap."""
        ap, cfg = self.autopilot, self.settings.config.pairs
        status = ap.status()
        if not (status["enabled"] and status["effective"] and "PAIRS" in ap.trade_types):
            return
        if ap.require_proven and ap.proof_missing(PAIRS_KEY):
            return
        if self.repo.pair_trades_opened_on(clock.session_date()) >= int(cfg.max_new_per_day):
            return
        acc = self.sizing_account(capital.SWING)
        if acc is None or (acc.equity and self.gross_exposure() > acc.equity * ap.max_gross_exposure_pct / 100.0):
            return
        rows = [r for r in self.pairs.watch(self.md.daily_frame, prices) if r["signal"] and r["live"]]
        for row in sorted(rows, key=lambda r: abs(r["z"] or 0.0) - r["entry_z"], reverse=True):
            if ap.dry_run:
                noted = (clock.session_date().isoformat(), row["id"])
                self._pairs_dry_noted = {n for n in self._pairs_dry_noted if n[0] == noted[0]}
                if noted not in self._pairs_dry_noted:
                    self._pairs_dry_noted.add(noted)
                    log.info("autopilot DRY-RUN would enter pair %s (%s)", row["id"], row["side"])
                return
            out = self.enter_pair(row["id"], operator="autopilot")
            if out.get("ok"):
                log.warning("autopilot ENTERED pair %s: %s", row["id"], out.get("note"))
                return
