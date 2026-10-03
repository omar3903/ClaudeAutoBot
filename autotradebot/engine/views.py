"""Read-only pieces of the dashboard snapshot."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..brokers.venues import plan_venue
from ..core.models import Account
from ..data.market_data import MarketData
from .capital import in_account_currency
from .connections import Connections


def exit_rules(cfg: Any) -> Dict[str, Any]:
    return {"enabled": bool(cfg.enabled), "breakeven_at_r": cfg.breakeven_at_r,
            "trail_start_r": cfg.trail_start_r, "trail_lock_ratio": cfg.trail_lock_ratio,
            "flatten_intraday_before_close_min": cfg.flatten_intraday_before_close_min,
            "intraday_time_stop": bool(getattr(cfg, "intraday_time_stop", False)),
            "max_swing_hold_days": cfg.max_swing_hold_days,
            "scale_out_pct": float(getattr(cfg, "scale_out_pct", 0.0) or 0.0),
            "scale_out_lock_r": float(getattr(cfg, "scale_out_lock_r", 0.0) or 0.0)}


def data_feed(md: MarketData) -> Dict[str, Any]:
    return {"source": md.source_name, "connected": md.attached, "delayed": md.delayed, "reason": md.data_problem}


def account(acc: Account, cfg: Any, paper: bool) -> Dict[str, Any]:
    raw = acc.raw or {}
    return {"equity": round(acc.equity, 2), "cash": round(acc.cash, 2),
            "buying_power": round(acc.buying_power, 2), "is_cash_account": acc.is_cash_account,
            "base": in_account_currency(acc),
            "realized_pl_session": raw.get("realized_pl"), "start_equity": raw.get("start_equity"),
            "floor_enforced": not paper, "min_start_equity": cfg.min_start_equity,
            "paper_start_cash": cfg.paper_start_cash, "pdt_threshold": cfg.pdt_equity_threshold}


def positions(acc: Optional[Account], marks: Optional[Mapping[str, Tuple[float, str]]] = None) -> List[Dict[str, Any]]:
    """The positions the broker holds. ``marks``: the app's own fresher price per stock (the one the exit
    manager acts on), with when it's from - used over the broker's mark, which IBKR updates only every
    few minutes."""
    out = []
    for p in (acc.positions if acc else []):
        mark = (marks or {}).get(p.symbol)
        price, at = mark if mark else (p.market_price, None)
        unrealized = (price - p.avg_price) * p.quantity if mark else p.unrealized_pl
        out.append({"symbol": p.symbol, "qty": p.quantity, "avg_price": round(p.avg_price, 4),
                    "market_price": round(price, 4), "price_at": at, "unrealized_pl": round(unrealized, 2)})
    return out


def connection_pill(mode: str, paper_platform: str, conns: Connections) -> Dict[str, str]:
    """The header pill: where orders go, and whether that's healthy."""
    plan = plan_venue(mode, paper_platform)
    status = conns.session_status() or {}
    role = "live" if mode == "live" else "paper"
    if conns.connected:
        data = f"{status.get('market_data', 'live')} IBKR data"
        if not plan.trade:
            return {"label": "Simulator", "cls": "good", "detail": f"Built-in simulator · {data}"}
        return {"label": f"IBKR {role} ●", "cls": "good",
                "detail": f"{status.get('message', 'connected')} · port {status.get('port')} · {data}"}
    why = "; ".join(conns.blockers) or "IB Gateway isn't connected"
    if not plan.trade:
        return {"label": "Simulator · no prices", "cls": "warn", "action": "connections",
                "detail": f"The simulator fills on IBKR prices, so nothing trades until IB Gateway connects. {why}"}
    reconnecting = bool(status.get("reconnecting"))
    # orders stay pointed at the account while it's away (TradingEngine._bind): refused, never sent elsewhere
    return {"label": f"IBKR {role} {'↻' if reconnecting else '✕'}", "cls": "warn" if reconnecting else "bad",
            "detail": f"{why} · No order goes out until it's back - none go to the simulator in its place.",
            "action": "connections"}
