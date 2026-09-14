"""Turn a Play's entry/stop geometry into a share count the account can bear.

Fixed-fractional risk: risk at most ``max_risk_per_trade_pct`` of equity
between entry and the protective stop, then clip by a notional cap and by
available buying power.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List

from ..core.models import Account, Play


@dataclass
class SizingResult:
    qty: int
    risk_per_share: float
    dollar_risk: float
    notional: float
    caps_hit: List[str]

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def size_play(play: Play, account: Account, cfg, open_risk_used: float = 0.0) -> SizingResult:
    entry = play.entry
    stop = play.stop
    rps = abs(entry - stop)
    caps: List[str] = []
    if rps <= 0 or entry <= 0 or account.equity <= 0:
        _apply(play, 0, rps, 0.0, 0.0)
        return SizingResult(0, rps, 0.0, 0.0, ["degenerate geometry"])

    equity = account.equity
    risk_budget = equity * cfg.max_risk_per_trade_pct / 100.0

    # respect the portfolio-wide open-risk ceiling
    room = equity * cfg.max_open_risk_pct / 100.0 - open_risk_used
    if room < risk_budget:
        risk_budget = max(0.0, room)
        caps.append("portfolio open-risk ceiling")

    qty = math.floor(risk_budget / rps)

    # notional cap per name
    max_notional = equity * cfg.max_position_pct_of_equity / 100.0
    if qty * entry > max_notional:
        qty = math.floor(max_notional / entry)
        caps.append("max position % of equity")

    # buying power
    bp = account.buying_power if account.buying_power else equity
    if qty * entry > bp:
        qty = math.floor(bp / entry)
        caps.append("buying power")

    # what's left of a trading-capital limit (see TradingEngine.sizing_account)
    room = (getattr(account, "raw", None) or {}).get("capital_room")
    if room is not None and qty * entry > room:
        qty = math.floor(max(0.0, float(room)) / entry)
        caps.append("trading capital")

    # round lot
    lot = max(1, int(getattr(cfg, "round_lot", 1)))
    qty = (qty // lot) * lot
    qty = max(0, qty)

    dollar_risk = qty * rps
    notional = qty * entry
    _apply(play, qty, rps, dollar_risk, notional)
    if qty == 0:
        caps.append("risk budget too small for one share")
    return SizingResult(qty, round(rps, 4), round(dollar_risk, 2), round(notional, 2), caps)


def _apply(play: Play, qty: int, rps: float, dollar_risk: float, notional: float) -> None:
    play.suggested_qty = int(qty)
    play.risk_per_share = round(rps, 4)
    play.dollar_risk = round(dollar_risk, 2)
    play.notional = round(notional, 2)
