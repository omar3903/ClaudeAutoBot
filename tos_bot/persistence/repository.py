"""All database reads/writes the engine and API need, in one place."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select

from ..core.models import Account, Play
from ..util import clock
from .db import session_scope
from .models_orm import (
    AccountSnapshot,
    Fill,
    OrderAudit,
    PlayLog,
    ScanRun,
    TokenAudit,
    Trade,
)

log = logging.getLogger(__name__)


def _f(x) -> Optional[float]:
    return None if x is None else float(x)


def trade_to_dict(t: Trade) -> Dict[str, Any]:
    return {
        "id": t.id, "play_id": t.play_id, "symbol": t.symbol, "side": t.side,
        "strategy": t.strategy, "kind": t.kind, "timeframe": t.timeframe, "broker": t.broker,
        "status": t.status, "quantity": _f(t.quantity),
        "entry_price": _f(t.entry_price),
        "entry_time": t.entry_time.isoformat() if t.entry_time else None,
        "stop_price": _f(t.stop_price), "target_price": _f(t.target_price),
        "exit_price": _f(t.exit_price),
        "exit_time": t.exit_time.isoformat() if t.exit_time else None,
        "exit_reason": t.exit_reason, "fees": _f(t.fees),
        "realized_pl": _f(t.realized_pl), "realized_pl_pct": _f(t.realized_pl_pct),
        "r_multiple": _f(t.r_multiple), "is_day_trade": bool(t.is_day_trade),
        "session_date": t.session_date.isoformat() if t.session_date else None,
        "notes": t.notes,
    }


def play_to_dict(p: PlayLog) -> Dict[str, Any]:
    return {
        "id": p.id, "scan_run_id": p.scan_run_id,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "symbol": p.symbol, "side": p.side, "strategy": p.strategy, "kind": p.kind,
        "timeframe": p.timeframe, "entry": _f(p.entry), "stop": _f(p.stop),
        "targets": p.targets, "reward_risk": _f(p.reward_risk),
        "confidence": _f(p.confidence), "score": _f(p.score),
        "suggested_qty": p.suggested_qty, "dollar_risk": _f(p.dollar_risk),
        "notional": _f(p.notional), "rationale": p.rationale,
        "explanation": p.explanation, "evidence": p.evidence, "tags": p.tags,
        "status": p.status,
    }


class Repository:
    # -------------------------------------------------------------- #
    #  Scans + plays                                                #
    # -------------------------------------------------------------- #
    def record_scan(self, result, keep_rejected: bool = True, top_n: int = 60) -> None:
        with session_scope() as s:
            s.merge(ScanRun(
                id=result.run_id, started_at=_naive(result.started_at),
                finished_at=_naive(result.finished_at),
                universe_size=result.universe_size, scanned=result.scanned,
                prefiltered=result.prefiltered, n_plays=len(result.plays),
                shortlist={"symbols": result.shortlist}, n_errors=len(result.errors),
                elapsed_s=result.elapsed_s,
            ))
            plays = result.plays if keep_rejected else result.plays[:top_n]
            for p in plays:
                s.merge(_play_row(p))

    def record_play(self, play: Play) -> None:
        with session_scope() as s:
            s.merge(_play_row(play))

    def set_play_status(self, play_id: str, status: str, decided_by: str = "") -> None:
        with session_scope() as s:
            row = s.get(PlayLog, play_id)
            if row:
                row.status = status
                row.decided_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
                row.decided_by = decided_by

    def get_play(self, play_id: str) -> Optional[Dict[str, Any]]:
        with session_scope() as s:
            row = s.get(PlayLog, play_id)
            return play_to_dict(row) if row else None

    # -------------------------------------------------------------- #
    #  Trades                                                       #
    # -------------------------------------------------------------- #
    def open_trade(
        self, play: Play, fill_price: float, fill_qty: float, broker: str,
        broker_order_id: str = "", commission: float = 0.0,
    ) -> str:
        tid = f"trd_{play.id.split('_', 1)[-1]}"
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        with session_scope() as s:
            s.add(Trade(
                id=tid, play_id=play.id, symbol=play.symbol, side=play.side.value,
                strategy=play.strategy, kind=play.kind.value, timeframe=play.timeframe.value,
                broker=broker, status="OPEN", quantity=fill_qty, entry_price=fill_price,
                entry_time=now, stop_price=play.stop,
                target_price=play.primary_target, fees=commission,
                session_date=clock.session_date(),
                is_day_trade=(play.timeframe.value == "INTRADAY"),
            ))
            s.add(Fill(trade_id=tid, broker_order_id=broker_order_id, ts=now,
                       side=play.side.value, leg="ENTRY", quantity=fill_qty,
                       price=fill_price, commission=commission))
            row = s.get(PlayLog, play.id)
            if row:
                row.status = "FILLED"
        log.info("trade opened %s %s x%s @ %.4f", tid, play.symbol, fill_qty, fill_price)
        return tid

    def add_fill(self, trade_id: str, side: str, leg: str, qty: float, price: float,
                 commission: float = 0.0, broker_order_id: str = "") -> None:
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        with session_scope() as s:
            s.add(Fill(trade_id=trade_id, broker_order_id=broker_order_id, ts=now,
                       side=side, leg=leg, quantity=qty, price=price, commission=commission))

    def close_trade(
        self, trade_id: str, exit_price: float, exit_reason: str = "manual",
        commission: float = 0.0, exit_qty: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        with session_scope() as s:
            t = s.get(Trade, trade_id)
            if t is None or t.status == "CLOSED":
                return trade_to_dict(t) if t else None
            qty = float(exit_qty if exit_qty is not None else t.quantity)
            sign = 1.0 if t.side == "LONG" else -1.0
            gross = (exit_price - float(t.entry_price)) * qty * sign
            fees = float(t.fees or 0.0) + commission
            pl = gross - commission
            t.exit_price = exit_price
            t.exit_time = now
            t.exit_reason = exit_reason
            t.fees = fees
            t.realized_pl = pl
            basis = float(t.entry_price) * qty
            t.realized_pl_pct = (pl / basis * 100.0) if basis else None
            risk_ps = abs(float(t.entry_price) - float(t.stop_price)) if t.stop_price else 0.0
            t.r_multiple = (pl / (risk_ps * qty)) if risk_ps and qty else None
            # day-trade if entry and exit fall on the same NY session
            if t.entry_time:
                t.is_day_trade = clock.session_date(_as_utc(t.entry_time)) == clock.session_date(_as_utc(now))
            t.status = "CLOSED"
            s.add(Fill(trade_id=trade_id, ts=now, side=("SHORT" if t.side == "LONG" else "LONG"),
                       leg="EXIT", quantity=qty, price=exit_price, commission=commission))
            out = trade_to_dict(t)
        log.info("trade closed %s: P/L %.2f (%s)", trade_id, out["realized_pl"], exit_reason)
        return out

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

    # -------------------------------------------------------------- #
    #  PDT counter                                                  #
    # -------------------------------------------------------------- #
    def count_day_trades(self, lookback_sessions: int = 5) -> int:
        window = list(clock.last_n_sessions(clock.session_date(), lookback_sessions))
        lo = window[0]
        with session_scope() as s:
            closed = s.execute(
                select(func.count()).select_from(Trade)
                .where(Trade.is_day_trade.is_(True), Trade.status == "CLOSED",
                       Trade.session_date >= lo)
            ).scalar_one()
            open_today = s.execute(
                select(func.count()).select_from(Trade)
                .where(Trade.status == "OPEN", Trade.timeframe == "INTRADAY",
                       Trade.session_date == clock.session_date())
            ).scalar_one()
        return int(closed) + int(open_today)

    # -------------------------------------------------------------- #
    #  P/L analytics                                                #
    # -------------------------------------------------------------- #
    def pnl_summary(self) -> Dict[str, Any]:
        today = clock.session_date()
        wk = clock.last_n_sessions(today, 5)[0]
        with session_scope() as s:
            closed = s.execute(select(Trade).where(Trade.status == "CLOSED")).scalars().all()
        pls = [float(t.realized_pl) for t in closed if t.realized_pl is not None]
        wins = [x for x in pls if x > 0]
        losses = [x for x in pls if x < 0]
        day = sum(float(t.realized_pl or 0) for t in closed
                  if t.session_date and t.session_date >= today)
        week = sum(float(t.realized_pl or 0) for t in closed
                   if t.session_date and t.session_date >= wk)
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
        }

    def equity_curve(self, limit: int = 500) -> List[Dict[str, Any]]:
        with session_scope() as s:
            rows = s.execute(
                select(Trade).where(Trade.status == "CLOSED", Trade.exit_time.is_not(None))
                .order_by(Trade.exit_time.asc()).limit(limit)
            ).scalars().all()
        cum = 0.0
        out = []
        for t in rows:
            cum += float(t.realized_pl or 0.0)
            out.append({"ts": t.exit_time.isoformat(), "symbol": t.symbol,
                        "pl": round(float(t.realized_pl or 0.0), 2), "cumulative": round(cum, 2)})
        return out

    # -------------------------------------------------------------- #
    #  Snapshots + audits                                           #
    # -------------------------------------------------------------- #
    def snapshot_account(self, account: Account, broker: str, realized_day: float = 0.0) -> None:
        with session_scope() as s:
            s.add(AccountSnapshot(
                broker=broker, equity=account.equity, cash=account.cash,
                buying_power=account.buying_power,
                day_trades_5d=self.count_day_trades(5),
                open_positions=len([p for p in account.positions if abs(p.quantity) > 1e-9]),
                unrealized_pl=sum(p.unrealized_pl for p in account.positions),
                realized_pl_day=realized_day,
            ))

    def record_order_audit(self, action: str, request: dict, response: dict, ok: bool,
                           broker: str, play_id: str = "", trade_id: str = "",
                           message: str = "") -> None:
        with session_scope() as s:
            s.add(OrderAudit(action=action, request=request, response=response, ok=ok,
                             broker=broker, play_id=play_id or None, trade_id=trade_id or None,
                             message=message[:400]))

    def record_token_event(self, broker: str, event: str, age_days: Optional[float] = None,
                           expires_at: Optional[dt.datetime] = None, detail: str = "") -> None:
        with session_scope() as s:
            s.add(TokenAudit(broker=broker, event=event, refresh_token_age_days=age_days,
                             refresh_token_expires_at=_naive(expires_at), detail=detail[:400]))

    def token_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        with session_scope() as s:
            rows = s.execute(select(TokenAudit).order_by(TokenAudit.ts.desc()).limit(limit)).scalars().all()
            return [{"ts": r.ts.isoformat(), "broker": r.broker, "event": r.event,
                     "age_days": r.refresh_token_age_days,
                     "expires_at": r.refresh_token_expires_at.isoformat() if r.refresh_token_expires_at else None,
                     "detail": r.detail} for r in rows]


# --------------------------------------------------------------------------- #
def _play_row(p: Play) -> PlayLog:
    return PlayLog(
        id=p.id, scan_run_id=p.scan_run_id, created_at=_naive(p.created_at),
        symbol=p.symbol, side=p.side.value, strategy=p.strategy, kind=p.kind.value,
        timeframe=p.timeframe.value, entry=p.entry, stop=p.stop, targets=p.targets,
        reward_risk=p.reward_risk, confidence=p.confidence, score=p.score,
        suggested_qty=p.suggested_qty, dollar_risk=p.dollar_risk, notional=p.notional,
        rationale=p.rationale[:400], explanation=p.explanation, evidence=p.evidence,
        tags=p.tags, status=p.status.value if hasattr(p.status, "value") else str(p.status),
    )


def _naive(d: Optional[dt.datetime]) -> Optional[dt.datetime]:
    if d is None:
        return None
    return d.astimezone(dt.timezone.utc).replace(tzinfo=None) if d.tzinfo else d


def _as_utc(d: dt.datetime) -> dt.datetime:
    return d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d
