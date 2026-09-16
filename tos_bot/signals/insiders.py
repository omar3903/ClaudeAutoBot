"""How unusual a stock's insider trading is.

Insiders sell for many reasons - taxes, a house, spreading their wealth - but buy
for one: they think the stock will rise. A wave of buying stands out when a senior
insider is in it, when it is large in dollars and against what they already held,
when several insiders buy within a few weeks, and when this company's insiders
rarely buy at all. Selling has to clear a much higher bar.

Left out: trades under a 10b5-1 plan (scheduled months ahead) and purchases the
filing says were made in a private placement or offering (arranged with the
company). Several insiders paying exactly the same price on the same day usually
means such a deal too, so that wave gets no credit for being a cluster and never
counts as unusual.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass
from statistics import median
from typing import Any, Dict, List, Mapping, Optional, Sequence

import numpy as np

from .form4 import BUY, ROLE_LABELS, ROLES, SELL, InsiderTrade

ROLE_WEIGHT = {"ceo_cfo": 1.0, "officer": 0.75, "director": 0.65, "ten_percent_owner": 0.45, "other": 0.3}
#: what each part of the score is worth; together they make 1
WEIGHTS = {"role": 0.25, "size": 0.25, "stake": 0.15, "cluster": 0.20, "rare": 0.15}


@dataclass(frozen=True)
class InsiderSettings:
    window_days: int = 30              # trades this close together count as one wave
    history_days: int = 365            # how far back "rarely trade" looks
    buy_floor: float = 25_000.0        # a wave of buying smaller than this is a token
    buy_full: float = 1_000_000.0      # a wave this size gets the whole size score
    sell_floor: float = 250_000.0
    sell_full: float = 10_000_000.0
    unusual_buying: float = 0.55       # the score from which a wave counts as unusual
    unusual_selling: float = 0.65


@dataclass
class InsiderSignal:
    symbol: str
    direction: str                     # buying | selling
    score: float                       # 0..1, how unusual
    unusual: bool
    insiders: int                      # distinct people in the wave
    trades: int
    value: float
    first_date: dt.date
    last_date: dt.date
    top_role: str
    reasons: List[str]
    shares: float = 0.0

    @property
    def avg_price(self) -> float:
        """What the insiders paid (or got) per share, on average."""
        return self.value / self.shares if self.shares else 0.0

    def as_dict(self) -> Dict[str, object]:
        row = asdict(self)
        row.update(first_date=self.first_date.isoformat(), last_date=self.last_date.isoformat(),
                   avg_price=round(self.avg_price, 4))
        return row


#: SEC's deadline for a Form 4: two business days after the trade
FORM4_DEADLINE_DAYS = 2


def filing_delays(rows: Sequence[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """How long after their trades insiders filed, in business days: rows as the store's
    ``insider_filings`` returns them. None without any."""
    lags = [int(np.busday_count(r["trade_date"], r["filed"])) for r in rows
            if r.get("filed") and r.get("trade_date") and r["filed"] >= r["trade_date"]]
    if not lags:
        return None
    on_time = sum(1 for d in lags if d <= FORM4_DEADLINE_DAYS)
    return {"trades": len(lags), "median_days": median(lags), "on_time": round(on_time / len(lags), 3),
            "late": len(lags) - on_time, "max_days": max(lags)}


def insider_signals(symbol: str, trades: Sequence[InsiderTrade], today: dt.date,
                    settings: InsiderSettings = InsiderSettings(), history_known: bool = True) -> List[InsiderSignal]:
    """A buying and a selling signal for ``symbol`` - each only when its insiders traded
    that way in the last ``window_days``. ``history_known`` says whether the stock's
    past year of filings has been read; until it has, "its insiders rarely trade" is
    neither claimed nor fully scored."""
    wave_start = today - dt.timedelta(days=settings.window_days)
    history_start = today - dt.timedelta(days=settings.history_days)
    counted = [t for t in trades if t.symbol == symbol and not t.planned and not t.offering
               and history_start <= t.trade_date <= today]
    signals: List[InsiderSignal] = []
    for code in (BUY, SELL):
        same_way = [t for t in counted if t.code == code]
        wave = [t for t in same_way if t.trade_date >= wave_start]
        if wave:
            before = [t for t in same_way if t.trade_date < wave_start]
            signals.append(_signal(symbol, code, wave, before, settings, history_known))
    return signals


def _person(t: InsiderTrade) -> object:
    return t.owner_cik or t.owner_name


def _signal(symbol: str, code: str, wave: List[InsiderTrade], before: List[InsiderTrade],
            s: InsiderSettings, history_known: bool) -> InsiderSignal:
    buying = code == BUY
    people = {_person(t) for t in wave}
    value = sum(t.value for t in wave)
    top = min((t.role for t in wave), key=ROLES.index)
    new_holding = any(t.new_holding for t in wave)
    stake = min(1.0, max((abs(t.holding_change) for t in wave if not t.new_holding), default=0.0))
    if new_holding:
        stake = max(stake, 0.5)
    deal_like = (buying and len(people) >= 2 and len({t.trade_date for t in wave}) == 1
                 and len({round(t.price, 2) for t in wave}) == 1)
    floor, full = (s.buy_floor, s.buy_full) if buying else (s.sell_floor, s.sell_full)
    repeat = people & {_person(t) for t in before}
    parts = {
        "role": ROLE_WEIGHT[top],
        "size": _between(value, floor, full) * (0.5 if deal_like else 1.0),
        "stake": stake,
        "cluster": 0.0 if deal_like else min(1.0, (len(people) - 1) / 2),
        "rare": (1.0 if not before else 0.5 if not repeat else 0.0) if history_known else 0.5,
    }
    score = round(sum(WEIGHTS[k] * v for k, v in parts.items()), 3)
    first, last = min(t.trade_date for t in wave), max(t.trade_date for t in wave)
    reasons = _reasons(wave, buying, top, value, stake, new_holding, len(people), deal_like,
                       parts["rare"] if history_known else None, s)
    return InsiderSignal(
        symbol=symbol, direction="buying" if buying else "selling", score=score,
        unusual=(score >= (s.unusual_buying if buying else s.unusual_selling) and value >= floor
                 and not deal_like),
        insiders=len(people), trades=len(wave), value=round(value, 2), first_date=first, last_date=last,
        top_role=top, reasons=reasons, shares=sum(t.shares for t in wave))


def _between(value: float, floor: float, full: float) -> float:
    """0 at ``floor`` or less, 1 at ``full`` or more, on a log scale between."""
    if value <= floor:
        return 0.0
    if value >= full:
        return 1.0
    return math.log(value / floor) / math.log(full / floor)


def _reasons(wave: List[InsiderTrade], buying: bool, top: str, value: float, stake: float, new_holding: bool,
             people: int, deal_like: bool, rare, s: InsiderSettings) -> List[str]:
    verb = "bought" if buying else "sold"
    senior = next(t for t in wave if t.role == top)
    who = senior.title or ROLE_LABELS[top]
    days = (max(t.trade_date for t in wave) - min(t.trade_date for t in wave)).days + 1
    when = "on the same day" if days == 1 else f"within {days} days"
    lines = [f"{people} insiders {verb} {money(value)} in the open market {when}, led by the {who}"
             if people > 1 else f"The {who} {verb} {money(value)} in the open market"]
    if deal_like:
        lines.append(f"Every insider paid exactly ${wave[0].price:,.2f} on the same day - more likely a private "
                     "placement or offering than open-market buying")
    if buying and stake >= 1.0:
        lines.append("At least doubled a holding")
    elif buying and new_holding and stake <= 0.5:
        lines.append("Started a new holding, or added to one held another way")
    elif buying and stake >= 0.25:
        lines.append(f"Grew a holding by {stake:.0%}")
    elif not buying and stake >= 1.0:
        lines.append("Sold an entire holding")
    elif not buying and stake >= 0.25:
        lines.append(f"Sold {stake:.0%} of a holding")
    period = "year" if s.history_days >= 360 else f"{s.history_days} days"
    if rare == 1.0:
        lines.append(f"No other open-market {'buying' if buying else 'selling'} by its insiders in the previous {period}")
    elif rare == 0.5:
        lines.append(f"None of these insiders {verb} in the previous {period}")
    return lines


def money(value: float) -> str:
    if value >= 1e9:
        return f"${value / 1e9:.1f}B"
    if value >= 1e6:
        return f"${value / 1e6:.1f}M"
    if value >= 1e3:
        return f"${value / 1e3:.0f}K"
    return f"${value:,.0f}"
