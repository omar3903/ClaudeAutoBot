"""Turn a Play's entry/stop geometry into a share count the account can bear.

Fixed-fractional risk: risk at most ``max_risk_per_trade_pct`` of equity
between entry and the protective stop, then clip by a notional cap per trade,
a cap on everything held in the same stock, a slice of the stock's usual daily
volume, and available buying power.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..core.models import Account, Play
from ..util import clock


@dataclass
class SizingResult:
    qty: int
    risk_per_share: float
    dollar_risk: float
    notional: float
    caps_hit: List[str]
    #: the limit the share count stopped at, and each limit's value - the dollars it allows (``usd``) and the shares
    #: they buy (``shares``) - so a trade's record can say which one sized it and by how much: the risk budget (the
    #: risk per trade after half-Kelly, mid-day and the size factor), the room under the open-risk ceiling
    #: (open_risk), the per-position % (per_position), the cap on one stock (per_symbol), the slice of its daily
    #: volume (volume), buying power (buying_power) and what is left of the trading capital (capital_room)
    decided_by: str = ""
    limits: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def size_play(play: Play, account: Account, cfg, open_risk_used: float = 0.0,
              symbol_notional: float = 0.0, risk_pct: Optional[float] = None,
              risk_why: Optional[str] = None, size_factor: float = 1.0) -> SizingResult:
    """``symbol_notional``: dollars already in this stock - shares held at the
    broker and entry orders still working. ``risk_pct``: the strategy's half-Kelly
    risk per trade (see quant/sizing.py), which can only lower the configured one.
    ``risk_why``: what set that risk, when it isn't the record's half-Kelly - the
    practice size of a strategy the replay hasn't proven - as caps_hit names it. ``size_factor``: the
    operator's position size factor (0-5), which scales the risk budget before any cap below - so the
    open-risk ceiling, the per-stock and liquidity caps and buying power still hold at 5x."""
    entry = play.entry
    stop = play.stop
    rps = abs(entry - stop)
    caps: List[str] = []
    if rps <= 0 or entry <= 0 or account.equity <= 0:
        _apply(play, 0, rps, 0.0, 0.0)
        return SizingResult(0, rps, 0.0, 0.0, ["degenerate geometry"])

    equity = account.equity
    pct = float(cfg.max_risk_per_trade_pct)
    if risk_pct is not None and risk_pct < pct:
        pct = max(0.0, float(risk_pct))
        caps.append(risk_why or "half-Kelly from the strategy's record")
    factor = time_of_day_factor(play, cfg)
    if factor < 1.0:
        pct *= factor
        caps.append("Mid-day: a smaller size (Aziz)")
    if size_factor != 1.0:
        pct *= max(0.0, float(size_factor))
        caps.append(f"position size factor {float(size_factor):g}x")
    risk_budget = equity * pct / 100.0
    limits = {"risk_budget": _limit(risk_budget, rps, pct=round(pct, 4), size_factor=float(size_factor))}
    decided = "risk_budget"                   # until a cap below cuts the share count

    # respect the portfolio-wide open-risk ceiling
    room = equity * cfg.max_open_risk_pct / 100.0 - open_risk_used
    limits["open_risk"] = _limit(room, rps, pct=float(cfg.max_open_risk_pct), used=round(float(open_risk_used), 2))
    if room < risk_budget:
        risk_budget = max(0.0, room)
        caps.append("portfolio open-risk ceiling")
        decided = "open_risk"

    by_risk = qty = math.floor(risk_budget / rps)

    # notional cap per name
    max_notional = equity * cfg.max_position_pct_of_equity / 100.0
    limits["per_position"] = _limit(max_notional, entry, pct=float(cfg.max_position_pct_of_equity))
    if qty * entry > max_notional:
        qty = math.floor(max_notional / entry)
        caps.append("max position % of equity")
        decided = "per_position"

    # everything in one stock together, however it got there
    symbol_cap = getattr(cfg, "max_symbol_pct_of_equity", None)
    if symbol_cap is not None:
        room_in_symbol = max(0.0, equity * symbol_cap / 100.0 - symbol_notional)
        limits["per_symbol"] = _limit(room_in_symbol, entry, pct=float(symbol_cap),
                                      held=round(float(symbol_notional), 2))
        if qty * entry > room_in_symbol:
            qty = math.floor(room_in_symbol / entry)
            caps.append("max exposure per stock")
            decided = "per_symbol"

    # a slice of the stock's usual daily volume, so the order - and its stop - can fill without
    # moving a thin stock (scanner/evaluator.py median_volume); unknown volume means no cap
    liquidity = liquidity_cap(play, cfg)
    if liquidity is not None:
        limits["volume"] = {"pct": float(cfg.max_adv_pct), "shares": liquidity}
        if qty > liquidity:
            qty = liquidity
            caps.append(liquidity_label(cfg))
            decided = "volume"

    # buying power
    bp = account.buying_power if account.buying_power else equity
    limits["buying_power"] = _limit(bp, entry)
    if qty * entry > bp:
        qty = math.floor(bp / entry)
        caps.append("buying power")
        decided = "buying_power"

    # what's left of a trading-capital limit (see TradingEngine.sizing_account)
    room = (getattr(account, "raw", None) or {}).get("capital_room")
    if room is not None:
        limits["capital_room"] = _limit(float(room), entry)
        if qty * entry > room:
            qty = math.floor(max(0.0, float(room)) / entry)
            caps.append("trading capital")
            decided = "capital_room"

    # round lot
    lot = max(1, int(getattr(cfg, "round_lot", 1)))
    qty = (qty // lot) * lot
    qty = max(0, qty)

    dollar_risk = qty * rps
    notional = qty * entry
    _apply(play, qty, rps, dollar_risk, notional)
    if qty == 0 and by_risk < lot:            # a cap that took it to nothing is named above instead
        caps.append("risk budget too small for one share")
    return SizingResult(qty, round(rps, 4), round(dollar_risk, 2), round(notional, 2), caps, decided, limits)


def _limit(usd: float, per_share: float, **more: Any) -> Dict[str, Any]:
    """A limit for SizingResult.limits: the dollars it allows - of risk for the risk budget and the open-risk
    ceiling, of cost for the others - and the whole shares they buy at ``per_share``."""
    usd = max(0.0, float(usd))
    return {**more, "usd": round(usd, 2), "shares": math.floor(usd / per_share)}


def liquidity_cap(play: Play, cfg) -> Optional[int]:
    """The most shares one order may take: ``cfg.max_adv_pct`` percent of the stock's median daily
    volume over its last 20 completed sessions, which the scan writes into the play's evidence.
    None when the cap is off or the volume isn't known (a play saved before the scan wrote it)."""
    pct = float(getattr(cfg, "max_adv_pct", 0.0) or 0.0)
    adv = (play.evidence or {}).get("adv_shares")
    if pct <= 0 or not adv or float(adv) <= 0:
        return None
    return math.floor(float(adv) * pct / 100.0)


def liquidity_label(cfg) -> str:
    """The cap's name in caps_hit, which the order preview lists under "Size limited by"."""
    return f"liquidity: {float(cfg.max_adv_pct):g}% of its usual daily volume"


def time_of_day_factor(play: Play, cfg, now=None) -> float:
    """How much of the usual risk a play may take right now. Aziz (ch. 7, "Trading Based on the
    Time of Day"): Mid-day (12-3 pm ET) is the most dangerous part of the session - thin, choppy,
    strange moves stop you out - "I lower my share size and keep my stops tight". A day trade
    sized then risks ``cfg.midday_size_pct`` percent of the usual; a swing trade is untouched."""
    if not play.is_day_trade:
        return 1.0
    pct = float(getattr(cfg, "midday_size_pct", 100.0) or 100.0)
    if pct >= 100.0 or clock.time_of_day(now) != "MIDDAY":
        return 1.0
    return max(0.0, pct) / 100.0


def _apply(play: Play, qty: int, rps: float, dollar_risk: float, notional: float) -> None:
    play.suggested_qty = int(qty)
    play.risk_per_share = round(rps, 4)
    play.dollar_risk = round(dollar_risk, 2)
    play.notional = round(notional, 2)
