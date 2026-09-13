"""Fundamental data: financial statements + a peer group.

`Financials` is the neutral shape the valuation engine consumes. The default
provider is Yahoo Finance via ``yfinance`` (free, occasionally patchy - every
field is optional and the valuation code degrades gracefully). A paid provider
(Financial Modeling Prep / Alpha Vantage) can be dropped in behind the same
:class:`FundamentalsProvider` interface later.
"""

from __future__ import annotations

import abc
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..config import PROJECT_ROOT
from ..util.ratelimit import RateLimiter

log = logging.getLogger(__name__)

_CACHE_DIR = PROJECT_ROOT / "data" / "cache"


# --------------------------------------------------------------------------- #
#  Neutral shape                                                             #
# --------------------------------------------------------------------------- #
@dataclass
class Financials:
    symbol: str
    as_of: float = field(default_factory=time.time)

    # market
    price: float = math.nan
    shares_out: float = math.nan            # diluted, current
    market_cap: float = math.nan
    beta: float = math.nan
    sector: str = ""
    industry: str = ""
    currency: str = "USD"

    # capital structure (most recent)
    total_debt: float = math.nan
    cash_and_st_investments: float = math.nan
    minority_interest: float = 0.0
    preferred_equity: float = 0.0
    capital_leases: float = 0.0

    # income statement - annual history, oldest first (up to ~4y)
    revenue: List[float] = field(default_factory=list)
    ebitda: List[float] = field(default_factory=list)
    ebit: List[float] = field(default_factory=list)
    net_income: List[float] = field(default_factory=list)
    dep_amort: List[float] = field(default_factory=list)
    interest_expense: List[float] = field(default_factory=list)

    # cash-flow history, oldest first
    capex: List[float] = field(default_factory=list)          # negative = outflow
    change_in_wc: List[float] = field(default_factory=list)
    deferred_tax: List[float] = field(default_factory=list)
    sbc: List[float] = field(default_factory=list)

    # trailing-twelve-month scalars (preferred when present)
    ttm_revenue: float = math.nan
    ttm_ebitda: float = math.nan
    ttm_ebit: float = math.nan
    ttm_net_income: float = math.nan
    ttm_eps: float = math.nan
    ttm_fcf: float = math.nan

    # convenience
    dividend_yield: float = math.nan
    tax_rate: float = math.nan

    # ---- helpers ----------------------------------------------------- #
    @property
    def net_debt(self) -> float:
        td = 0.0 if math.isnan(self.total_debt) else self.total_debt
        cash = 0.0 if math.isnan(self.cash_and_st_investments) else self.cash_and_st_investments
        return td - cash

    def last(self, series: List[float]) -> float:
        for v in reversed(series):
            if v is not None and not (isinstance(v, float) and math.isnan(v)):
                return float(v)
        return math.nan

    def revenue_cagr(self, years: int = 3) -> float:
        s = [v for v in self.revenue if v and not math.isnan(v)]
        if len(s) < 2:
            return math.nan
        s = s[-(years + 1):]
        n = len(s) - 1
        if s[0] <= 0 or s[-1] <= 0 or n <= 0:
            return math.nan
        return (s[-1] / s[0]) ** (1.0 / n) - 1.0

    def has_min_data(self) -> bool:
        return (
            not math.isnan(self.price)
            and not math.isnan(self.shares_out)
            and (not math.isnan(self.ttm_ebitda) or bool(self.ebitda))
        )


# --------------------------------------------------------------------------- #
#  Provider interface                                                        #
# --------------------------------------------------------------------------- #
class FundamentalsProvider(abc.ABC):
    name = "base"

    @abc.abstractmethod
    def get(self, symbol: str) -> Financials: ...

    @abc.abstractmethod
    def peers(self, symbol: str, limit: int = 8) -> List[str]: ...


# --------------------------------------------------------------------------- #
#  yfinance implementation                                                   #
# --------------------------------------------------------------------------- #
def _row(df, *names) -> List[float]:
    """Pull a statement row by any of several possible labels, oldest-first."""
    if df is None or getattr(df, "empty", True):
        return []
    for n in names:
        if n in df.index:
            vals = list(df.loc[n].values)[::-1]  # yfinance is newest-first
            return [float(v) if v == v else math.nan for v in vals]
    return []


class YFinanceFundamentals(FundamentalsProvider):
    name = "yfinance"
    _PEER_FALLBACK: Dict[str, List[str]] = {
        "Technology": ["AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "ADBE", "AMD", "QCOM", "TXN"],
        "Consumer Cyclical": ["AMZN", "TSLA", "HD", "MCD", "NKE", "SBUX", "LOW", "BKNG", "TJX", "ORLY"],
        "Communication Services": ["GOOGL", "META", "NFLX", "TMUS", "DIS", "CMCSA", "VZ", "T", "CHTR", "EA"],
        "Healthcare": ["UNH", "JNJ", "LLY", "ABBV", "MRK", "PFE", "TMO", "ABT", "DHR", "AMGN"],
        "Financial Services": ["JPM", "BAC", "WFC", "MS", "GS", "SCHW", "BLK", "C", "AXP", "SPGI"],
        "Industrials": ["CAT", "HON", "UNP", "GE", "BA", "RTX", "DE", "LMT", "UPS", "ETN"],
        "Consumer Defensive": ["WMT", "PG", "KO", "PEP", "COST", "MDLZ", "CL", "KMB", "GIS", "KHC"],
        "Energy": ["XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "OXY", "VLO", "WMB"],
    }

    def __init__(self, throttle: float = 0.4) -> None:
        try:
            import yfinance  # noqa: F401
            self._ok = True
        except Exception:  # noqa: BLE001
            self._ok = False
            log.warning("yfinance missing - fundamentals disabled")
        # paces request starts across the scanner's threads (never sleeps under a lock)
        self._limiter = RateLimiter(throttle)
        self._cache: Dict[str, Financials] = {}

    # -------------------------------------------------------------- #
    def get(self, symbol: str) -> Financials:
        # fundamentals barely move intraday; cache hard to spare the yfinance quota
        if symbol in self._cache and time.time() - self._cache[symbol].as_of < 12 * 3600:
            return self._cache[symbol]
        fin = Financials(symbol=symbol)
        if not self._ok:
            return fin
        import yfinance as yf

        self._nap()
        t = yf.Ticker(symbol)
        info: Dict = {}
        for getter in ("get_info", "info"):
            try:
                info = getattr(t, getter)() if callable(getattr(t, getter)) else getattr(t, getter)
                if info:
                    break
            except Exception:  # noqa: BLE001
                info = {}

        g = info.get
        fin.price = _f(g("currentPrice") or g("regularMarketPrice") or g("previousClose"))
        fin.shares_out = _f(g("sharesOutstanding") or g("impliedSharesOutstanding"))
        fin.market_cap = _f(g("marketCap"))
        fin.beta = _f(g("beta"))
        fin.sector = g("sector") or ""
        fin.industry = g("industry") or ""
        fin.currency = g("currency") or "USD"
        fin.total_debt = _f(g("totalDebt"))
        fin.cash_and_st_investments = _f(g("totalCash"))
        fin.ttm_ebitda = _f(g("ebitda"))
        fin.ttm_net_income = _f(g("netIncomeToCommon"))
        fin.ttm_eps = _f(g("trailingEps"))
        fin.ttm_revenue = _f(g("totalRevenue"))
        fin.ttm_fcf = _f(g("freeCashflow"))
        fin.dividend_yield = _f(g("dividendYield"))

        try:
            inc = t.income_stmt
            bs = t.balance_sheet
            cf = t.cashflow
        except Exception:  # noqa: BLE001
            inc = bs = cf = None

        fin.revenue = _row(inc, "Total Revenue", "Operating Revenue")
        fin.ebit = _row(inc, "EBIT", "Operating Income")
        fin.net_income = _row(inc, "Net Income", "Net Income Common Stockholders")
        fin.dep_amort = _row(inc, "Reconciled Depreciation") or _row(cf, "Depreciation And Amortization",
                                                                    "Depreciation Amortization Depletion")
        fin.interest_expense = _row(inc, "Interest Expense", "Interest Expense Non Operating")
        ebitda_hist = _row(inc, "EBITDA", "Normalized EBITDA")
        if not ebitda_hist and fin.ebit and fin.dep_amort:
            ebitda_hist = [a + b for a, b in zip(fin.ebit, fin.dep_amort)]
        fin.ebitda = ebitda_hist
        fin.capex = _row(cf, "Capital Expenditure", "Purchase Of PPE")
        fin.change_in_wc = _row(cf, "Change In Working Capital")
        fin.deferred_tax = _row(cf, "Deferred Income Tax", "Deferred Tax")
        fin.sbc = _row(cf, "Stock Based Compensation")

        if bs is not None and not getattr(bs, "empty", True):
            mi = _row(bs, "Minority Interest")
            pf = _row(bs, "Preferred Stock", "Preferred Securities Outside Stock Equity")
            cl = _row(bs, "Capital Lease Obligations", "Finance Lease Liabilities",
                      "Long Term Capital Lease Obligation")
            fin.minority_interest = mi[-1] if mi else 0.0
            fin.preferred_equity = pf[-1] if pf else 0.0
            fin.capital_leases = cl[-1] if cl else 0.0
            if math.isnan(fin.total_debt):
                td = _row(bs, "Total Debt")
                fin.total_debt = td[-1] if td else math.nan

        # effective tax rate from history
        pretax = _row(inc, "Pretax Income")
        taxprov = _row(inc, "Tax Provision")
        if pretax and taxprov and pretax[-1]:
            fin.tax_rate = max(0.0, min(0.45, taxprov[-1] / pretax[-1]))

        if math.isnan(fin.market_cap) and not math.isnan(fin.price) and not math.isnan(fin.shares_out):
            fin.market_cap = fin.price * fin.shares_out

        self._cache[symbol] = fin
        return fin

    def peers(self, symbol: str, limit: int = 8) -> List[str]:
        fin = self.get(symbol)
        pool = self._PEER_FALLBACK.get(fin.sector, [])
        peers = [p for p in pool if p != symbol][:limit]
        if len(peers) < 3:
            # last resort: a broad megacap set
            peers = [p for p in
                     ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "JPM", "XOM", "UNH", "PG"]
                     if p != symbol][:limit]
        return peers

    # -------------------------------------------------------------- #
    def _nap(self) -> None:
        self._limiter.wait()


def _f(v) -> float:
    try:
        if v is None:
            return math.nan
        return float(v)
    except (TypeError, ValueError):
        return math.nan
