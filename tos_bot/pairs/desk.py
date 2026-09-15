"""The pair desk: watches the pairs' spreads and trades both legs of a pair together.

A pair trade is two ordinary trades - one per stock, tagged with the pair's id - and a pair
record that keeps the model it was entered on, its planned risk and what became of it.

How it stays safe:

- both legs are market orders, in the regular session only, sent one straight after the
  other. If the second can't be sent, doesn't fill, or both haven't filled within two
  minutes, whatever did fill is closed again: a pair is never left half on;
- the legs carry no stop or target of their own, so the regular exit manager leaves them
  alone. The desk closes both together: back at the mean, at the stop, at the time stop -
  and at once, any time of day, when the pair has lost ``emergency_loss_r`` times its
  planned risk;
- if one leg is closed or disappears outside the desk (an Exit click, Exit all, quitting,
  the broker check), the other leg is closed too;
- entries, and exits at the mean, the stop or the time stop, are decided in the last half
  hour of the session, when the day's close is nearly in - the way the replay reads them.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import threading
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

import numpy as np
import pandas as pd

from ..core.enums import PlayStatus, Side, StrategyKind, Timeframe
from ..core.eventbus import BUS
from ..core.models import Play
from ..util import clock
from .backtest import validate
from .finder import FinderSettings, aligned_closes, find_pairs
from .model import (KEY, LONG_SPREAD, SHORT_SPREAD, PairModel, PairRules, current_z, pair_pl, rolling_stats, signal,
                    size_pair, spread)

log = logging.getLogger(__name__)

#: how a leg's order goes out: at the market, in the regular session, without a bracket of its own
LEG_PLAN = {"executable": True, "order_type": "MARKET", "order_session": "REGULAR",
            "session_label": "market (regular hours)", "limit_price": None, "stop_price": None, "tif": "DAY",
            "bracket_mode": "managed", "note": "one leg of a pair"}
ENTRY_TIMEOUT_S = 120.0
EXIT_RETRY_S = 30.0
ACTIVE = ["ENTERING", "OPEN", "CLOSING"]
EXIT_REASONS = {"exit": "mean", "stop": "stop", "time": "time-stop"}
CHART_SESSIONS = 150

Closes = Callable[[str], Optional[pd.DataFrame]]


def decision_window(now: dt.datetime, cfg) -> bool:
    """Whether it's the part of the session when the pairs' entries and exits are decided."""
    start, end = (float(x) for x in getattr(cfg, "window_minutes", (30, 5)))
    return end <= clock.minutes_to_close(now) <= start


class PairDesk:
    def __init__(self, repo, cfg, state_path: Path, bus=BUS) -> None:
        self.repo, self.cfg, self.state_path, self.bus = repo, cfg, state_path, bus
        self.rules = PairRules.from_config(cfg)
        self.settings = FinderSettings.from_config(cfg)
        self.models: List[PairModel] = []
        self.refreshed_for: Optional[str] = None
        self._legs: Dict[str, Tuple[Play, Play]] = {}
        self._entering_at: Dict[str, float] = {}
        self._exit_sent_at: Dict[str, float] = {}
        self._lock = threading.RLock()
        self._load()

    # ------------------------------------------------------------------ the watch list
    def model(self, pair_id: str) -> Optional[PairModel]:
        return next((m for m in list(self.models) if m.id == pair_id), None)

    def refresh(self, frames: Mapping[str, pd.DataFrame], groups: Mapping[str, str], force: bool = False) -> bool:
        """Find the pairs again once a new session's candles are in. Returns whether it ran."""
        latest = [f.index[-1].date() for f in frames.values() if f is not None and len(f)]
        if not latest:
            return False
        through = max(latest).isoformat()
        if not force and through == self.refreshed_for:
            return False
        found = find_pairs(frames, groups, self.rules, self.settings)
        kept = []
        for m in found:
            m.validation = validate(m.first, m.second, frames, self.rules, self.settings, int(self.cfg.test_days))
            if bool(self.cfg.require_stable) and m.validation.get("stable") is False:
                continue
            kept.append(m)
        with self._lock:
            self.models, self.refreshed_for = kept, through
        self._save()
        log.info("pairs: watching %d of %d cointegrated pair(s)%s", len(kept), len(found),
                 f": {', '.join(m.id for m in kept)}" if kept else "")
        return True

    def watch(self, closes: Closes, prices: Mapping[str, float]) -> List[Dict[str, Any]]:
        """Each watched pair with its spread's z-score now - on the live prices when both are given."""
        rows = []
        for m in list(self.models):
            fa, fb = closes(m.first), closes(m.second)
            if fa is None or fb is None:
                continue
            a, b, _ = aligned_closes(fa, fb)
            if not len(a):
                continue
            pa, pb = prices.get(m.first), prices.get(m.second)
            live = bool(pa and pb)
            z, sd = current_z(m, a, b, pa if live else None, pb if live else None)
            wanted = signal(z, None, 0, m)
            rows.append({**m.as_dict(), "z": _r(z), "spread_sd": _r(sd, 5), "live": live,
                         "price_first": round(pa if live else float(a[-1]), 4),
                         "price_second": round(pb if live else float(b[-1]), 4), "signal": wanted,
                         "side": {"enter_long": LONG_SPREAD, "enter_short": SHORT_SPREAD}.get(wanted)})
        return rows

    # ------------------------------------------------------------------ entering
    def enter(self, pair_id: str, *, executor, account, prices: Mapping[str, float], closes: Closes,
              risk_dollars: float, max_leg_value: float, buying_power: float, holding: Iterable[str],
              venue: str, by: str = "operator") -> Dict[str, Any]:
        m = self.model(pair_id)
        if m is None:
            return _no(f"{pair_id} isn't on the pairs watch list any more.")
        if clock.current_session() is not clock.Session.REGULAR:
            return _no("Pairs are entered in the regular session only - both legs go in at the market.")
        active = self.repo.pair_trades(statuses=ACTIVE)
        if any(r["pair"] == m.id for r in active):
            return _no(f"{m.id} is already on.")
        if len(active) >= int(self.cfg.max_open_pairs):
            return _no(f"{len(active)} pairs are already on (pairs.max_open_pairs).")
        busy = sorted(set(holding) & {m.first, m.second})
        if busy:
            return _no(f"Already holding or trading {', '.join(busy)} - a pair needs both stocks to itself.")
        pa, pb = prices.get(m.first), prices.get(m.second)
        if not pa or not pb:
            return _no(f"No live price for {m.first if not pa else m.second}.")
        fa, fb = closes(m.first), closes(m.second)
        if fa is None or fb is None:
            return _no("No daily candles for one of the two stocks.")
        a, b, _ = aligned_closes(fa, fb)
        z, sd = current_z(m, a, b, pa, pb)
        wanted = signal(z, None, 0, m)
        if wanted is None:
            return _no(f"The spread is at z {z:+.2f}, inside the ±{m.entry_z:.2f} band - nothing to trade yet.")
        size = size_pair(m, pa, pb, z, sd, risk_dollars, max_leg_value, buying_power)
        if size.qty_first < 1:
            return _no("The position size rounds to zero: " + "; ".join(size.caps))
        side = LONG_SPREAD if wanted == "enter_long" else SHORT_SPREAD
        pid = f"pair_{uuid.uuid4().hex[:12]}"
        self.repo.create_pair_trade({
            "id": pid, "pair": m.id, "first": m.first, "second": m.second, "side": side, "status": "ENTERING",
            "venue": venue, "by": by, "hedge": m.hedge, "lookback": m.lookback, "half_life": m.half_life,
            "entry_z": round(z, 3), "band_z": m.entry_z, "stop_z": m.stop_z, "exit_z": m.exit_z,
            "time_stop_days": m.time_stop_days, "spread_sd": round(sd, 6), "qty_first": size.qty_first,
            "qty_second": size.qty_second, "price_first": pa, "price_second": pb, "dollar_risk": size.dollar_risk,
            "model": m.as_dict()})
        long_first = side == LONG_SPREAD
        first = _leg(m, m.first, Side.LONG if long_first else Side.SHORT, pa, size.qty_first, pid)
        second = _leg(m, m.second, Side.SHORT if long_first else Side.LONG, pb, size.qty_second, pid)
        for leg in (first, second):
            self.repo.record_play(leg)                  # a trade's play must be on record before the trade opens
        sent = self._send(executor, first, account)
        if not sent.get("ok"):
            return self._fail(pid, f"The {m.first} order wasn't sent: {sent.get('reason')}")
        sent = self._send(executor, second, account)
        if not sent.get("ok"):
            self._unwind(executor, [first])
            return self._fail(pid, f"The {m.second} order wasn't sent ({sent.get('reason')}), so the {m.first} "
                                   "leg was closed again.")
        with self._lock:
            self._legs[pid], self._entering_at[pid] = (first, second), time.monotonic()
        self.sync(executor)
        status = (self.repo.get_pair_trade(pid) or {}).get("status", "ENTERING")
        self.bus.publish("pairs.entered", pair_trade_id=pid, pair=m.id, side=side, status=status, by=by)
        return {"ok": True, "pair_trade_id": pid, "status": status,
                "note": (f"{'Bought' if long_first else 'Shorted'} {size.qty_first:,} {m.first} and "
                         f"{'shorted' if long_first else 'bought'} {size.qty_second:,} {m.second} at the market - the "
                         f"spread was at z {z:+.2f}. Planned risk ${size.dollar_risk:,.0f}.")}

    def sync(self, executor) -> None:
        """Turn entering pairs into open ones once both legs have filled - or unwind them."""
        for rec in self.repo.pair_trades(statuses=["ENTERING"]):
            pid = rec["id"]
            legs = {t["symbol"]: t for t in self.repo.trades_for_pair(pid)}
            first, second = legs.get(rec["first"]), legs.get(rec["second"])
            if first and second and first["status"] == "OPEN" and second["status"] == "OPEN":
                self.repo.update_pair_trade(
                    pid, status="OPEN", trade_first_id=first["id"], trade_second_id=second["id"],
                    entry_first=first["entry_price"], entry_second=second["entry_price"],
                    qty_first=abs(first["quantity"]), qty_second=abs(second["quantity"]), opened_at=_now())
                self._forget(pid)
                self.bus.publish("pairs.opened", pair_trade_id=pid, pair=rec["pair"])
                log.info("pair %s on: %s", pid, rec["pair"])
                continue
            plays = self._legs.get(pid)
            failed = plays is not None and any(p.status in (PlayStatus.CANCELED, PlayStatus.ERROR) for p in plays)
            stale = time.monotonic() - self._entering_at.get(pid, float("-inf")) > ENTRY_TIMEOUT_S
            if not (failed or stale):
                continue
            for p in plays or ():
                if not p.trade_id:
                    executor.cancel_entries_for(p.id)
            for t in (first, second):
                if t and t["status"] == "OPEN":
                    executor.close_trade(t["id"], reason="pair-unwind")
            self._fail(pid, "One leg didn't fill, so the other was closed again." if failed else
                       "The legs didn't both fill in time, so what did fill was closed again.")

    # ------------------------------------------------------------------ managing
    def manage(self, executor, prices: Mapping[str, float], closes: Closes, *, in_window: bool,
               now: Optional[dt.datetime] = None) -> List[Dict[str, Any]]:
        """Close pairs whose rules say so, finish pairs whose legs are both out, and close any leg
        left without its partner."""
        now = now or clock.now_ny()
        pending = executor.pending_exit_trade_ids()
        legs = [t for t in self.repo.open_trades() if t.get("pair_id")]
        active = {r["id"]: r for r in self.repo.pair_trades(statuses=["OPEN", "CLOSING"])}
        entering = {r["id"] for r in self.repo.pair_trades(statuses=["ENTERING"])}
        for t in legs:
            if t["pair_id"] not in active and t["pair_id"] not in entering:
                self._close_leg(executor, t, "pair-unwind", pending)           # its pair is over or never came together
        by_pair: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
        for t in legs:
            by_pair[t["pair_id"]][t["symbol"]] = t
        acted: List[Dict[str, Any]] = []
        for rec in active.values():
            first, second = by_pair[rec["id"]].get(rec["first"]), by_pair[rec["id"]].get(rec["second"])
            if first is None and second is None:
                acted.append(self._finish(rec))
                continue
            if rec["status"] == "CLOSING" or first is None or second is None:
                if rec["status"] == "OPEN":
                    gone = rec["first"] if first is None else rec["second"]
                    rec = self.repo.update_pair_trade(rec["id"], status="CLOSING", exit_reason="leg-closed") or rec
                    log.warning("pair %s: the %s leg was closed outside the desk - closing the other", rec["pair"], gone)
                    self.bus.publish("pairs.broken", pair_trade_id=rec["id"], pair=rec["pair"], leg=gone)
                for t in (first, second):
                    if t:
                        self._close_leg(executor, t, _leg_reason(rec.get("exit_reason")), pending)
                continue
            reason, z, r_now = self._exit_reason(rec, prices, closes, in_window, now)
            if reason:
                self.repo.update_pair_trade(rec["id"], status="CLOSING", exit_reason=reason, exit_z_at=_r(z))
                for t in (first, second):
                    self._close_leg(executor, t, _leg_reason(reason), pending)
                log.info("pair %s: closing (%s, %+.2fR)", rec["pair"], reason, r_now)
                self.bus.publish("pairs.exiting", pair_trade_id=rec["id"], pair=rec["pair"], reason=reason, r=_r(r_now))
                acted.append({"pair_trade_id": rec["id"], "pair": rec["pair"], "reason": reason, "r": _r(r_now)})
        return acted

    def _exit_reason(self, rec: Mapping[str, Any], prices: Mapping[str, float], closes: Closes, in_window: bool,
                     now: dt.datetime) -> Tuple[Optional[str], Optional[float], float]:
        pa, pb = prices.get(rec["first"]), prices.get(rec["second"])
        if not pa or not pb or not rec.get("entry_first") or not rec.get("entry_second"):
            return None, None, 0.0
        pl = pair_pl(rec["side"], rec["qty_first"], rec["entry_first"], pa, rec["qty_second"], rec["entry_second"], pb)
        r_now = pl / rec["dollar_risk"] if rec.get("dollar_risk") else 0.0
        if r_now <= -float(self.cfg.emergency_loss_r):
            return "emergency-stop", None, r_now
        if not in_window or not rec.get("model"):
            return None, None, r_now
        fa, fb = closes(rec["first"]), closes(rec["second"])
        if fa is None or fb is None:
            return None, None, r_now
        model = PairModel.from_dict(rec["model"])
        a, b, _ = aligned_closes(fa, fb)
        z, _ = current_z(model, a, b, pa, pb)
        held = int(clock.trading_days_between(_as_dt(rec["opened_at"]), now)) if rec.get("opened_at") else 0
        return EXIT_REASONS.get(signal(z, rec["side"], held, model) or ""), z, r_now

    def close(self, pair_trade_id: str, executor, reason: str = "manual") -> Dict[str, Any]:
        """Exit both legs of a pair at the market."""
        rec = self.repo.get_pair_trade(pair_trade_id)
        if rec is None or rec["status"] not in ("OPEN", "CLOSING"):
            return _no("That pair trade isn't open.")
        self.repo.update_pair_trade(pair_trade_id, status="CLOSING", exit_reason=reason)
        results = []
        for t in self.repo.trades_for_pair(pair_trade_id):
            if t["status"] == "OPEN":
                out = executor.close_trade(t["id"], reason=_leg_reason(reason))
                self._exit_sent_at[t["id"]] = time.monotonic()
                results.append({"symbol": t["symbol"], "ok": bool(out.get("ok")), "reason": out.get("reason", "")})
        failed = [r for r in results if not r["ok"]]
        note = (f"Exit sent for both legs of {rec['pair']}." if not failed else
                f"Not every leg of {rec['pair']} could be closed ({'; '.join(r['symbol'] + ': ' + r['reason'] for r in failed)}) "
                "- the desk keeps trying.")
        return {"ok": not failed, "note": note, "results": results}

    def _close_leg(self, executor, t: Mapping[str, Any], reason: str, pending: set) -> None:
        if t["id"] in pending:
            return
        last = self._exit_sent_at.get(t["id"])
        if last is not None and time.monotonic() - last < EXIT_RETRY_S:
            return
        self._exit_sent_at[t["id"]] = time.monotonic()
        out = executor.close_trade(t["id"], reason=reason)
        if not out.get("ok"):
            log.warning("pair leg %s (%s) not closed: %s", t["id"], t["symbol"], out.get("reason"))

    def _finish(self, rec: Mapping[str, Any]) -> Dict[str, Any]:
        legs = self.repo.trades_for_pair(rec["id"])
        pl = round(sum(float(t.get("realized_pl") or 0.0) for t in legs), 2)
        reason = rec.get("exit_reason") or next((t["exit_reason"] for t in legs if t.get("exit_reason")), "") or "closed"
        r = round(pl / rec["dollar_risk"], 3) if rec.get("dollar_risk") else None
        note = "" if len(legs) == 2 else ("A leg's record was removed (its position was no longer at the broker), "
                                          "so its P/L isn't in the total.")
        self.repo.update_pair_trade(rec["id"], status="CLOSED", closed_at=_now(), realized_pl=pl, r_multiple=r,
                                    exit_reason=str(reason)[:24], notes=note)
        for t in legs:
            self._exit_sent_at.pop(t["id"], None)
        log.info("pair %s closed: %s, P/L %.2f (%s)", rec["id"], rec["pair"], pl, reason)
        self.bus.publish("pairs.closed", pair_trade_id=rec["id"], pair=rec["pair"], realized_pl=pl, r=r, reason=reason)
        return {"pair_trade_id": rec["id"], "pair": rec["pair"], "reason": "closed", "realized_pl": pl, "r": r}

    def _send(self, executor, leg: Play, account) -> Dict[str, Any]:
        """Send one leg. Whatever goes wrong on the way - even after the broker has filled it -
        leaves no shares behind without a record: they're closed again."""
        try:
            return executor.execute_play(leg, account, plan=dict(LEG_PLAN))
        except Exception as e:  # noqa: BLE001
            log.exception("the %s leg's order failed", leg.symbol)
            cleaned = self._flatten(executor, leg)
            return {"ok": False, "reason": f"{e}" + (" - the shares that had filled were closed again" if cleaned else "")}

    def _flatten(self, executor, leg: Play) -> bool:
        """Close what a leg left at the broker when its order failed half way. Both stocks were free
        before the pair, so shares in the leg's direction can only be the leg's."""
        if leg.trade_id:
            return bool(executor.close_trade(leg.trade_id, reason="pair-unwind").get("ok"))
        executor.cancel_entries_for(leg.id)
        try:
            position = executor.broker.get_account().position(leg.symbol)
        except Exception:  # noqa: BLE001
            log.error("couldn't check %s after its pair leg failed - check the position at the broker", leg.symbol)
            return False
        if position is None or abs(position.quantity) < 1e-9 or (position.quantity > 0) != (leg.side is Side.LONG):
            return False
        qty = min(abs(position.quantity), float(leg.suggested_qty))
        return executor.flatten_untracked(leg.symbol, "LONG" if position.quantity > 0 else "SHORT", qty)

    def _unwind(self, executor, plays: Iterable[Play]) -> None:
        for p in plays:
            if p.trade_id:
                executor.close_trade(p.trade_id, reason="pair-unwind")
            else:
                executor.cancel_entries_for(p.id)

    def _fail(self, pid: str, reason: str) -> Dict[str, Any]:
        rec = self.repo.update_pair_trade(pid, status="FAILED", closed_at=_now(), notes=reason) or {}
        self._forget(pid)
        log.warning("pair %s (%s) not entered: %s", pid, rec.get("pair"), reason)
        self.bus.publish("pairs.failed", pair_trade_id=pid, pair=rec.get("pair"), reason=reason)
        return _no(reason)

    def _forget(self, pid: str) -> None:
        with self._lock:
            self._legs.pop(pid, None)
            self._entering_at.pop(pid, None)

    # ------------------------------------------------------------------ what the dashboard shows
    def trade_rows(self, prices: Mapping[str, float], closes: Closes, statuses: List[str] = ACTIVE,
                   limit: int = 50) -> List[Dict[str, Any]]:
        rows = []
        now = clock.now_ny()
        for rec in self.repo.pair_trades(statuses=statuses, limit=limit):
            row = {k: v for k, v in rec.items() if k != "model"}
            pa, pb = prices.get(rec["first"]), prices.get(rec["second"])
            if rec["status"] in ("OPEN", "CLOSING") and pa and pb and rec.get("entry_first") and rec.get("entry_second"):
                pl = pair_pl(rec["side"], rec["qty_first"], rec["entry_first"], pa, rec["qty_second"],
                             rec["entry_second"], pb)
                row["unrealized_pl"] = round(pl, 2)
                row["r_now"] = round(pl / rec["dollar_risk"], 2) if rec.get("dollar_risk") else None
            if rec["status"] in ACTIVE and rec.get("model"):
                fa, fb = closes(rec["first"]), closes(rec["second"])
                if fa is not None and fb is not None:
                    a, b, _ = aligned_closes(fa, fb)
                    row["z_now"] = _r(current_z(PairModel.from_dict(rec["model"]), a, b, pa, pb)[0])
            if rec.get("opened_at") and rec["status"] in ACTIVE:
                row["days_held"] = round(clock.trading_days_between(_as_dt(rec["opened_at"]), now), 1)
            rows.append(row)
        return rows

    def chart(self, pair_id: str, closes: Closes, prices: Mapping[str, float]) -> Dict[str, Any]:
        """The spread's z-score over the last sessions, with the band, the stop and this pair's trades."""
        model = self.model(pair_id)
        records = [r for r in self.repo.pair_trades(limit=200) if r["pair"] == pair_id]
        if model is None and records and records[0].get("model"):
            model = PairModel.from_dict(records[0]["model"])
        if model is None:
            return _no(f"{pair_id} isn't watched or traded.")
        fa, fb = closes(model.first), closes(model.second)
        if fa is None or fb is None:
            return _no("No daily candles for one of the two stocks.")
        a, b, dates = aligned_closes(fa, fb)
        z, _ = rolling_stats(spread(_log(a), _log(b), model.hedge), model.lookback)
        points = [{"date": d.isoformat(), "z": round(float(v), 3)}
                  for d, v in zip(dates[-CHART_SESSIONS:], z[-CHART_SESSIONS:]) if math.isfinite(v)]
        live_z = None
        if prices.get(model.first) and prices.get(model.second):
            live_z = _r(current_z(model, a, b, prices[model.first], prices[model.second])[0])
        trades = [{"side": r["side"], "status": r["status"], "opened_at": r.get("opened_at"), "closed_at": r.get("closed_at"),
                   "entry_z": r.get("entry_z"), "exit_z": r.get("exit_z_at"), "r": r.get("r_multiple")}
                  for r in records if r["status"] != "FAILED"]
        return {"ok": True, "pair": model.as_dict(), "points": points, "live_z": live_z, "trades": trades}

    # ------------------------------------------------------------------ storage
    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"refreshed_for": self.refreshed_for,
                                       "models": [m.as_dict() for m in self.models]}), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError:
            log.warning("could not save the pairs watch list", exc_info=True)

    def _load(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        try:
            self.models = [PairModel.from_dict(m) for m in data.get("models", [])]
            self.refreshed_for = data.get("refreshed_for")
        except TypeError:
            self.models, self.refreshed_for = [], None


def _leg(model: PairModel, symbol: str, side: Side, price: float, qty: int, pair_trade_id: str) -> Play:
    """One leg's play. Its stop and target are placeholders the trade record doesn't keep - the
    desk closes the legs together."""
    far = 0.5
    stop, target = (price * (1 - far), price * (1 + far)) if side is Side.LONG else (price * (1 + far), price * (1 - far))
    play = Play(symbol=symbol, side=side, strategy=KEY, kind=StrategyKind.TECHNICAL, timeframe=Timeframe.SWING,
                entry=round(price, 4), stop=round(stop, 4), targets=[round(target, 4)], confidence=0.5,
                rationale=f"one leg of the {model.id} pair", tags=["pair-leg"], pair_id=pair_trade_id)
    play.suggested_qty = int(qty)
    return play


def _leg_reason(reason: Optional[str]) -> str:
    return f"pair-{reason or 'exit'}"[:24]


def _no(reason: str) -> Dict[str, Any]:
    return {"ok": False, "reason": reason}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _as_dt(stamp: Any) -> dt.datetime:
    value = stamp if isinstance(stamp, dt.datetime) else dt.datetime.fromisoformat(str(stamp))
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def _log(values) -> np.ndarray:
    return np.log(np.asarray(values, dtype=float))


def _r(value: Optional[float], digits: int = 3) -> Optional[float]:
    return round(float(value), digits) if value is not None and math.isfinite(value) else None
