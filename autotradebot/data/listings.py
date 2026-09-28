"""Every US-listed stock and ADR, from Nasdaq Trader's daily symbol directory.

The directory covers Nasdaq, NYSE, NYSE American, NYSE Arca, Cboe BZX and IEX.
ETFs, test issues, warrants, units, rights, preferreds and notes are left out;
what remains is common stock and depositary receipts, roughly 5,700 names.
Each day's copy is cached; if the download fails the newest cached copy is used.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import requests

log = logging.getLogger(__name__)

_NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
_OTHER_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
_OTHER_EXCHANGES = {"N": "NYSE", "A": "NYSE American", "P": "NYSE Arca", "Z": "Cboe BZX", "V": "IEX"}
_NOT_A_STOCK = re.compile(
    r"\b(warrants?|units?|rights?|preferred|notes?|debentures?|subordinated|trust preferred)\b|%",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Listing:
    symbol: str          # IBKR spelling - share classes use a space ("BRK B")
    name: str
    exchange: str


def ibkr_symbol(directory_symbol: str) -> str:
    return re.sub(r"[./]", " ", directory_symbol.strip().upper())


def parse_directory(nasdaq_text: str, other_text: str) -> List[Listing]:
    rows: List[Listing] = []
    for text, symbol_col, exchange_of in (
        (nasdaq_text, "Symbol", lambda r: "Nasdaq"),
        (other_text, "ACT Symbol", lambda r: _OTHER_EXCHANGES.get(r.get("Exchange", ""), "")),
    ):
        for r in csv.DictReader(io.StringIO(text), delimiter="|"):
            raw = (r.get(symbol_col) or "").strip()
            if not raw or raw.startswith("File Creation") or "$" in raw:
                continue
            if r.get("Test Issue") == "Y" or r.get("ETF") == "Y":
                continue
            name = (r.get("Security Name") or "").strip()
            if _NOT_A_STOCK.search(name):
                continue
            rows.append(Listing(ibkr_symbol(raw), name, exchange_of(r)))
    unique = {l.symbol: l for l in rows}
    return sorted(unique.values(), key=lambda l: l.symbol)


def _download() -> Tuple[str, str]:
    texts = []
    for url in (_NASDAQ_URL, _OTHER_URL):
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        texts.append(r.text)
    return texts[0], texts[1]


class UsListings:
    def __init__(self, cache_dir: Path, fetch: Callable[[], Tuple[str, str]] = _download) -> None:
        self.cache_dir = cache_dir
        self._fetch = fetch

    def load(self, today: Optional[dt.date] = None) -> List[Listing]:
        today = today or dt.date.today()
        cached = self._read(self.cache_dir / f"listings_{today.isoformat()}.json")
        if cached:
            return cached
        try:
            listings = parse_directory(*self._fetch())
        except Exception as e:  # noqa: BLE001
            log.warning("symbol directory download failed (%s) - using the last copy", e)
            return self._newest_cached()
        if listings:
            self._write(self.cache_dir / f"listings_{today.isoformat()}.json", listings)
        log.info("US listings: %d stocks and ADRs", len(listings))
        return listings

    def _newest_cached(self) -> List[Listing]:
        files = sorted(self.cache_dir.glob("listings_*.json"))
        return self._read(files[-1]) if files else []

    @staticmethod
    def _read(path: Path) -> List[Listing]:
        try:
            return [Listing(*row) for row in json.loads(path.read_text(encoding="utf-8"))]
        except (OSError, ValueError, TypeError):
            return []

    def _write(self, path: Path, listings: List[Listing]) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([[l.symbol, l.name, l.exchange] for l in listings]), encoding="utf-8")
        for old in sorted(self.cache_dir.glob("listings_*.json"))[:-3]:
            old.unlink(missing_ok=True)
