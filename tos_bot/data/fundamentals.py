"""Company financial statements in one neutral shape, and the provider interface.

:class:`Financials` is what the valuation engine consumes. Every field is
optional and the valuation code degrades gracefully when one is missing.
Statements come from a provider (:mod:`tos_bot.data.sec_edgar`); the market
figures - price, market cap and beta - are filled in by the scanner from the
candles it already has.
"""

from __future__ import annotations

import abc
import math
import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Financials:
    symbol: str
    as_of: float = field(default_factory=time.time)

    # market
    price: float = math.nan
    shares_out: float = math.nan
    market_cap: float = math.nan
    beta: float = math.nan

    # capital structure (most recent balance sheet)
    total_debt: float = math.nan
    cash_and_st_investments: float = math.nan
    minority_interest: float = 0.0
    preferred_equity: float = 0.0
    capital_leases: float = 0.0

    # annual income statement history, oldest first
    revenue: List[float] = field(default_factory=list)
    ebitda: List[float] = field(default_factory=list)
    ebit: List[float] = field(default_factory=list)
    net_income: List[float] = field(default_factory=list)
    dep_amort: List[float] = field(default_factory=list)
    interest_expense: List[float] = field(default_factory=list)

    # annual cash-flow history, oldest first
    capex: List[float] = field(default_factory=list)          # negative = outflow
    change_in_wc: List[float] = field(default_factory=list)
    deferred_tax: List[float] = field(default_factory=list)
    sbc: List[float] = field(default_factory=list)

    # trailing-twelve-month figures, preferred when present
    ttm_revenue: float = math.nan
    ttm_ebitda: float = math.nan
    ttm_ebit: float = math.nan
    ttm_net_income: float = math.nan
    ttm_eps: float = math.nan
    ttm_fcf: float = math.nan

    tax_rate: float = math.nan

    @property
    def net_debt(self) -> float:
        debt = 0.0 if math.isnan(self.total_debt) else self.total_debt
        cash = 0.0 if math.isnan(self.cash_and_st_investments) else self.cash_and_st_investments
        return debt - cash

    @staticmethod
    def last(series: List[float]) -> float:
        for v in reversed(series):
            if v is not None and not (isinstance(v, float) and math.isnan(v)):
                return float(v)
        return math.nan

    def revenue_cagr(self, years: int = 3) -> float:
        s = [v for v in self.revenue if v and not math.isnan(v)][-(years + 1):]
        n = len(s) - 1
        if n <= 0 or s[0] <= 0 or s[-1] <= 0:
            return math.nan
        return (s[-1] / s[0]) ** (1.0 / n) - 1.0

    def has_min_data(self) -> bool:
        return (not math.isnan(self.price) and not math.isnan(self.shares_out)
                and (not math.isnan(self.ttm_ebitda) or bool(self.ebitda)))


class FundamentalsProvider(abc.ABC):
    """Supplies statements; returns None when it has nothing usable for a symbol."""

    @abc.abstractmethod
    def get(self, symbol: str) -> Optional[Financials]:
        ...
