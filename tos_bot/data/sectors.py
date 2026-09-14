"""The 11 sectors the app filters and reports on, and how IBKR's own
classification maps onto them.

IBKR classifies stocks Bloomberg-style: a broad *industry* ("Consumer,
Non-cyclical") and a narrower *category* ("Pharmaceuticals"). Most industries
map straight onto a sector; a few categories override that, so drug makers land
in Healthcare and REITs in Real Estate.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

SECTORS = (
    "Technology", "Communication Services", "Consumer Discretionary", "Consumer Staples",
    "Healthcare", "Financials", "Industrials", "Energy", "Utilities", "Materials", "Real Estate",
)

# other common spellings -> SECTORS (the dashboard and config.yaml accept these)
_ALIASES = {
    "information technology": "Technology",
    "consumer cyclical": "Consumer Discretionary",
    "consumer defensive": "Consumer Staples",
    "health care": "Healthcare",
    "financial services": "Financials",
    "basic materials": "Materials",
    "telecommunication services": "Communication Services",
}

_IBKR_INDUSTRY = {
    "Technology": "Technology",
    "Communications": "Communication Services",
    "Consumer, Cyclical": "Consumer Discretionary",
    "Consumer, Non-cyclical": "Consumer Staples",
    "Financial": "Financials",
    "Industrial": "Industrials",
    "Diversified": "Industrials",
    "Energy": "Energy",
    "Utilities": "Utilities",
    "Basic Materials": "Materials",
}
_IBKR_CATEGORY = {
    "Pharmaceuticals": "Healthcare",
    "Biotechnology": "Healthcare",
    "Healthcare-Products": "Healthcare",
    "Healthcare-Services": "Healthcare",
    "REITS": "Real Estate",
    "Real Estate": "Real Estate",
    "Commercial Services": "Industrials",
}


def canonical_sector(name: Optional[str]) -> str:
    n = (name or "").strip()
    for s in SECTORS:
        if s.lower() == n.lower():
            return s
    return _ALIASES.get(n.lower(), n)


def sector_from_ibkr(industry: Optional[str], category: Optional[str]) -> str:
    """One of SECTORS, or "" when IBKR gives no classification."""
    return _IBKR_CATEGORY.get(category or "") or _IBKR_INDUSTRY.get(industry or "", "")


def clean_sector_list(values: Optional[Iterable[str]]) -> List[str]:
    """Normalise a sector selection. Every sector (or none) means no filter: []."""
    picked = {canonical_sector(v) for v in (values or [])}
    chosen = [s for s in SECTORS if s in picked]
    return [] if len(chosen) == len(SECTORS) else chosen


def sector_allowed(sector: Optional[str], allowed: Optional[Iterable[str]]) -> bool:
    """No filter allows everything; with one, an unknown sector is excluded."""
    return not allowed or canonical_sector(sector) in allowed
