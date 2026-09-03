"""Load the tradable universe.

Primary source: Nasdaq Trader public symbol directory
(https://www.nasdaqtrader.com/dynamic/SymDir/). Cached to
``data/cache/universe_*.csv`` for the day. If the download fails we fall back
to a bundled Nasdaq-100 list so the app still runs offline.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
from pathlib import Path
from typing import List, Optional, Set

from ..config import PROJECT_ROOT

log = logging.getLogger(__name__)

_CACHE_DIR = PROJECT_ROOT / "data" / "cache"
_NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
_OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

# Bundled fallback - Nasdaq-100 constituents (trimmed, good enough offline).
_FALLBACK_NDX = """
AAPL MSFT NVDA AMZN META GOOGL GOOG AVGO TSLA COST NFLX AMD PEP ADBE CSCO
TMUS INTC CMCSA QCOM INTU AMGN TXN HON AMAT BKNG ISRG VRTX ADP REGN SBUX
GILD MDLZ ADI LRCX PANW MU KLAC SNPS CDNS MELI ASML CRWD PYPL MAR ORLY
CTAS ABNB FTNT DXCM ROP NXPI ADSK PCAR CPRT MNST AEP KDP PAYX ODP ROST
IDXX FAST EA CSGP DDOG VRSK EXC XEL CCEP KHC LULU BIIB ON GEHC TTD ANSS
DLTR WBD ZS TEAM MDB CDW GFS TTWO ILMN WDAY SIRI
""".split()


class UniverseLoader:
    def __init__(self, cache_dir: Path = _CACHE_DIR) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    def load(
        self,
        which: str = "nasdaq",
        *,
        include_etf: bool = False,
        max_symbols: Optional[int] = None,
        universe_file: Optional[str] = None,
    ) -> List[str]:
        which = which.lower()
        if which == "file" and universe_file:
            return self._from_file(universe_file, max_symbols)
        if which in ("nasdaq100", "ndx"):
            syms = list(_FALLBACK_NDX)
        elif which == "sp500":
            syms = self._sp500() or list(_FALLBACK_NDX)
        else:
            syms = self._nasdaq_all(include_etf=include_etf)

        syms = self._clean(syms)
        if max_symbols:
            syms = syms[:max_symbols]
        log.info("universe '%s' -> %d symbols", which, len(syms))
        return syms

    # ------------------------------------------------------------------ #
    def _from_file(self, path: str, max_symbols: Optional[int]) -> List[str]:
        p = Path(path)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        if not p.exists():
            log.warning("universe file %s missing - using fallback", p)
            return self._clean(list(_FALLBACK_NDX))
        syms = [ln.strip().upper() for ln in p.read_text().splitlines()
                if ln.strip() and not ln.startswith("#")]
        syms = self._clean(syms)
        return syms[:max_symbols] if max_symbols else syms

    def _cache_file(self, tag: str) -> Path:
        today = dt.date.today().isoformat()
        return self.cache_dir / f"universe_{tag}_{today}.csv"

    def _nasdaq_all(self, include_etf: bool) -> List[str]:
        cache = self._cache_file("nasdaq")
        if cache.exists():
            return cache.read_text().split()

        syms: Set[str] = set()
        try:
            import requests

            for url, sym_col, etf_col, test_col in (
                (_NASDAQ_LISTED, "Symbol", "ETF", "Test Issue"),
                (_OTHER_LISTED, "ACT Symbol", "ETF", "Test Issue"),
            ):
                r = requests.get(url, timeout=20)
                r.raise_for_status()
                text = r.text
                reader = csv.DictReader(io.StringIO(text), delimiter="|")
                for row in reader:
                    sym = (row.get(sym_col) or "").strip().upper()
                    if not sym or sym.startswith("File Creation"):
                        continue
                    if (row.get(test_col) or "").strip() == "Y":
                        continue
                    if not include_etf and (row.get(etf_col) or "").strip() == "Y":
                        continue
                    syms.add(sym)
            if syms:
                cache.write_text(" ".join(sorted(syms)))
        except Exception as e:  # noqa: BLE001
            log.warning("nasdaqtrader download failed (%s) - using fallback list", e)
            return list(_FALLBACK_NDX)
        return sorted(syms) if syms else list(_FALLBACK_NDX)

    def _sp500(self) -> List[str]:
        cache = self._cache_file("sp500")
        if cache.exists():
            return cache.read_text().split()
        try:
            import requests

            r = requests.get(
                "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                timeout=20, headers={"User-Agent": "tos-trader/0.1"},
            )
            r.raise_for_status()
            import re

            rows = re.findall(r'<td><a[^>]+>([A-Z][A-Z.\-]{0,6})</a>', r.text)
            syms = sorted(set(s.replace(".", "-") for s in rows))
            if len(syms) > 400:
                cache.write_text(" ".join(syms))
                return syms
        except Exception as e:  # noqa: BLE001
            log.warning("sp500 fetch failed: %s", e)
        return []

    @staticmethod
    def _clean(syms: List[str]) -> List[str]:
        out: List[str] = []
        seen: Set[str] = set()
        for s in syms:
            s = s.strip().upper()
            if not s or s in seen:
                continue
            # drop warrants / units / preferreds / test tickers
            if any(ch in s for ch in (".", "$", " ")) or len(s) > 5:
                continue
            if s.endswith(("W", "R", "U")) and len(s) == 5:
                continue
            seen.add(s)
            out.append(s)
        return out
