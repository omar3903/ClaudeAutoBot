"""Trading capital: how much of the account the bot may use.

Set per venue, in the account's own currency, and never more than the account
holds. Position sizing then sees an account of that size, with only what's left
of it available for new positions. The PDT rule and the live equity floor keep
looking at the real account.

While the filters let the bot trade both kinds, it is split between day trades
and swing trades: day trades may hold up to ``day_pct`` of it at once and swing
trades (pair trades among them) the rest. With only one kind switched on, that
kind gets all of it. A trade that doesn't fit what's left of its share is made
smaller; risk per trade is still measured against the whole trading capital.

Account amounts are in US dollars (US stocks are sized in dollars); an account
kept in another currency converts with ``Account.usd_per_base``.
"""

from __future__ import annotations

import dataclasses
import math
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

from ..core.models import Account

DAY, SWING = "INTRADAY", "SWING"
DEFAULT_DAY_PCT = 75.0

#: what "the whole account" means for the positions held together: its buying power, margin included (MODE_MARGIN),
#: or only the money in it (MODE_CASH: longs and shorts together never hold more than the account's value, so
#: nothing is borrowed). A set amount is money the account holds, so it never uses margin either
MODE_MARGIN, MODE_CASH = "margin", "cash"
MODES = (MODE_MARGIN, MODE_CASH)

_CURRENCY_SIGN = {"USD": "$", "CAD": "CA$", "EUR": "€", "GBP": "£", "AUD": "A$", "HKD": "HK$"}


def money(amount: float, currency: str = "USD") -> str:
    sign = _CURRENCY_SIGN.get(currency)
    return f"{sign}{amount:,.0f}" if sign else f"{amount:,.0f} {currency}"


def in_account_currency(acc: Account) -> Dict[str, Any]:
    """Equity, cash and buying power in the account's own currency."""
    rate = float(acc.usd_per_base or 0.0)
    reported = (acc.raw or {}).get("base") or {}

    def amount(key: str, usd: float) -> float:
        if key in reported:
            return float(reported[key])
        return usd / rate if rate else 0.0

    return {"currency": acc.base_currency or "USD", "usd_per_base": round(rate, 6),
            "equity": round(amount("equity", acc.equity), 2),
            "cash": round(amount("cash", acc.cash), 2),
            "buying_power": round(amount("buying_power", acc.buying_power), 2)}


def invested_usd(acc: Optional[Account], trades: Iterable[Mapping[str, Any]]) -> float:
    """What the bot has in these positions, at the broker's marks."""
    marks = {p.symbol: p.market_price for p in (acc.positions if acc else [])}
    return sum(abs(float(t.get("quantity") or 0.0)) * float(marks.get(t["symbol"]) or t.get("entry_price") or 0.0)
               for t in trades)


def kind_of(timeframe: Any) -> str:
    """Day trade or swing trade: anything not closed the same day is a swing trade."""
    return DAY if str(getattr(timeframe, "value", timeframe) or "").upper() == DAY else SWING


def share_of(kind: str, day_pct: float) -> float:
    return max(0.0, min(1.0, (day_pct if kind == DAY else 100.0 - day_pct) / 100.0))


def capacity_usd(acc: Account, limit: Optional[float], invested: float, mode: str = MODE_MARGIN) -> float:
    """The most the bot's positions may hold together, in US dollars. A set amount (``limit``, in the account's
    currency): that much, never more than the account's value. Cash only: the account's value. The whole account
    with margin: what the positions hold now plus IBKR's buying power - the broker's own figure for what's left
    after them, with the margin it allows already in it; with no buying power reported, the account's value."""
    if limit:
        return min(limit * float(acc.usd_per_base or 0.0), acc.equity)
    power = float(acc.buying_power or 0.0)
    if mode == MODE_CASH or power <= 0:
        return acc.equity
    return max(0.0, invested) + power


def invested_by_kind(acc: Optional[Account], trades: Sequence[Mapping[str, Any]]) -> Dict[str, float]:
    return {kind: invested_usd(acc, [t for t in trades if kind_of(t.get("timeframe")) == kind]) for kind in (DAY, SWING)}


def sizing_account(acc: Account, limit: Optional[float], invested: float, *, share: float = 1.0,
                   invested_in_kind: float = 0.0, mode: str = MODE_MARGIN) -> Account:
    """The account as position sizing sees it: ``limit`` (account currency) is a set amount, None the whole account
    - with margin or cash only, as ``mode`` says. Risk per trade and the per-position limits are measured against
    the set amount or the account's value, never against margin; new positions only get what's left of the most the
    positions may hold (capacity_usd), and with ``share`` under 1 only that part of it, less what that kind holds."""
    base = min(limit * float(acc.usd_per_base or 0.0), acc.equity) if limit else acc.equity
    cap = capacity_usd(acc, limit, invested, mode)
    room = max(0.0, cap - invested)
    if share < 1.0:
        room = min(room, max(0.0, cap * share - invested_in_kind))
    return dataclasses.replace(acc, equity=round(base, 2), cash=round(min(acc.cash, base), 2),
                               buying_power=round(min(acc.buying_power, cap), 2),
                               raw={**(acc.raw or {}), "capital_room": round(room, 2), "capital_share": share})


def state(acc: Account, venue: str, label: str, limit: Optional[float], invested: float,
          day_pct: float = DEFAULT_DAY_PCT, by_kind: Optional[Mapping[str, float]] = None,
          mode: str = MODE_MARGIN) -> Dict[str, Any]:
    worth = in_account_currency(acc)
    rate, value = worth["usd_per_base"], worth["equity"]
    if limit:
        effective = min(limit, value)
    elif mode == MODE_CASH or not rate:
        effective = value
    else:                                   # with margin: what the positions hold plus the buying power left
        effective = capacity_usd(acc, None, invested, mode) / rate
    held = invested / rate if rate else 0.0

    def part(kind: str) -> Dict[str, float]:
        size = effective * share_of(kind, day_pct)
        used = float((by_kind or {}).get(kind, 0.0)) / rate if rate else 0.0
        return {"pct": round(100 * share_of(kind, day_pct), 1), "limit": round(size, 2), "invested": round(used, 2),
                "available": round(max(0.0, min(size - used, effective - held)), 2),
                # held beyond its share - the slider moved, or the prices did. Nothing is sold for it; the
                # kind just takes no new entries until it is back under
                "over": round(max(0.0, used - size), 2)}
    return {"venue": venue, "venue_label": label, "currency": worth["currency"], "usd_per_base": rate,
            "limit": limit, "mode": "amount" if limit else mode, "buying_power": worth["buying_power"],
            "account_value": value, "effective": round(effective, 2),
            "invested": round(held, 2), "available": round(max(0.0, effective - held), 2),
            "clipped": bool(limit and limit > value), "fx_missing": not rate,
            "split": {"day_pct": day_pct, "day": part(DAY), "swing": part(SWING)}}


def parse_day_pct(value: Any) -> float:
    try:
        pct = round(float(value), 1)
    except (TypeError, ValueError):
        raise ValueError("Enter the day-trade share as a percentage, like 75.") from None
    if not math.isfinite(pct) or not 0.0 <= pct <= 100.0:
        raise ValueError("The day-trade share has to be between 0% and 100%.")
    return pct


#: the position size factor's range: 0 sizes every position at nothing, 5 at five times the usual
SIZE_FACTOR_MAX = 5.0


def parse_size_factor(value: Any) -> float:
    try:
        factor = round(float(value), 2)
    except (TypeError, ValueError):
        raise ValueError("Enter the position size factor as a number, like 1.5.") from None
    if not math.isfinite(factor) or not 0.0 <= factor <= SIZE_FACTOR_MAX:
        raise ValueError(f"The position size factor has to be between 0 and {SIZE_FACTOR_MAX:g}.")
    return factor


def parse_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    if mode not in MODES:
        raise ValueError("Choose the whole account with margin, or cash only.")
    return mode


def parse_position_pct(value: Any) -> float:
    """The most one position may hold, as % of the account's value: 1-100."""
    try:
        pct = round(float(value), 1)
    except (TypeError, ValueError):
        raise ValueError("Enter the most one position may hold as a percentage, like 15.") from None
    if not math.isfinite(pct) or not 1.0 <= pct <= 100.0:
        raise ValueError("The most one position may hold has to be between 1% and 100% of the account.")
    return pct


def parse_amount(amount: Any) -> float:
    try:
        value = round(float(amount), 2)
    except (TypeError, ValueError):
        raise ValueError("Enter an amount, like 250000.") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Trading capital must be more than zero.")
    return value


def check_fits(value: float, acc: Account, label: str) -> Dict[str, Any]:
    """The account in its own currency, if ``value`` fits in it; else ValueError."""
    worth = in_account_currency(acc)
    currency = worth["currency"]
    if not worth["usd_per_base"]:
        raise ValueError(f"No {currency}->USD exchange rate yet, so trades can't be sized. Try again in a minute.")
    if value > worth["equity"] + 0.005:
        raise ValueError(f"That's more than the account holds: {label} is worth "
                         f"{money(worth['equity'], currency)}. Trading capital can't be more than that.")
    return worth
