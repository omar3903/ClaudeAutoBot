"""Enterprise value <-> equity value bridge.

Pignataro Ch. 8 (pp. 280-284):

    Enterprise Value = Market Capitalisation
                     + Net Debt                         (short + long debt - cash)
                     + Non-controlling Interests
                     + Preferred Securities
                     + Capital Lease Obligations
                     + Other Non-operating Liabilities

Cash is subtracted because it is not an operating asset. EV is "a way of
determining the implied value of a company's core operating assets."
"""

from __future__ import annotations

import math


def enterprise_value(
    market_cap: float,
    net_debt: float,
    minority_interest: float = 0.0,
    preferred_equity: float = 0.0,
    capital_leases: float = 0.0,
    other_non_operating: float = 0.0,
) -> float:
    parts = [market_cap, net_debt, minority_interest, preferred_equity,
             capital_leases, other_non_operating]
    return float(sum(0.0 if (p is None or math.isnan(p)) else p for p in parts))


def equity_value_from_ev(
    ev: float,
    net_debt: float,
    minority_interest: float = 0.0,
    preferred_equity: float = 0.0,
    capital_leases: float = 0.0,
    other_non_operating: float = 0.0,
) -> float:
    """Reverse the bridge: strip the non-equity claims back out of EV."""
    for x in (net_debt, minority_interest, preferred_equity, capital_leases,
              other_non_operating):
        if x is not None and not math.isnan(x):
            ev -= x
    return float(ev)


def implied_share_price(equity_value: float, shares_out: float) -> float:
    if not shares_out or math.isnan(shares_out) or shares_out <= 0:
        return math.nan
    return float(equity_value / shares_out)
