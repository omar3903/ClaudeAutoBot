"""Ticker -> sector, cheaply.

Order of resolution:
  1. a value already on the play's Financials (free - the valuation strategies
     fetch fundamentals anyway)
  2. a bundled static map of the liquid large-caps that pass the pre-filter
  3. a JSON cache on disk (``data/cache/sectors.json``) - sectors don't change
  4. a one-off yfinance ``.info`` lookup, written back to the cache

Only symbols that actually produce a play get looked up, so steady-state this
costs a handful of calls per cycle at most.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from ..config import PROJECT_ROOT

log = logging.getLogger(__name__)

_CACHE_FILE = PROJECT_ROOT / "data" / "cache" / "sectors.json"

# Bundled - keeps the common names instant and offline.
_STATIC: Dict[str, str] = {
    # Technology
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology", "AVGO": "Technology",
    "ORCL": "Technology", "CRM": "Technology", "ADBE": "Technology", "AMD": "Technology",
    "QCOM": "Technology", "TXN": "Technology", "INTC": "Technology", "AMAT": "Technology",
    "MU": "Technology", "LRCX": "Technology", "KLAC": "Technology", "ADI": "Technology",
    "SNPS": "Technology", "CDNS": "Technology", "PANW": "Technology", "CRWD": "Technology",
    "FTNT": "Technology", "ANSS": "Technology", "ASML": "Technology", "NXPI": "Technology",
    "ADSK": "Technology", "ROP": "Technology", "MSI": "Technology", "IBM": "Technology",
    "NOW": "Technology", "ACN": "Technology", "GFS": "Technology", "ZS": "Technology",
    "TEAM": "Technology", "MDB": "Technology", "DDOG": "Technology", "WDAY": "Technology",
    "CDW": "Technology", "ON": "Technology", "MCHP": "Technology", "SMCI": "Technology",
    "DELL": "Technology", "HPQ": "Technology", "GLW": "Technology",
    # Communication Services
    "GOOGL": "Communication Services", "GOOG": "Communication Services",
    "META": "Communication Services", "NFLX": "Communication Services",
    "TMUS": "Communication Services", "DIS": "Communication Services",
    "CMCSA": "Communication Services", "VZ": "Communication Services",
    "T": "Communication Services", "CHTR": "Communication Services",
    "EA": "Communication Services", "TTWO": "Communication Services",
    "WBD": "Communication Services", "OMC": "Communication Services",
    "LYV": "Communication Services",
    # Consumer Discretionary
    "AMZN": "Consumer Discretionary", "TSLA": "Consumer Discretionary",
    "HD": "Consumer Discretionary", "MCD": "Consumer Discretionary",
    "NKE": "Consumer Discretionary", "LOW": "Consumer Discretionary",
    "SBUX": "Consumer Discretionary", "BKNG": "Consumer Discretionary",
    "TJX": "Consumer Discretionary", "ORLY": "Consumer Discretionary",
    "MAR": "Consumer Discretionary", "ABNB": "Consumer Discretionary",
    "CMG": "Consumer Discretionary", "ROST": "Consumer Discretionary",
    "AZO": "Consumer Discretionary", "LULU": "Consumer Discretionary",
    "DHI": "Consumer Discretionary", "LEN": "Consumer Discretionary",
    "GM": "Consumer Discretionary", "F": "Consumer Discretionary",
    "YUM": "Consumer Discretionary", "DPZ": "Consumer Discretionary",
    # Consumer Staples
    "WMT": "Consumer Staples", "PG": "Consumer Staples", "KO": "Consumer Staples",
    "PEP": "Consumer Staples", "COST": "Consumer Staples", "MDLZ": "Consumer Staples",
    "CL": "Consumer Staples", "KMB": "Consumer Staples", "GIS": "Consumer Staples",
    "KHC": "Consumer Staples", "MNST": "Consumer Staples", "KDP": "Consumer Staples",
    "STZ": "Consumer Staples", "KR": "Consumer Staples", "SYY": "Consumer Staples",
    "CCEP": "Consumer Staples", "ADM": "Consumer Staples",
    # Healthcare
    "UNH": "Healthcare", "JNJ": "Healthcare", "LLY": "Healthcare", "ABBV": "Healthcare",
    "MRK": "Healthcare", "PFE": "Healthcare", "TMO": "Healthcare", "ABT": "Healthcare",
    "DHR": "Healthcare", "AMGN": "Healthcare", "ISRG": "Healthcare", "VRTX": "Healthcare",
    "GILD": "Healthcare", "REGN": "Healthcare", "MDT": "Healthcare", "BSX": "Healthcare",
    "CI": "Healthcare", "ELV": "Healthcare", "SYK": "Healthcare", "BDX": "Healthcare",
    "HCA": "Healthcare", "IDXX": "Healthcare", "DXCM": "Healthcare", "BIIB": "Healthcare",
    "GEHC": "Healthcare", "ILMN": "Healthcare", "MRNA": "Healthcare", "IQV": "Healthcare",
    # Financials
    "BRK-B": "Financials", "JPM": "Financials", "V": "Financials", "MA": "Financials",
    "BAC": "Financials", "WFC": "Financials", "MS": "Financials", "GS": "Financials",
    "SCHW": "Financials", "BLK": "Financials", "C": "Financials", "AXP": "Financials",
    "SPGI": "Financials", "PGR": "Financials", "MMC": "Financials", "CB": "Financials",
    "PYPL": "Financials", "FI": "Financials", "ICE": "Financials", "CME": "Financials",
    "PNC": "Financials", "USB": "Financials", "AON": "Financials", "COF": "Financials",
    "PAYX": "Financials", "MET": "Financials", "AIG": "Financials",
    # Industrials
    "CAT": "Industrials", "HON": "Industrials", "UNP": "Industrials", "GE": "Industrials",
    "BA": "Industrials", "RTX": "Industrials", "DE": "Industrials", "LMT": "Industrials",
    "UPS": "Industrials", "ETN": "Industrials", "ADP": "Industrials", "CSX": "Industrials",
    "NSC": "Industrials", "EMR": "Industrials", "ITW": "Industrials", "GD": "Industrials",
    "FDX": "Industrials", "PCAR": "Industrials", "NOC": "Industrials", "WM": "Industrials",
    "PH": "Industrials", "CTAS": "Industrials", "CPRT": "Industrials", "FAST": "Industrials",
    "ODFL": "Industrials", "VRSK": "Industrials", "PAYC": "Industrials", "URI": "Industrials",
    "AXON": "Industrials", "GEV": "Industrials", "CARR": "Industrials", "OTIS": "Industrials",
    # Energy
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy", "SLB": "Energy", "EOG": "Energy",
    "MPC": "Energy", "PSX": "Energy", "OXY": "Energy", "VLO": "Energy", "WMB": "Energy",
    "KMI": "Energy", "HES": "Energy", "BKR": "Energy", "FANG": "Energy", "HAL": "Energy",
    # Utilities
    "NEE": "Utilities", "DUK": "Utilities", "SO": "Utilities", "AEP": "Utilities",
    "D": "Utilities", "EXC": "Utilities", "XEL": "Utilities", "SRE": "Utilities",
    "PEG": "Utilities", "ED": "Utilities", "PCG": "Utilities", "CEG": "Utilities",
    # Materials
    "LIN": "Materials", "SHW": "Materials", "APD": "Materials", "ECL": "Materials",
    "FCX": "Materials", "NUE": "Materials", "DOW": "Materials", "CTVA": "Materials",
    "NEM": "Materials", "DD": "Materials",
    # Real Estate
    "PLD": "Real Estate", "AMT": "Real Estate", "EQIX": "Real Estate", "CCI": "Real Estate",
    "PSA": "Real Estate", "SPG": "Real Estate", "O": "Real Estate", "CSGP": "Real Estate",
    "WELL": "Real Estate", "DLR": "Real Estate",
}


class SectorLookup:
    def __init__(self, use_yfinance: bool = True) -> None:
        self.use_yfinance = use_yfinance
        self._lock = threading.Lock()
        self._cache: Dict[str, str] = {}
        self._last_yf = 0.0
        try:
            if _CACHE_FILE.exists():
                self._cache = json.loads(_CACHE_FILE.read_text())
        except Exception:  # noqa: BLE001
            self._cache = {}

    def _save(self) -> None:
        try:
            _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _CACHE_FILE.write_text(json.dumps(self._cache, indent=0, sort_keys=True))
        except Exception:  # noqa: BLE001
            pass

    def get(self, symbol: str, known: Optional[str] = None) -> str:
        symbol = symbol.upper()
        if known:                                   # from Financials - trust + cache it
            if self._cache.get(symbol) != known:
                with self._lock:
                    self._cache[symbol] = known
                    self._save()
            return known
        if symbol in _STATIC:
            return _STATIC[symbol]
        if symbol in self._cache:
            return self._cache[symbol]
        if not self.use_yfinance:
            return ""
        sec = self._yf_sector(symbol)
        with self._lock:
            self._cache[symbol] = sec
            self._save()
        return sec

    def _yf_sector(self, symbol: str) -> str:
        try:
            import yfinance as yf
        except Exception:  # noqa: BLE001
            return ""
        # be gentle with the .info endpoint
        wait = 0.5 - (time.time() - self._last_yf)
        if wait > 0:
            time.sleep(wait)
        self._last_yf = time.time()
        try:
            t = yf.Ticker(symbol)
            info = t.get_info() if hasattr(t, "get_info") else t.info
            return (info or {}).get("sector", "") or ""
        except Exception as e:  # noqa: BLE001
            log.debug("sector lookup failed for %s: %s", symbol, e)
            return ""
