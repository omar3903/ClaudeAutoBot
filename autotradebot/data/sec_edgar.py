"""Company financials from SEC EDGAR: the XBRL facts filed with every 10-K.

Free, official and keyless. One ``companyfacts`` document per company holds
every figure it has filed; the latest annual values are read off it and the
result is cached on disk for a week, since statements only change with new
filings. Companies that don't file annual US-GAAP figures (most foreign ADRs,
which use IFRS) aren't covered, so their valuation setups sit out. SEC asks for
at most 10 requests a second (see sec_http.py).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import math
import time
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple

from .fundamentals import Financials, FundamentalsProvider
from .sec_http import SEC

log = logging.getLogger(__name__)

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
_FACTS_TTL_S = 7 * 86400
_TICKERS_TTL_S = 86400
_ANNUAL_FORMS = ("10-K", "20-F", "40-F")
_YEARS = 5
_STALE_AFTER_DAYS = 550

Series = Dict[dt.date, float]

# statement line -> XBRL tags, best first
_FLOWS = {
    "revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
                "RevenuesNetOfInterestExpense"),
    "operating_income": ("OperatingIncomeLoss",),
    "pretax_income": ("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                      "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLoss"
                      "FromEquityMethodInvestments"),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "dep_amort": ("DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
                  "DepreciationAmortizationAndAccretionNet", "Depreciation"),
    "interest_expense": ("InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt"),
    "income_tax": ("IncomeTaxExpenseBenefit",),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
              "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets"),
    "deferred_tax": ("DeferredIncomeTaxExpenseBenefit",),
    "sbc": ("ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"),
    "eps": ("EarningsPerShareDiluted", "EarningsPerShareBasic"),
}
_BALANCES = {
    "cash": ("CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "Cash"),
    "short_term_investments": ("ShortTermInvestments", "MarketableSecuritiesCurrent",
                               "AvailableForSaleSecuritiesDebtSecuritiesCurrent"),
    "long_term_debt_noncurrent": ("LongTermDebtNoncurrent",),
    "long_term_debt_total": ("LongTermDebt", "LongTermDebtAndCapitalLeaseObligations"),
    "debt_current": ("LongTermDebtCurrent", "DebtCurrent"),
    "short_term_borrowings": ("CommercialPaper", "ShortTermBorrowings"),
    "finance_leases": ("FinanceLeaseLiabilityNoncurrent", "FinanceLeaseLiability"),
    "minority_interest": ("MinorityInterest",),
    "preferred_stock": ("PreferredStockValue",),
}


def sec_ticker(symbol: str) -> str:
    """IBKR's "BRK B" is SEC's "BRK-B"."""
    return symbol.replace(" ", "-").upper()


def _date(value: Optional[str]) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(value) if value else None
    except ValueError:
        return None


def annual_series(gaap: Mapping[str, dict], tags: Sequence[str], balance: bool) -> Series:
    """Annual values by fiscal period end. The best tag with a value for a date
    wins, and within a tag the most recently filed figure (a restatement) wins."""
    out: Series = {}
    for tag in tags:
        latest: Dict[dt.date, Tuple[str, float]] = {}
        for rows in gaap.get(tag, {}).get("units", {}).values():
            for row in rows:
                if not str(row.get("form", "")).startswith(_ANNUAL_FORMS):
                    continue
                end = _date(row.get("end"))
                if end is None:
                    continue
                if not balance:
                    start = _date(row.get("start"))
                    if start is None or not 350 <= (end - start).days <= 380:
                        continue
                filed = str(row.get("filed", ""))
                if end not in latest or filed > latest[end][0]:
                    latest[end] = (filed, float(row["val"]))
        for end, (_, value) in latest.items():
            out.setdefault(end, value)
    return out


def _at(series: Series, when: dt.date, max_age_days: int = 400) -> float:
    dates = [d for d in series if d <= when and (when - d).days <= max_age_days]
    return series[max(dates)] if dates else math.nan


def _nz(x: float) -> float:
    return 0.0 if math.isnan(x) else x


def _sum_known(*xs: float) -> float:
    known = [x for x in xs if not math.isnan(x)]
    return sum(known) if known else math.nan


def _shares_outstanding(facts: Mapping[str, dict]) -> float:
    """Every share class's count from the latest cover page, added up."""
    rows = facts.get("dei", {}).get("EntityCommonStockSharesOutstanding", {}).get("units", {}).get("shares", [])
    if not rows:
        return math.nan
    last_filing = max(str(r.get("accn", "")) for r in rows)
    return float(sum(r["val"] for r in rows if str(r.get("accn", "")) == last_filing))


def financials_from_facts(symbol: str, facts: Mapping[str, dict], today: dt.date) -> Optional[Financials]:
    gaap = facts.get("us-gaap") or {}
    flow = {k: annual_series(gaap, tags, balance=False) for k, tags in _FLOWS.items()}
    years = sorted(set(flow["revenue"]) | set(flow["net_income"]) | set(flow["operating_income"]))[-_YEARS:]
    if not years or (today - years[-1]).days > _STALE_AFTER_DAYS:
        return None
    fy_end = years[-1]

    def by_year(key: str):
        return [flow[key].get(y, math.nan) for y in years]

    op_income, pretax, interest = by_year("operating_income"), by_year("pretax_income"), by_year("interest_expense")
    dep_amort = by_year("dep_amort")
    ebit = [oi if not math.isnan(oi) else _sum_known(pt, abs(ie)) if not math.isnan(pt) and not math.isnan(ie) else math.nan
            for oi, pt, ie in zip(op_income, pretax, interest)]
    ebitda = [e + da if not (math.isnan(e) or math.isnan(da)) else math.nan for e, da in zip(ebit, dep_amort)]

    bal = {k: _at(annual_series(gaap, tags, balance=True), fy_end) for k, tags in _BALANCES.items()}
    long_term = (bal["long_term_debt_noncurrent"] + _nz(bal["debt_current"])
                 if not math.isnan(bal["long_term_debt_noncurrent"])
                 else _sum_known(bal["long_term_debt_total"]) if not math.isnan(bal["long_term_debt_total"])
                 else bal["debt_current"])
    tax, last_pretax = flow["income_tax"].get(fy_end, math.nan), flow["pretax_income"].get(fy_end, math.nan)

    return Financials(
        symbol=symbol,
        shares_out=_shares_outstanding(facts),
        total_debt=_sum_known(long_term, bal["short_term_borrowings"]),
        cash_and_st_investments=_sum_known(bal["cash"], bal["short_term_investments"]),
        minority_interest=_nz(bal["minority_interest"]),
        preferred_equity=_nz(bal["preferred_stock"]),
        capital_leases=_nz(bal["finance_leases"]),
        revenue=by_year("revenue"),
        ebitda=ebitda,
        ebit=ebit,
        net_income=by_year("net_income"),
        dep_amort=dep_amort,
        interest_expense=[abs(x) if not math.isnan(x) else math.nan for x in interest],
        capex=[-abs(x) if not math.isnan(x) else math.nan for x in by_year("capex")],
        deferred_tax=by_year("deferred_tax"),
        sbc=by_year("sbc"),
        ttm_eps=flow["eps"].get(fy_end, math.nan),        # latest fiscal year
        tax_rate=(max(0.0, min(0.45, tax / last_pretax))
                  if not math.isnan(tax) and last_pretax and last_pretax > 0 else math.nan),
    )


class SecEdgarFundamentals(FundamentalsProvider):
    def __init__(self, cache_dir: Path, fetch_json: Optional[Callable[[str], dict]] = None) -> None:
        self.cache_dir = cache_dir
        self._fetch_json = fetch_json or SEC.json
        self._ciks: Dict[str, int] = {}
        self._ciks_at = 0.0

    def get(self, symbol: str) -> Optional[Financials]:
        hit = self._read_cache(symbol)
        if hit is not None:
            return hit[0]
        cik = self._cik(symbol)
        if cik is None:
            self._write_cache(symbol, None)
            return None
        try:
            facts = self._fetch_json(_FACTS_URL.format(cik=cik)).get("facts", {})
        except Exception as e:  # noqa: BLE001
            log.debug("SEC facts for %s failed: %s", symbol, e)
            return None
        fin = financials_from_facts(symbol, facts, dt.date.today())
        self._write_cache(symbol, fin)
        return fin

    def _cik(self, symbol: str) -> Optional[int]:
        if not self._ciks or time.time() - self._ciks_at > _TICKERS_TTL_S:
            try:
                rows = self._fetch_json(_TICKERS_URL).values()
                self._ciks = {str(r["ticker"]).upper(): int(r["cik_str"]) for r in rows}
                self._ciks_at = time.time()
            except Exception as e:  # noqa: BLE001
                log.warning("SEC ticker list download failed: %s", e)
        return self._ciks.get(sec_ticker(symbol))

    def _cache_path(self, symbol: str) -> Path:
        return self.cache_dir / f"{sec_ticker(symbol)}.json"

    def _read_cache(self, symbol: str) -> Optional[Tuple[Optional[Financials]]]:
        try:
            doc = json.loads(self._cache_path(symbol).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if time.time() - float(doc.get("fetched_at", 0)) > _FACTS_TTL_S:
            return None
        data = doc.get("financials")
        return (Financials(**data) if data else None,)

    def _write_cache(self, symbol: str, fin: Optional[Financials]) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        doc = {"fetched_at": time.time(), "financials": dataclasses.asdict(fin) if fin else None}
        self._cache_path(symbol).write_text(json.dumps(doc), encoding="utf-8")
