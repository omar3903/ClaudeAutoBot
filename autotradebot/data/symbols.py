"""What IBKR knows about each symbol: contract id, security type, sector.

Looked up once per symbol with IBKR contract details and kept on disk, so only
symbols never seen before cost a request - new listings, or everything on the
very first run. Symbols IBKR has no contract for are remembered too, and asked
about again after a month.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

from .sectors import sector_from_ibkr

log = logging.getLogger(__name__)

#: IBKR stock types that are ordinary shares
TRADABLE_STOCK_TYPES = frozenset({"COMMON", "ADR", "REIT", "NY REG SHRS", "GDR"})
_RETRY_MISSING_S = 30 * 86400


@dataclass
class SymbolInfo:
    symbol: str
    con_id: int = 0
    exchange: str = ""
    stock_type: str = ""
    industry: str = ""
    category: str = ""
    checked_at: float = 0.0

    @property
    def found(self) -> bool:
        return self.con_id > 0

    @property
    def tradable(self) -> bool:
        return self.found and (not self.stock_type or self.stock_type in TRADABLE_STOCK_TYPES)

    @property
    def sector(self) -> str:
        return sector_from_ibkr(self.industry, self.category)


class SymbolMaster:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._info: Dict[str, SymbolInfo] = self._load()

    def get(self, symbol: str) -> Optional[SymbolInfo]:
        return self._info.get(symbol)

    def sector(self, symbol: str) -> str:
        info = self._info.get(symbol)
        return info.sector if info else ""

    def unknown(self, symbols: Iterable[str]) -> List[str]:
        now = time.time()
        return [s for s in symbols
                if s not in self._info
                or (not self._info[s].found and now - self._info[s].checked_at > _RETRY_MISSING_S)]

    def tradable(self, symbols: Iterable[str]) -> List[str]:
        return [s for s in symbols if s in self._info and self._info[s].tradable]

    def record(self, details: Mapping[str, Optional[dict]]) -> None:
        """Store contract details by symbol; ``None`` = IBKR has no such stock."""
        now = time.time()
        with self._lock:
            for symbol, d in details.items():
                self._info[symbol] = SymbolInfo(symbol=symbol, checked_at=now, **(d or {}))
            self._save()

    def peers(self, symbol: str, among: Iterable[str], limit: int) -> List[str]:
        """Same IBKR category first, then the same industry."""
        me = self._info.get(symbol)
        if me is None or not me.industry:
            return []
        pool = [s for s in among if s != symbol and s in self._info]
        same_cat = [s for s in pool if self._info[s].category == me.category]
        same_ind = [s for s in pool if self._info[s].industry == me.industry and s not in same_cat]
        return (same_cat + same_ind)[:limit]

    def _load(self) -> Dict[str, SymbolInfo]:
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
            return {r["symbol"]: SymbolInfo(**r) for r in rows}
        except (OSError, ValueError, TypeError, KeyError):
            return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(i) for i in self._info.values()]), encoding="utf-8")
        tmp.replace(self.path)
