"""All database reads/writes the engine and API need, in one place."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, delete, func, insert, or_, select, update

from ..core.models import Account, Play
from ..util import clock
from .db import session_scope
from .models_orm import (AccountSnapshot, DailyReviewLog, Fill, OrderAudit, PairTradeLog, PlayLog, ScanRun,
                         ShadowTradeLog, SimTradeLog, Trade)

log = logging.getLogger(__name__)

#: where a play row's evidence keeps why Autopilot refused it and how the play stood then (note_refusal)
REFUSAL = "autopilot_refused"


def _expected_exit_times(entry: dt.datetime, timeframe: str, typ: float, mx: float):
    """(expected_exit_at, overwatch_at) as naive-UTC datetimes."""
    if typ <= 0 and mx <= 0:
        return None, None
    entry_utc = entry if entry.tzinfo else entry.replace(tzinfo=dt.timezone.utc)
    if timeframe == "INTRADAY":
        exp = entry_utc + dt.timedelta(minutes=typ or mx)
        ow = entry_utc + dt.timedelta(minutes=mx or (typ * 2))
        # a day trade's overwatch can't run past the exit manager's EOD flatten
        ny_close = clock.regular_close_time(clock.session_date(entry_utc))
        close_utc = dt.datetime.combine(
            clock.session_date(entry_utc), ny_close, tzinfo=clock.NY
        ).astimezone(dt.timezone.utc)
        exp = min(exp, close_utc)
        ow = min(ow, close_utc)
    else:
        exp = clock.add_trading_days(entry_utc, typ or mx)
        ow = clock.add_trading_days(entry_utc, mx or (typ * 2))
    return exp.astimezone(dt.timezone.utc).replace(tzinfo=None), \
        ow.astimezone(dt.timezone.utc).replace(tzinfo=None)


def _time_status(t: Trade) -> Dict[str, Any]:
    """How the trade is doing against its expected timeline (informational)."""
    if not t.entry_time or t.status == "CLOSED":
        return {}
    now = dt.datetime.utcnow()
    et = t.entry_time
    held_min = max(0.0, (now - et).total_seconds() / 60.0)
    intraday = t.timeframe == "INTRADAY"
    held_str = (f"{held_min:.0f}m" if intraday
                else _dhm((now - et)))
    exp, ow = t.expected_exit_at, t.overwatch_at
    used_pct = None
    status = "on_track"
    if exp and ow and ow > et:
        span = (ow - et).total_seconds()
        used_pct = round(max(0.0, (now - et).total_seconds()) / span * 100.0, 1) if span else None
        if now >= ow:
            status = "overdue"
        elif now >= exp:
            status = "aging"
    return {
        "held_label": held_str,
        "expected_exit_at": exp.isoformat() if exp else None,
        "overwatch_at": ow.isoformat() if ow else None,
        "time_used_pct": used_pct,
        "time_status": status,
    }


def _dhm(td: dt.timedelta) -> str:
    s = int(td.total_seconds())
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _f(x) -> Optional[float]:
    return None if x is None else float(x)


def trade_to_dict(t: Trade) -> Dict[str, Any]:
    return {
        "id": t.id, "play_id": t.play_id, "symbol": t.symbol,
        "sector": getattr(t, "sector", "") or "", "side": t.side,
        "strategy": t.strategy, "kind": t.kind, "timeframe": t.timeframe, "broker": t.broker,
        "status": t.status, "quantity": _f(t.quantity),
        "initial_quantity": _f(getattr(t, "initial_quantity", None)),
        "banked_pl": _f(getattr(t, "banked_pl", 0.0)) or 0.0,
        "target2_price": _f(getattr(t, "target2_price", None)),
        "entry_price": _f(t.entry_price),
        "entry_time": t.entry_time.isoformat() if t.entry_time else None,
        "order_type": getattr(t, "order_type", None),
        "order_session": getattr(t, "order_session", None),
        "submitted_at": t.submitted_at.isoformat() if getattr(t, "submitted_at", None) else None,
        "exit_submitted_at": (t.exit_submitted_at.isoformat() if getattr(t, "exit_submitted_at", None) else None),
        "entry_latency_s": _f(getattr(t, "entry_latency_s", None)),
        "exit_latency_s": _f(getattr(t, "exit_latency_s", None)),
        "entry_context": getattr(t, "entry_context", None),
        "mfe_at": t.mfe_at.isoformat() if getattr(t, "mfe_at", None) else None,
        "decision_price": _f(getattr(t, "decision_price", None)), "spread_bps": _f(getattr(t, "spread_bps", None)),
        "entry_slippage_bps": _f(getattr(t, "entry_slippage_bps", None)),
        "exit_decision_price": _f(getattr(t, "exit_decision_price", None)),
        "exit_slippage_bps": _f(getattr(t, "exit_slippage_bps", None)),
        "stop_price": _f(t.stop_price), "target_price": _f(t.target_price),
        "initial_stop_price": _f(getattr(t, "initial_stop_price", None)),
        "initial_target_price": _f(getattr(t, "initial_target_price", None)),
        "hwm_price": _f(getattr(t, "hwm_price", None)),
        "managed_exit": bool(getattr(t, "managed_exit", True)),
        "overdue_notified": bool(getattr(t, "overdue_notified", False)),
        "exit_price": _f(t.exit_price),
        "exit_time": t.exit_time.isoformat() if t.exit_time else None,
        "exit_reason": t.exit_reason, "fees": _f(t.fees),
        "realized_pl": _f(t.realized_pl), "realized_pl_pct": _f(t.realized_pl_pct),
        "r_multiple": _f(t.r_multiple), "mae": _f(t.mae), "mfe": _f(t.mfe),
        "is_day_trade": bool(t.is_day_trade),
        "session_date": t.session_date.isoformat() if t.session_date else None,
        "notes": t.notes, "pair_id": getattr(t, "pair_id", None),
        # the times the plan gave it stay on a closed trade too, so its review can hold it to them
        "expected_exit_at": t.expected_exit_at.isoformat() if getattr(t, "expected_exit_at", None) else None,
        "overwatch_at": t.overwatch_at.isoformat() if getattr(t, "overwatch_at", None) else None,
        **_time_status(t),
    }


def play_to_dict(p: PlayLog) -> Dict[str, Any]:
    return {
        "id": p.id, "scan_run_id": p.scan_run_id,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "symbol": p.symbol, "sector": getattr(p, "sector", "") or "",
        "side": p.side, "strategy": p.strategy, "kind": p.kind,
        "timeframe": p.timeframe, "entry": _f(p.entry), "stop": _f(p.stop),
        "targets": p.targets, "reward_risk": _f(p.reward_risk),
        "confidence": _f(p.confidence), "score": _f(p.score),
        "suggested_qty": p.suggested_qty, "dollar_risk": _f(p.dollar_risk),
        "notional": _f(p.notional), "rationale": p.rationale,
        "explanation": p.explanation, "evidence": p.evidence, "tags": p.tags,
        "noise": list(getattr(p, "noise", None) or []), "confirmations": int(getattr(p, "confirmations", None) or 1),
        "probability": _f(getattr(p, "probability", None)),
        "status": p.status, "decided_by": getattr(p, "decided_by", None),
    }


class Repository:
    # -------------------------------------------------------------- #
    #  Scans + plays                                                #
    # -------------------------------------------------------------- #
    def record_scan(self, result, keep_rejected: bool = True, top_n: int = 60,
                    plays: Optional[List[Play]] = None) -> None:
        """The scan, and its plays: ``plays`` when given (the ones the board took - a setup already
        acted on this session isn't offered again, so it isn't logged again), else all of them. A play
        whose row says it was decided - sent, filled, dismissed - keeps that row as it is."""
        with session_scope() as s:
            s.merge(ScanRun(
                id=result.run_id, kind=result.kind, started_at=_naive(result.started_at),
                finished_at=_naive(result.finished_at),
                universe_size=result.universe_size, scanned=result.scanned,
                liquid=result.liquid, n_plays=len(result.plays),
                hot={"symbols": result.hot}, n_errors=len(result.errors),
                elapsed_s=result.elapsed_s,
            ))
            chosen = list(result.plays if plays is None else plays)
            for p in (chosen if keep_rejected else chosen[:top_n]):
                row = s.get(PlayLog, p.id)
                if row is not None and row.status != "PROPOSED":
                    continue
                s.merge(_keeping_refusal(row, _play_row(p)))

    def record_play(self, play: Play) -> None:
        with session_scope() as s:
            s.merge(_keeping_refusal(s.get(PlayLog, play.id), _play_row(play)))

    def note_refusal(self, play: Play, refusal: Dict[str, Any]) -> None:
        """Why Autopilot refused a play at the engine's assessment or at the last look before the order, and how the
        play stood then, kept in its row's evidence (REFUSAL) - written first when no scan logged the play. Its status
        stays: the play is still offered (a click can take it, a change of settings hands it back to Autopilot), and
        the row written again by a later scan, or by an approval, keeps the note (_keeping_refusal)."""
        with session_scope() as s:
            row = s.get(PlayLog, play.id) or s.merge(_play_row(play))
            row.evidence = {**(row.evidence or {}), REFUSAL: dict(refusal)}

    def get_play(self, play_id: str) -> Optional[Dict[str, Any]]:
        with session_scope() as s:
            row = s.get(PlayLog, play_id)
            return play_to_dict(row) if row else None

    def settle_play(self, play_id: str, status: str, outcome: Optional[Dict[str, Any]] = None) -> bool:
        """What became of a play that was sent: its status, and ``outcome`` (the broker's word and the
        reason) kept in its evidence. Who decided and when stay as they were - Autopilot knows its own
        entries by them after a restart - and a play that became a trade stays FILLED."""
        with session_scope() as s:
            row = s.get(PlayLog, play_id)
            if row is None or row.status == "FILLED":
                return False
            row.status = status
            if outcome:
                row.evidence = {**(row.evidence or {}), "entry_outcome": dict(outcome)}
            return True

    def submitted_plays(self, since: dt.datetime, until: dt.datetime) -> List[Dict[str, Any]]:
        """The plays sent to a broker from ``since`` to ``until`` (when they were decided, else recorded) whose
        ending was never heard: still SUBMITTED, with no trade record - an entry that may have filled while the
        app was off (Executor._book_entries_filled_while_off). Oldest first."""
        sent = func.coalesce(PlayLog.decided_at, PlayLog.created_at)
        with session_scope() as s:
            rows = s.execute(select(PlayLog).where(PlayLog.status == "SUBMITTED", ~PlayLog.trade.has(),
                                                   sent >= _naive(since), sent < _naive(until))
                             .order_by(sent)).scalars().all()
            return [play_to_dict(r) for r in rows]

    def set_play_status(self, play_id: str, status: str, decided_by: str = "") -> None:
        with session_scope() as s:
            row = s.get(PlayLog, play_id)
            if row:
                row.status = status
                row.decided_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
                row.decided_by = decided_by

    # -------------------------------------------------------------- #
    #  Trades                                                       #
    # -------------------------------------------------------------- #
    def open_trade(
        self, play: Play, fill_price: float, fill_qty: float, broker: str,
        broker_order_id: str = "", commission: float = 0.0,
        order_type: str = "LIMIT", order_session: str = "REGULAR",
        entry_context: Optional[Dict[str, Any]] = None, submitted_at: Optional[dt.datetime] = None,
        decision: Optional[Dict[str, Any]] = None,
    ) -> str:
        """``entry_context``: the play's features at the fill (research/features.py), the row a
        model learns from; ``submitted_at``: when the entry order went out; ``decision``: the quote
        at the decision (``mid``, ``spread_bps``, ``live``), which the fill is measured against when it
        was live (_shortfall)."""
        tid = f"trd_{play.id.split('_', 1)[-1]}"
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        with session_scope() as s:
            existing = s.get(Trade, tid)
            if existing is not None:
                # a play produces exactly one trade - a repeat call is a
                # double-submit; ignore it rather than crash on the PK.
                log.warning("open_trade: %s already exists (%s) - ignoring repeat submit",
                            tid, existing.status)
                return tid
            exp_exit, overwatch = _expected_exit_times(
                now, play.timeframe.value,
                float(getattr(play, "expected_hold_typical", 0.0) or 0.0),
                float(getattr(play, "expected_hold_max", 0.0) or 0.0),
            )
            pair_id = getattr(play, "pair_id", None)     # a pair leg: no stop or target of its own
            s.add(Trade(
                id=tid, play_id=play.id, symbol=play.symbol,
                sector=getattr(play, "sector", "") or "", side=play.side.value,
                strategy=play.strategy, kind=play.kind.value, timeframe=play.timeframe.value,
                broker=broker, status="OPEN", quantity=fill_qty, initial_quantity=fill_qty,
                entry_price=fill_price,
                entry_time=now, order_type=order_type, order_session=order_session,
                submitted_at=_naive(submitted_at),
                entry_latency_s=None if broker == SIMULATOR else _took(submitted_at, now),
                entry_context=entry_context,
                **_shortfall(decision, fill_price, play.side.value),
                stop_price=None if pair_id else play.stop,
                target_price=None if pair_id else play.primary_target,
                target2_price=None if (pair_id or len(play.targets) < 2) else float(play.targets[1]),
                initial_stop_price=None if pair_id else play.stop,
                initial_target_price=None if pair_id else play.primary_target,
                hwm_price=fill_price, managed_exit=not pair_id, pair_id=pair_id, fees=commission,
                expected_exit_at=exp_exit, overwatch_at=overwatch,
                session_date=clock.session_date(),
                is_day_trade=(play.timeframe.value == "INTRADAY"),
            ))
            s.add(Fill(trade_id=tid, broker_order_id=broker_order_id, ts=now,
                       side=play.side.value, leg="ENTRY", quantity=fill_qty,
                       price=fill_price, commission=commission))
            row = s.get(PlayLog, play.id)
            if row:
                row.status = "FILLED"
        log.info("trade opened %s %s x%s @ %.4f (%s/%s)", tid, play.symbol, fill_qty,
                 fill_price, order_type, order_session)
        return tid

    def update_trade_risk(
        self, trade_id: str, *, stop_price: Optional[float] = None,
        target_price: Optional[float] = None, hwm_price: Optional[float] = None,
        mae: Optional[float] = None, mfe: Optional[float] = None,
        note_append: Optional[str] = None, managed_exit: Optional[bool] = None,
    ) -> None:
        with session_scope() as s:
            t = s.get(Trade, trade_id)
            if t is None or t.status == "CLOSED":
                return
            if stop_price is not None:
                t.stop_price = stop_price
            if target_price is not None:
                t.target_price = target_price
            if hwm_price is not None:
                t.hwm_price = hwm_price
            if mae is not None:
                t.mae = mae
            if mfe is not None:
                if t.mfe is None or float(mfe) > float(t.mfe):
                    t.mfe_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
                t.mfe = mfe
            if managed_exit is not None:
                t.managed_exit = managed_exit
            if note_append:
                t.notes = ((t.notes + " | ") if t.notes else "") + note_append

    def note_overdue(self, trade_id: str) -> None:
        with session_scope() as s:
            t = s.get(Trade, trade_id)
            if t and not t.overdue_notified:
                t.overdue_notified = True

    def close_trade(
        self, trade_id: str, exit_price: float, exit_reason: str = "manual",
        commission: float = 0.0, exit_qty: Optional[float] = None,
        exit_time: Optional[dt.datetime] = None, decision_price: Optional[float] = None,
        submitted_at: Optional[dt.datetime] = None, broker_order_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        """``exit_time``: when the position actually closed, for a fill learned after the fact
        (default: now); ``decision_price``: the price that triggered the exit, which the fill is
        measured against; ``submitted_at``: when the app's exit order went out, so the seconds it
        took to fill are kept (a stop or target resting at the broker has none - it waits for the
        price, not for the broker); ``broker_order_id``: the order that filled, which a fee the broker
        reports later is put down to (add_fill_fees). A record closed already is returned as it stands
        and nothing more is booked: the close takes it from OPEN in one conditional write, so two closes
        of the same record at once - the order sync's and the position check's, say - can't both book."""
        now = (exit_time.astimezone(dt.timezone.utc).replace(tzinfo=None) if exit_time and exit_time.tzinfo
               else exit_time) or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        with session_scope() as s:
            out, closed = _close(s, trade_id, exit_price, exit_reason, now, commission=commission, exit_qty=exit_qty,
                                 decision_price=decision_price, submitted_at=submitted_at,
                                 broker_order_id=broker_order_id)
        if closed:
            log.info("trade closed %s: P/L %.2f (%s)", trade_id, out["realized_pl"], exit_reason)
        return out

    def reduce_trade(self, trade_id: str, exit_qty: float, exit_price: float, exit_reason: str = "target-1",
                     commission: float = 0.0, stop_price: Optional[float] = None,
                     target_price: Optional[float] = None, broker_order_id: str = "") -> Optional[Dict[str, Any]]:
        """Book part of a position taken off - the scale-out at the first target: those shares
        leave the record, what they made after their fee is banked toward the trade's final P/L, and
        the stop and target move on to what the rest of the position now has to do (the stop only ever
        in the trade's favour). A part covering the whole position closes the trade instead. The check
        and the booking are one transaction, so two bookings at once can't both take the same shares off,
        nor one take a part off a record another has just closed. A part sold on the session the trade
        was entered makes it a day trade - one buy and a sale the same day - whatever becomes of the rest."""
        qty = float(exit_qty)
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        with session_scope() as s:
            # the shares come off first, in one conditional write: only from an OPEN record holding more than them.
            # The write also locks the record (SQLite: the database) until this commits, so what is read below stays
            # as read
            cut = qty > 0 and s.execute(
                update(Trade).where(Trade.id == trade_id, Trade.status == "OPEN", Trade.quantity > qty + 1e-9)
                .values(quantity=Trade.quantity - qty,
                        initial_quantity=func.coalesce(Trade.initial_quantity, Trade.quantity))
                .execution_options(synchronize_session=False)).rowcount == 1
            t = s.get(Trade, trade_id)
            if not cut:
                if t is None or t.status != "OPEN" or qty <= 0:
                    return trade_to_dict(t) if t else None
                # the part is the whole position: it closes the trade, in this same transaction
                out, closed = _close(s, trade_id, exit_price, exit_reason, now, commission=commission,
                                     broker_order_id=broker_order_id)
            else:
                closed = False
                sign = 1.0 if t.side == "LONG" else -1.0
                gross = (exit_price - float(t.entry_price)) * qty * sign - commission
                t.banked_pl = float(t.banked_pl or 0.0) + gross
                t.fees = float(t.fees or 0.0) + commission
                if stop_price is not None:
                    current = t.stop_price
                    better = current is None or (stop_price > float(current) if sign > 0
                                                 else stop_price < float(current))
                    if better:
                        t.stop_price = stop_price
                if target_price is not None:
                    t.target_price = target_price
                _fold_fill(t, exit_price, now)
                if t.entry_time and clock.session_date(_as_utc(t.entry_time)) == clock.session_date(_as_utc(now)):
                    t.is_day_trade = True
                note = f"took {qty:g} off at {exit_price:.2f} ({exit_reason}), {gross:+.2f} banked"
                t.notes = ((t.notes + " | ") if t.notes else "") + note
                s.add(Fill(trade_id=trade_id, broker_order_id=broker_order_id or "", ts=now,
                           side=("SHORT" if t.side == "LONG" else "LONG"), leg="EXIT", quantity=qty, price=exit_price,
                           commission=commission))
                out = trade_to_dict(t)
        if closed:
            log.info("trade closed %s: P/L %.2f (%s)", trade_id, out["realized_pl"], exit_reason)
        elif cut:
            log.info("trade reduced %s: %s, %s left", trade_id, note, out["quantity"])
        return out

    # -------------------------------------------------------------- #
    #  Fees the broker reports after the fill                       #
    # -------------------------------------------------------------- #
    def fills_on(self, venue: str, day: dt.date) -> List[Dict[str, Any]]:
        """The fills booked during the New York day ``day`` on trades at ``venue`` that carry the broker's order id,
        with the fee each was booked with - IBKR's commission report comes a moment after the fill, so a fill is
        mostly booked before its fee is known, or all of it (Executor._top_up_fees adds the rest). Oldest first."""
        start, end = _ny_bounds(day)
        with session_scope() as s:
            rows = s.execute(select(Fill, Trade.symbol).join(Trade, Fill.trade_id == Trade.id)
                             .where(Trade.broker == venue, Fill.ts >= start, Fill.ts < end, Fill.broker_order_id != "")
                             .order_by(Fill.ts, Fill.id)).all()
            return [{"fill_id": f.id, "trade_id": f.trade_id, "symbol": symbol, "order_id": f.broker_order_id,
                     "leg": f.leg, "quantity": float(f.quantity), "commission": float(f.commission or 0.0),
                     "ts": f.ts} for f, symbol in rows]

    def add_fill_fees(self, fees: Dict[int, float]) -> List[str]:
        """Fees learned after their fills were booked, by fill row id -> what each fill's fee grows by: it goes on the
        fill and on its trade's fees. A part taken off earlier banked what it made after its fee, so its fee comes off
        what it banked; a closed trade's P/L loses each one, and its % and R follow. Returns the trades changed."""
        changed: List[str] = []
        with session_scope() as s:
            for fill_id, fee in fees.items():
                f = s.get(Fill, fill_id)
                if f is None or not fee:
                    continue
                t = s.get(Trade, f.trade_id)
                if t is None:
                    continue
                fee = float(fee)
                closing = t.status == "CLOSED" and f.id == s.execute(
                    select(func.max(Fill.id)).where(Fill.trade_id == t.id, Fill.leg == "EXIT")).scalar()
                f.commission = float(f.commission or 0.0) + fee
                t.fees = float(t.fees or 0.0) + fee
                if f.leg == "EXIT" and not closing:
                    t.banked_pl = float(t.banked_pl or 0.0) - fee
                if t.status == "CLOSED" and t.realized_pl is not None:
                    _score(t, float(t.realized_pl) - fee, float(t.initial_quantity or t.quantity))
                changed.append(t.id)
        return changed

    def first_fee_day(self) -> Optional[dt.date]:
        """The New York day of the first fee on record for a trade at a broker (the simulator aside). Records from
        before it were booked without their commissions - IBKR keeps only the current day's executions, so they
        can't be had afterwards - and their P/L and R are before fees. None while no fee is on record."""
        with session_scope() as s:
            first = s.execute(select(func.min(Fill.ts)).join(Trade, Fill.trade_id == Trade.id)
                              .where(Fill.commission != 0, Trade.broker != SIMULATOR)).scalar()
        return _as_utc(first).astimezone(clock.NY).date() if first else None

    def open_trades(self) -> List[Dict[str, Any]]:
        with session_scope() as s:
            rows = s.execute(select(Trade).where(Trade.status == "OPEN")
                             .order_by(Trade.entry_time.desc())).scalars().all()
            return [trade_to_dict(r) for r in rows]

    def recent_trades(self, limit: int = 100) -> List[Dict[str, Any]]:
        with session_scope() as s:
            rows = s.execute(select(Trade).order_by(Trade.created_at.desc()).limit(limit)).scalars().all()
            return [trade_to_dict(r) for r in rows]

    def get_trade(self, trade_id: str) -> Optional[Dict[str, Any]]:
        with session_scope() as s:
            t = s.get(Trade, trade_id)
            return trade_to_dict(t) if t else None

    def get_open_trade_for_symbol(self, symbol: str) -> Optional[Dict[str, Any]]:
        with session_scope() as s:
            t = s.execute(select(Trade).where(Trade.symbol == symbol, Trade.status == "OPEN")
                          .order_by(Trade.entry_time.desc())).scalars().first()
            return trade_to_dict(t) if t else None

    def delete_trade(self, trade_id: str) -> bool:
        """Remove a trade and its fills. The order audit log is kept - it's the
        record of what was actually sent to a broker."""
        with session_scope() as s:
            t = s.get(Trade, trade_id)
            if t is None:
                return False
            s.delete(t)                       # fills go with it (cascade)
        log.warning("trade record %s deleted", trade_id)
        return True

    def trade_record(self, trade_id: str) -> Optional[Dict[str, Any]]:
        """Everything stored about one trade: the trade, the play that led to it,
        its fills and the broker orders sent for it."""
        with session_scope() as s:
            t = s.get(Trade, trade_id)
            if t is None:
                return None
            play = s.get(PlayLog, t.play_id) if t.play_id else None
            fills = s.execute(select(Fill).where(Fill.trade_id == trade_id)
                              .order_by(Fill.ts)).scalars().all()
            cond = OrderAudit.trade_id == trade_id
            if t.play_id:
                cond = or_(cond, OrderAudit.play_id == t.play_id)
            orders = s.execute(select(OrderAudit).where(cond).order_by(OrderAudit.ts)).scalars().all()
            return {
                "trade": trade_to_dict(t),
                "play": play_to_dict(play) if play else None,
                "fills": [{"ts": f.ts.isoformat() if f.ts else None, "leg": f.leg, "side": f.side,
                           "quantity": _f(f.quantity), "price": _f(f.price),
                           "commission": _f(f.commission), "broker_order_id": f.broker_order_id}
                          for f in fills],
                "orders": [{"ts": o.ts.isoformat() if o.ts else None, "action": o.action,
                            "ok": bool(o.ok), "broker": o.broker, "message": o.message,
                            "request": o.request,
                            # the broker's id for an order placed - the one its fills carry
                            "order_id": str((o.response if isinstance(o.response, dict) else {}).get("order_id")
                                            or "")} for o in orders],
            }

    # -------------------------------------------------------------- #
    #  PDT counter                                                  #
    # -------------------------------------------------------------- #
    def count_day_trades(self, lookback_sessions: int = 5, venue: Optional[str] = None) -> int:
        """The day trades of the last ``lookback_sessions`` sessions: the closed trades that were one (closed on
        the session they were entered, or a part sold then), the open ones with a part sold on the session they were
        entered - a day trade already, whatever becomes of the rest - and the open day trades entered today, which
        the session's end will make one. A trade counts once, however many parts it left in. ``venue``: only the
        trades booked there - the rule is counted per account, and the simulator's or the paper account's trades
        say nothing about the live one's."""
        today = clock.session_date()
        lo = clock.last_n_sessions(today, lookback_sessions)[0]
        here = _at_venue(venue)
        with session_scope() as s:
            closed = s.execute(
                select(func.count()).select_from(Trade)
                .where(Trade.is_day_trade.is_(True), Trade.status == "CLOSED",
                       Trade.session_date >= lo, *here)
            ).scalar_one()
            still_open = s.execute(select(Trade.id, Trade.timeframe, Trade.session_date)
                                   .where(Trade.status == "OPEN", Trade.session_date >= lo, *here)).all()
            parted = _sold_on_entry_session(s, {r.id: r.session_date for r in still_open})
        return int(closed) + sum(1 for r in still_open
                                 if r.id in parted or (r.timeframe == "INTRADAY" and r.session_date == today))

    # -------------------------------------------------------------- #
    #  Pair trades (pairs/desk.py)                                   #
    # -------------------------------------------------------------- #
    def create_pair_trade(self, row: Dict[str, Any]) -> None:
        with session_scope() as s:
            s.add(PairTradeLog(**_pair_columns(row)))

    def update_pair_trade(self, pair_trade_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        with session_scope() as s:
            row = s.get(PairTradeLog, pair_trade_id)
            if row is None:
                return None
            for name, value in _pair_columns(fields).items():
                setattr(row, name, value)
            return pair_to_dict(row)

    def get_pair_trade(self, pair_trade_id: str) -> Optional[Dict[str, Any]]:
        with session_scope() as s:
            row = s.get(PairTradeLog, pair_trade_id)
            return pair_to_dict(row) if row is not None else None

    def pair_trades(self, statuses: Optional[List[str]] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Pair trades, newest first - only those in ``statuses`` when given."""
        with session_scope() as s:
            query = select(PairTradeLog).order_by(PairTradeLog.created_at.desc()).limit(limit)
            if statuses:
                query = query.where(PairTradeLog.status.in_(list(statuses)))
            return [pair_to_dict(r) for r in s.execute(query).scalars().all()]

    def trades_for_pair(self, pair_trade_id: str) -> List[Dict[str, Any]]:
        """Both legs of a pair trade, open or closed."""
        with session_scope() as s:
            rows = s.execute(select(Trade).where(Trade.pair_id == pair_trade_id)
                             .order_by(Trade.entry_time)).scalars().all()
            return [trade_to_dict(r) for r in rows]

    def pair_trades_closed_between(self, first: dt.date, last: dt.date) -> List[Dict[str, Any]]:
        start, end = _ny_bounds(first)[0], _ny_bounds(last)[1]
        with session_scope() as s:
            rows = s.execute(select(PairTradeLog).where(PairTradeLog.status == "CLOSED",
                                                        PairTradeLog.closed_at >= start, PairTradeLog.closed_at < end)
                             .order_by(PairTradeLog.closed_at)).scalars().all()
            return [pair_to_dict(r) for r in rows]

    def pair_trades_opened_on(self, day: dt.date) -> int:
        """Pair trades entered during the New York session ``day`` - failed attempts aside."""
        start, end = _ny_bounds(day)
        with session_scope() as s:
            return int(s.execute(select(func.count()).select_from(PairTradeLog)
                                 .where(PairTradeLog.created_at >= start, PairTradeLog.created_at < end,
                                        PairTradeLog.status != "FAILED")).scalar() or 0)

    # -------------------------------------------------------------- #
    #  The journal (research/journal.py)                            #
    # -------------------------------------------------------------- #
    def closed_trades_between(self, first: dt.date, last: dt.date) -> List[Dict[str, Any]]:
        """Trades closed during the New York sessions ``first`` .. ``last``, oldest first, each
        with the play it came from - its evidence holds what the trade was taken on - and its exit
        fills averaged (exit_avg_price, exit_parts)."""
        start, end = _ny_bounds(first)[0], _ny_bounds(last)[1]
        with session_scope() as s:
            rows = s.execute(select(Trade, PlayLog).outerjoin(PlayLog, Trade.play_id == PlayLog.id)
                             .where(Trade.status == "CLOSED", Trade.exit_time >= start, Trade.exit_time < end)
                             .order_by(Trade.exit_time)).all()
            exits = _exit_fills(s, [t.id for t, _ in rows])
            return [{**trade_to_dict(t), **exits.get(t.id, NO_EXITS), "play": play_to_dict(p) if p is not None else None}
                    for t, p in rows]

    def trades_opened_between(self, first: dt.date, last: dt.date) -> List[Dict[str, Any]]:
        """Trades opened during the New York sessions ``first`` .. ``last`` - still open or closed
        since - oldest first, each with the play it came from and its exit fills so far averaged."""
        start, end = _ny_bounds(first)[0], _ny_bounds(last)[1]
        with session_scope() as s:
            rows = s.execute(select(Trade, PlayLog).outerjoin(PlayLog, Trade.play_id == PlayLog.id)
                             .where(Trade.entry_time >= start, Trade.entry_time < end)
                             .order_by(Trade.entry_time)).all()
            exits = _exit_fills(s, [t.id for t, _ in rows])
            return [{**trade_to_dict(t), **exits.get(t.id, NO_EXITS), "play": play_to_dict(p) if p is not None else None}
                    for t, p in rows]

    def trades_on(self, day: dt.date) -> List[Dict[str, Any]]:
        """Trades opened or closed during the New York session ``day``, oldest first."""
        start, end = _ny_bounds(day)
        with session_scope() as s:
            rows = s.execute(select(Trade).where(or_(and_(Trade.entry_time >= start, Trade.entry_time < end),
                                                     and_(Trade.exit_time >= start, Trade.exit_time < end)))
                             .order_by(Trade.entry_time)).scalars().all()
            return [trade_to_dict(t) for t in rows]

    def plays_on(self, day: dt.date, limit: int = 5000) -> List[Dict[str, Any]]:
        """The plays recorded during the New York session ``day``, oldest first, each with when the scan that
        last wrote it finished (``scan_finished_at``, None when that scan isn't on record): a row holds that
        scan's values, and they reached the board only when it finished."""
        start, end = _ny_bounds(day)
        with session_scope() as s:
            rows = s.execute(select(PlayLog, ScanRun.finished_at).outerjoin(ScanRun, PlayLog.scan_run_id == ScanRun.id)
                             .where(PlayLog.created_at >= start, PlayLog.created_at < end)
                             .order_by(PlayLog.created_at).limit(limit)).all()
            return [{**play_to_dict(r), "scan_finished_at": finished.isoformat() if finished else None}
                    for r, finished in rows]

    # -------------------------------------------------------------- #
    #  What a model learns from (research/dataset.py)               #
    # -------------------------------------------------------------- #
    def save_sim_trades(self, ran_at: str, trades, split: Optional[Dict[str, Optional[str]]] = None) -> str:
        """Keep one replay run's simulated trades, replacing the same run's rows if it is saved
        again. ``trades``: research/replay.py SimTrades. Returns the run id."""
        from ..research.replay import held_out

        run_id = "rpl_" + "".join(ch for ch in ran_at[:19] if ch.isdigit())
        stamp = _naive(dt.datetime.fromisoformat(ran_at)) if ran_at else None
        rows = [{
            "run_id": run_id, "ran_at": stamp, "strategy": t.strategy, "symbol": t.symbol, "side": t.side,
            "timeframe": t.timeframe, "entered_at": _naive_iso(t.entered_at), "exited_at": _naive_iso(t.exited_at),
            "entry_price": t.entry, "exit_price": t.exit, "r": t.r, "exit_reason": t.exit_reason, "noise": list(t.noise),
            "confirmed": bool(t.confirmed), "mfe_r": t.mfe_r, "scaled": bool(t.scaled),
            "held_out": held_out(t, split), "features": dict(t.features or {}),
            "feature_schema": int((t.features or {}).get("schema", 0) or 0),
            "drift_r": float(getattr(t, "drift_r", 0.0) or 0.0), "cost_r": float(getattr(t, "cost_r", 0.0) or 0.0),
        } for t in trades]
        with session_scope() as s:
            s.execute(delete(SimTradeLog).where(SimTradeLog.run_id == run_id))
            for start in range(0, len(rows), 500):
                s.execute(insert(SimTradeLog), rows[start:start + 500])
        return run_id

    def sim_runs(self) -> List[Dict[str, Any]]:
        """Each replay run kept, newest first, with its row count."""
        with session_scope() as s:
            rows = s.execute(select(SimTradeLog.run_id, func.max(SimTradeLog.ran_at), func.count())
                             .group_by(SimTradeLog.run_id).order_by(func.max(SimTradeLog.ran_at).desc())).all()
            return [{"run_id": r[0], "ran_at": r[1].isoformat() if r[1] else None, "trades": int(r[2])} for r in rows]

    def latest_sim_run(self) -> Optional[str]:
        runs = self.sim_runs()
        return runs[0]["run_id"] if runs else None

    def sim_trades(self, run_id: Optional[str] = None, limit: int = 200000) -> List[Dict[str, Any]]:
        with session_scope() as s:
            q = select(SimTradeLog)
            if run_id:
                q = q.where(SimTradeLog.run_id == run_id)
            rows = s.execute(q.order_by(SimTradeLog.exited_at).limit(limit)).scalars().all()
            return [sim_to_dict(r) for r in rows]

    def save_shadow_trades(self, day: dt.date, shadows: List[Dict[str, Any]]) -> int:
        """Keep the review's shadow trades (research/journal.py shadow_outcomes rows) for ``day`` in place of
        the ones an earlier build of that review kept, so the day's rows are the ones its latest review
        followed. No rows replaces nothing: the rows kept before stay."""
        rows = [row for row in shadows if row.get("play_id")]
        if not rows:
            return 0
        with session_scope() as s:
            # a play the rebuild no longer follows would otherwise go on teaching the model an outcome
            # the report has dropped
            s.execute(delete(ShadowTradeLog).where(ShadowTradeLog.session_date == day,
                                                   ShadowTradeLog.play_id.notin_([row["play_id"] for row in rows])))
            for row in rows:
                feats = dict(row.get("features") or {})
                s.merge(ShadowTradeLog(
                    play_id=row["play_id"], session_date=day, symbol=row["symbol"], strategy=row["strategy"],
                    side=row["side"], timeframe=row.get("timeframe") or "INTRADAY",
                    seen_at=_naive_iso(row.get("seen_at")), passed_checks=bool(row.get("passed_checks")),
                    filled=bool(row.get("filled")), entered_at=_naive_iso(row.get("entered_at")),
                    exited_at=_naive_iso(row.get("exited_at")), entry_price=row.get("entry"), exit_price=row.get("exit"),
                    r=row.get("r"), mfe_r=row.get("mfe_r"), exit_reason=str(row.get("exit_reason") or "")[:64],
                    noise=list(row.get("noise") or []), confirmations=int(row.get("confirmations") or 1),
                    features=feats, feature_schema=int(feats.get("schema", 0) or 0)))
        return len(rows)

    def shadow_trades(self, day: Optional[dt.date] = None, limit: int = 200000) -> List[Dict[str, Any]]:
        with session_scope() as s:
            q = select(ShadowTradeLog)
            if day is not None:
                q = q.where(ShadowTradeLog.session_date == day)
            rows = s.execute(q.order_by(ShadowTradeLog.seen_at).limit(limit)).scalars().all()
            return [shadow_to_dict(r) for r in rows]

    def save_review(self, day: dt.date, review: Dict[str, Any]) -> None:
        stats = review.get("day") or {}
        with session_scope() as s:
            s.merge(DailyReviewLog(
                session_date=day, created_at=dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
                trades=int(stats.get("trades") or 0), total_r=float(stats.get("total_r") or 0.0),
                realized_pl=float(stats.get("realized_pl") or 0.0), mistakes=len(review.get("mistakes") or []),
                opened=int(stats.get("opened") or 0), open_r=float(stats.get("open_r") or 0.0),
                review=review))

    def get_review(self, day: dt.date) -> Optional[Dict[str, Any]]:
        with session_scope() as s:
            row = s.get(DailyReviewLog, day)
            return dict(row.review) if row is not None and row.review else None

    def list_reviews(self, limit: int = 60) -> List[Dict[str, Any]]:
        with session_scope() as s:
            rows = s.execute(select(DailyReviewLog).order_by(DailyReviewLog.session_date.desc())
                             .limit(limit)).scalars().all()
            return [{"session": r.session_date.isoformat(), "trades": r.trades, "total_r": r.total_r,
                     "realized_pl": _f(r.realized_pl), "mistakes": r.mistakes,
                     "opened": int(r.opened or 0), "open_r": float(r.open_r or 0.0),
                     "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]

    # -------------------------------------------------------------- #
    #  P/L analytics                                                #
    # -------------------------------------------------------------- #
    def pnl_summary(self, venue: Optional[str] = None) -> Dict[str, Any]:
        """The closed trades' P/L figures. realized_today / realized_week are the trades closed this session and in
        the last five, by when they closed - a swing trade entered last week and closed today is today's - and with
        ``venue`` only those booked there, the account orders go to now. The rest covers every closed trade."""
        today = clock.session_date()
        wk = clock.last_n_sessions(today, 5)[0]
        # a session runs to the next one's day: a close booked on a weekend or a holiday belongs to the session
        # before it, as clock.session_date has it
        day_from, week_from = _ny_bounds(today)[0], _ny_bounds(wk)[0]
        until = _ny_bounds(clock.next_trading_day(today))[0]
        with session_scope() as s:
            closed = s.execute(select(Trade).where(Trade.status == "CLOSED")).scalars().all()
            pairs_open = set(s.execute(select(Trade.pair_id).where(Trade.status != "CLOSED",
                                                                   Trade.pair_id.is_not(None))).scalars().all())
        pls = [float(t.realized_pl) for t in closed if t.realized_pl is not None]
        # closed trades that made or lost money, by type: a pair is one trade - its legs' P/L together, once both
        # are closed
        by_type = {k: {"closed": 0, "profit": 0, "loss": 0, "even": 0} for k in ("INTRADAY", "SWING", "PAIRS")}
        pair_pl: Dict[str, float] = {}
        for t in closed:
            if t.realized_pl is None:
                continue
            if t.pair_id:
                if t.pair_id not in pairs_open:
                    pair_pl[t.pair_id] = pair_pl.get(t.pair_id, 0.0) + float(t.realized_pl)
                continue
            _count_outcome(by_type["INTRADAY" if t.timeframe == "INTRADAY" else "SWING"], float(t.realized_pl))
        for pl in pair_pl.values():
            _count_outcome(by_type["PAIRS"], pl)
        wins = [x for x in pls if x > 0]
        losses = [x for x in pls if x < 0]
        here = [t for t in closed if t.exit_time and (not venue or (t.broker or SIMULATOR) == venue)]
        day = sum(float(t.realized_pl or 0) for t in here if day_from <= t.exit_time < until)
        week = sum(float(t.realized_pl or 0) for t in here if week_from <= t.exit_time < until)
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        n = len(pls)
        return {
            "n_closed": n,
            "realized_total": round(sum(pls), 2),
            "realized_today": round(day, 2),
            "realized_week": round(week, 2),
            "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
            "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "expectancy": round(sum(pls) / n, 2) if n else 0.0,
            "best": round(max(pls), 2) if pls else 0.0,
            "worst": round(min(pls), 2) if pls else 0.0,
            "by_type": by_type,
        }

    # -------------------------------------------------------------- #
    #  Snapshots + audits                                           #
    # -------------------------------------------------------------- #
    def snapshot_account(self, account: Account, broker: str, realized_day: float = 0.0) -> None:
        with session_scope() as s:
            s.add(AccountSnapshot(
                broker=broker, equity=account.equity, cash=account.cash,
                buying_power=account.buying_power,
                day_trades_5d=self.count_day_trades(5, venue=broker),
                open_positions=len([p for p in account.positions if abs(p.quantity) > 1e-9]),
                unrealized_pl=sum(p.unrealized_pl for p in account.positions),
                realized_pl_day=realized_day,
            ))

    def record_order_audit(self, action: str, request: dict, response: dict, ok: bool,
                           broker: str, play_id: str = "", trade_id: str = "",
                           message: str = "", ts: Optional[dt.datetime] = None) -> None:
        """One row of the broker order audit (executor._audit): an order placed, a cancel or a change asked for, or
        an error the broker sent about one. ``ts``: when it happened, when that was before now - a broker's error is
        written on the order sync after it came, and is kept in its place among the rest."""
        with session_scope() as s:
            s.add(OrderAudit(action=action, request=request, response=response, ok=ok,
                             broker=broker, play_id=play_id or None, trade_id=trade_id or None,
                             message=message[:400], **({"ts": ts} if ts is not None else {})))


_PAIR_COLUMNS = {"first": "first_symbol", "second": "second_symbol"}
_PAIR_TIMES = ("opened_at", "closed_at", "created_at")


def _count_outcome(tally: Dict[str, int], pl: float) -> None:
    """One closed trade into its type's tally: a profit, a loss, or even."""
    tally["closed"] += 1
    tally["profit" if pl > 0 else "loss" if pl < 0 else "even"] += 1


def _pair_columns(row: Dict[str, Any]) -> Dict[str, Any]:
    """A pair trade's fields as table columns - unknown ones dropped, times made naive UTC."""
    known = set(PairTradeLog.__table__.columns.keys())
    out = {}
    for key, value in row.items():
        name = _PAIR_COLUMNS.get(key, key)
        if name not in known:
            continue
        if name in _PAIR_TIMES and isinstance(value, str):
            value = dt.datetime.fromisoformat(value)
        if name in _PAIR_TIMES and isinstance(value, dt.datetime):
            value = _naive(value)
        out[name] = value
    return out


def pair_to_dict(r: PairTradeLog) -> Dict[str, Any]:
    out = {("first" if c == "first_symbol" else "second" if c == "second_symbol" else c): getattr(r, c)
           for c in PairTradeLog.__table__.columns.keys()}
    for name in _PAIR_TIMES:
        out[name] = out[name].isoformat() if out[name] else None
    for name in ("qty_first", "qty_second", "price_first", "price_second", "entry_first", "entry_second",
                 "dollar_risk", "realized_pl"):
        out[name] = _f(out[name])
    return out


def _ny_bounds(day: dt.date):
    """A New York calendar day as naive-UTC bounds, the way the tables store times."""
    def utc(d: dt.date) -> dt.datetime:
        return dt.datetime.combine(d, dt.time(0), tzinfo=clock.NY).astimezone(dt.timezone.utc).replace(tzinfo=None)
    return utc(day), utc(day + dt.timedelta(days=1))


#: a trade with no exit fills on record (a record older than the fills, or one still whole)
NO_EXITS: Dict[str, Any] = {"exit_avg_price": None, "exit_parts": 0}


def _exit_fills(s, trade_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Each trade's exit fills, in one grouped query: their size-weighted average price and how many
    parts the position left in. A trade's exit_price is only its last part's, so a position taken off
    in parts needs the average for (exit - entry) x shares to come to what it made."""
    if not trade_ids:
        return {}
    rows = s.execute(select(Fill.trade_id, func.sum(Fill.quantity * Fill.price), func.sum(Fill.quantity), func.count())
                     .where(Fill.leg == "EXIT", Fill.trade_id.in_(trade_ids))
                     .group_by(Fill.trade_id)).all()
    return {tid: {"exit_avg_price": round(float(value) / float(qty), 6), "exit_parts": int(parts)}
            for tid, value, qty, parts in rows if qty}


# --------------------------------------------------------------------------- #
def _play_row(p: Play) -> PlayLog:
    return PlayLog(
        id=p.id, scan_run_id=p.scan_run_id, created_at=_naive(p.created_at),
        symbol=p.symbol, sector=getattr(p, "sector", "") or "",
        side=p.side.value, strategy=p.strategy, kind=p.kind.value,
        timeframe=p.timeframe.value, entry=p.entry, stop=p.stop, targets=p.targets,
        reward_risk=p.reward_risk, confidence=p.confidence, score=p.score,
        suggested_qty=p.suggested_qty, dollar_risk=p.dollar_risk, notional=p.notional,
        rationale=p.rationale[:400], explanation=p.explanation,
        # the hold goes with it: a play rebuilt from this row keeps the window its time stop runs on
        evidence={**(p.evidence or {}), "expected_hold": [float(p.expected_hold_typical or 0.0),
                                                          float(p.expected_hold_max or 0.0)]},
        tags=p.tags, noise=list(p.noise), confirmations=int(p.confirmations),
        probability=float(getattr(p, "probability", 0.5) or 0.0),
        status=p.status.value if hasattr(p.status, "value") else str(p.status),
    )


def _keeping_refusal(row: Optional[PlayLog], new: PlayLog) -> PlayLog:
    """``new``, the row a play is written again as, with the refusal Autopilot noted on the row it replaces
    (note_refusal): a scan that finds the setup again writes it from the board's play, which doesn't carry it."""
    note = (row.evidence or {}).get(REFUSAL) if row is not None else None
    if note and REFUSAL not in (new.evidence or {}):
        new.evidence = {**(new.evidence or {}), REFUSAL: note}
    return new


def _close(s, trade_id: str, exit_price: float, exit_reason: str, now: dt.datetime, commission: float = 0.0,
           exit_qty: Optional[float] = None, decision_price: Optional[float] = None,
           submitted_at: Optional[dt.datetime] = None, broker_order_id: str = ""):
    """Repository.close_trade's booking, in the session ``s`` - reduce_trade's too, when the part it was given is the
    whole position. Returns (the record, whether this call closed it)."""
    # the record is taken from OPEN first, in one conditional write: a second close of it - one running at the same
    # time, or one retried after the first went through - changes no row and books nothing. The write also locks the
    # record (SQLite: the database) until this commits, so nothing read below can change meanwhile
    claimed = s.execute(update(Trade).where(Trade.id == trade_id, Trade.status == "OPEN").values(status="CLOSED")
                        .execution_options(synchronize_session=False)).rowcount == 1
    t = s.get(Trade, trade_id)
    if not claimed:
        return (trade_to_dict(t) if t else None), False
    qty = float(exit_qty if exit_qty is not None else t.quantity)
    sign = 1.0 if t.side == "LONG" else -1.0
    gross = (exit_price - float(t.entry_price)) * qty * sign
    fees = float(t.fees or 0.0) + commission
    # what the part taken off earlier made joins the final P/L; R and % are on the shares entered with
    banked = float(getattr(t, "banked_pl", 0.0) or 0.0)
    # every fee comes off: the entry's, and this exit's - a part taken off earlier banked what it made after
    # its own already, so the fees on record less those are the ones still to come off
    parts_paid = float(s.execute(select(func.coalesce(func.sum(Fill.commission), 0.0))
                                 .where(Fill.trade_id == trade_id, Fill.leg == "EXIT")).scalar() or 0.0)
    pl = gross - commission - (float(t.fees or 0.0) - parts_paid) + banked
    basis_qty = float(getattr(t, "initial_quantity", None) or qty) if exit_qty is None else qty
    t.exit_price = exit_price
    t.exit_time = now
    t.exit_reason = exit_reason
    _fold_fill(t, exit_price, now)
    if submitted_at is not None and (t.broker or SIMULATOR) != SIMULATOR:
        t.exit_submitted_at = _naive(submitted_at)
        t.exit_latency_s = _took(submitted_at, now)
    if decision_price and float(decision_price) > 0:
        t.exit_decision_price = float(decision_price)
        t.exit_slippage_bps = round((float(decision_price) - exit_price) * sign / float(decision_price) * 1e4, 2)
    t.fees = fees
    _score(t, pl, basis_qty)
    # day-trade if entry and exit fall on the same NY session - or a part was sold on the session it was entered,
    # which made it one then, whenever the rest went
    if t.entry_time:
        entered = clock.session_date(_as_utc(t.entry_time))
        t.is_day_trade = (entered == clock.session_date(_as_utc(now))
                          or trade_id in _sold_on_entry_session(s, {trade_id: entered}))
    t.status = "CLOSED"             # written above already; this keeps the object (and what is returned) in step
    s.add(Fill(trade_id=trade_id, broker_order_id=broker_order_id or "", ts=now,
               side=("SHORT" if t.side == "LONG" else "LONG"), leg="EXIT", quantity=qty, price=exit_price,
               commission=commission))
    return trade_to_dict(t), True


def _fold_fill(t: Trade, price: float, at: dt.datetime) -> None:
    """An exit fill into the record's excursions - the whole position's or a part's. The fill is a price the trade
    saw, and the passes that mark the excursions (the exit manager) never see it: a stop that fills through the worst
    point marked so far would leave the MAE short of the trade's own loss, a target that fills past the best point its
    MFE and high-water mark short."""
    sign = 1.0 if t.side == "LONG" else -1.0
    entry = float(t.entry_price)
    gain, loss = (price - entry) * sign, (entry - price) * sign
    if gain > float(t.mfe or 0.0) + 1e-6:
        t.mfe, t.mfe_at = gain, at
    if loss > float(t.mae or 0.0) + 1e-6:
        t.mae = loss
    if t.hwm_price is None or (price - float(t.hwm_price)) * sign > 1e-6:
        t.hwm_price = price


def _at_venue(venue: Optional[str]) -> tuple:
    """The where-clause for the trades booked at ``venue`` - none, for every venue, when it isn't given. A record
    with no broker on it (null or empty) is the simulator's, the column's default, as the engine reads it."""
    if not venue:
        return ()
    if venue == SIMULATOR:
        return (or_(Trade.broker == venue, Trade.broker.is_(None), Trade.broker == ""),)
    return (Trade.broker == venue,)


def _sold_on_entry_session(s, entered: Dict[str, dt.date]) -> set:
    """Of the trades in ``entered`` (id -> the New York session it was entered on), the ones with a part sold on
    that session - an exit fill on record then: a buy and a sale the same day, which makes a day trade."""
    if not entered:
        return set()
    rows = s.execute(select(Fill.trade_id, Fill.ts).where(Fill.leg == "EXIT", Fill.trade_id.in_(list(entered)))).all()
    return {tid for tid, ts in rows if ts and clock.session_date(_as_utc(ts)) == entered[tid]}


def _score(t: Trade, pl: float, basis_qty: float) -> None:
    """A closed trade's P/L, and the % and R that follow from it, on ``basis_qty`` shares (the ones entered with)."""
    t.realized_pl = pl
    basis = float(t.entry_price) * basis_qty
    t.realized_pl_pct = (pl / basis * 100.0) if basis else None
    # R is measured against the ORIGINAL stop, not a trailed one
    ref_stop = getattr(t, "initial_stop_price", None) or t.stop_price
    risk_ps = abs(float(t.entry_price) - float(ref_stop)) if ref_stop else 0.0
    t.r_multiple = (pl / (risk_ps * basis_qty)) if risk_ps and basis_qty else None


def _shortfall(decision: Optional[Dict[str, Any]], fill_price: float, side: str) -> Dict[str, Any]:
    """Harris's implementation shortfall at the entry: the fill against the mid of the quote the
    decision was made on, in basis points (+ = paid) - only when that quote was live (``live``, as the
    entry check saw the data): a delayed quote is minutes old, and a fill against it measures how far the
    price moved since, not what the fill cost. The quote's spread is kept either way."""
    mid = float((decision or {}).get("mid") or 0.0)
    if mid <= 0:
        return {}
    spread = (decision or {}).get("spread_bps")
    out: Dict[str, Any] = {"spread_bps": float(spread) if spread is not None else None}
    if (decision or {}).get("live"):
        sign = 1.0 if side == "LONG" else -1.0
        out.update(decision_price=mid, entry_slippage_bps=round((float(fill_price) - mid) * sign / mid * 1e4, 2))
    return out


def sim_to_dict(t: SimTradeLog) -> Dict[str, Any]:
    return {"id": t.id, "run_id": t.run_id, "ran_at": t.ran_at.isoformat() if t.ran_at else None,
            "strategy": t.strategy, "symbol": t.symbol, "side": t.side, "timeframe": t.timeframe,
            "entered_at": t.entered_at.isoformat() if t.entered_at else None,
            "exited_at": t.exited_at.isoformat() if t.exited_at else None,
            "entry": _f(t.entry_price), "exit": _f(t.exit_price), "r": _f(t.r), "exit_reason": t.exit_reason,
            "noise": list(t.noise or []), "confirmed": bool(t.confirmed), "mfe_r": _f(t.mfe_r),
            "scaled": bool(t.scaled), "held_out": bool(t.held_out), "features": t.features or {},
            "schema": int(t.feature_schema or 0), "drift_r": _f(t.drift_r), "cost_r": _f(t.cost_r)}


def shadow_to_dict(t: ShadowTradeLog) -> Dict[str, Any]:
    return {"play_id": t.play_id, "session_date": t.session_date.isoformat() if t.session_date else None,
            "symbol": t.symbol, "strategy": t.strategy, "side": t.side, "timeframe": t.timeframe,
            "seen_at": t.seen_at.isoformat() if t.seen_at else None, "passed_checks": bool(t.passed_checks),
            "filled": bool(t.filled), "entered_at": t.entered_at.isoformat() if t.entered_at else None,
            "exited_at": t.exited_at.isoformat() if t.exited_at else None, "entry": _f(t.entry_price), "exit": _f(t.exit_price),
            "r": _f(t.r), "mfe_r": _f(t.mfe_r), "exit_reason": t.exit_reason, "noise": list(t.noise or []),
            "confirmations": int(t.confirmations or 1), "features": t.features or {}, "schema": int(t.feature_schema or 0)}


def _naive_iso(stamp: Any) -> Optional[dt.datetime]:
    """An ISO string (any zone) as the naive UTC the database keeps."""
    if not stamp:
        return None
    try:
        d = dt.datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    return _naive(d)


#: the in-app simulator's venue: it fills an order the moment it gets it, so it keeps no fill time - its
#: seconds would say nothing about how long an order takes, and pull every typical figure toward zero
SIMULATOR = "paper"

def _took(sent: Optional[dt.datetime], filled: dt.datetime) -> Optional[float]:
    """Seconds from an order going out to its fill - None when the send time isn't known."""
    if sent is None:
        return None
    gone = _naive(sent)
    return round(max(0.0, (filled - gone).total_seconds()), 3) if gone else None


def _naive(d: Optional[dt.datetime]) -> Optional[dt.datetime]:
    if d is None:
        return None
    return d.astimezone(dt.timezone.utc).replace(tzinfo=None) if d.tzinfo else d


def _as_utc(d: dt.datetime) -> dt.datetime:
    return d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d
