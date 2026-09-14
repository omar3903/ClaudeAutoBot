"""Trading capital: how much of the account the bot may use.

Set per venue, in the account's own currency, and never more than the account
holds. Position sizing then sees an account of that size, with only what's left
of it available for new positions. The PDT rule and the live equity floor keep
looking at the real account.

Account amounts are in US dollars (US stocks are sized in dollars); an account
kept in another currency converts with ``Account.usd_per_base``.
"""

from __future__ import annotations

import dataclasses
import math
from typing import Any, Dict, Iterable, Mapping, Optional

from ..core.models import Account

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


def sizing_account(acc: Account, limit: float, invested: float) -> Account:
    """The account shrunk to ``limit`` (account currency) for position sizing."""
    cap = min(limit * float(acc.usd_per_base or 0.0), acc.equity)
    room = max(0.0, cap - invested)
    return dataclasses.replace(acc, equity=round(cap, 2), cash=round(min(acc.cash, cap), 2),
                               buying_power=round(min(acc.buying_power, cap), 2),
                               raw={**(acc.raw or {}), "capital_room": round(room, 2)})


def state(acc: Account, venue: str, label: str, limit: Optional[float], invested: float) -> Dict[str, Any]:
    worth = in_account_currency(acc)
    rate, value = worth["usd_per_base"], worth["equity"]
    effective = min(limit, value) if limit else value
    held = invested / rate if rate else 0.0
    return {"venue": venue, "venue_label": label, "currency": worth["currency"], "usd_per_base": rate,
            "limit": limit, "account_value": value, "effective": round(effective, 2),
            "invested": round(held, 2), "available": round(max(0.0, effective - held), 2),
            "clipped": bool(limit and limit > value), "fx_missing": not rate}


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
