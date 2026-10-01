"""The dashboard's remembered choices (``data/runtime.json``).

Mode, paper platform, filters, strategies, trading capital, scan settings,
Autopilot and an unfinished quit are saved here whenever they change, and win
over config.yaml on the next start. Anything unreadable falls back to the
defaults rather than stopping the app.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Dict, Iterable

from ..scanner.filters import TradeFilters
from ..strategies.registry import REGISTRY

log = logging.getLogger(__name__)


class RuntimeFile:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def write(self, payload: Dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            log.warning("could not save %s", self.path, exc_info=True)


def load_filters(saved: Any, default_sectors: Iterable[str]) -> TradeFilters:
    f = saved if isinstance(saved, dict) else {}
    sectors = f.get("sectors", list(default_sectors))
    try:
        return TradeFilters.build(f.get("sides"), f.get("timeframes"), sectors)
    except ValueError:
        return TradeFilters.build(sectors=sectors)


def load_strategy_overrides(saved: Any) -> Dict[str, Dict[str, Any]]:
    """Only known setups, and only an on/off flag and a numeric weight."""
    out: Dict[str, Dict[str, Any]] = {}
    for key, override in (saved.items() if isinstance(saved, dict) else ()):
        if key not in REGISTRY or not isinstance(override, dict):
            continue
        clean: Dict[str, Any] = {}
        if isinstance(override.get("enabled"), bool):
            clean["enabled"] = override["enabled"]
        if isinstance(override.get("weight"), (int, float)):
            clean["weight"] = float(override["weight"])
        if clean:
            out[key] = clean
    return out


def load_day_trade_pct(saved: Any, default: float) -> float:
    """The part of the trading capital day trades may hold, in percent (see engine/capital.py)."""
    value = saved.get("day_pct") if isinstance(saved, dict) else None
    ok = isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 100
    return float(value) if ok else float(default)


def load_size_factor(saved: Any, default: float = 1.0) -> float:
    """The position size factor, 0-5: every position is the usual size times this (engine/capital.py)."""
    value = saved.get("factor") if isinstance(saved, dict) else None
    ok = (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
          and 0 <= value <= 5)
    return float(value) if ok else float(default)


def load_capital_mode(saved: Any) -> Dict[str, str]:
    """What the whole account means, per venue: "margin" (its buying power) or "cash" (only the money in it)."""
    return {str(venue): str(mode) for venue, mode in (saved.items() if isinstance(saved, dict) else ())
            if mode in ("margin", "cash")}


def load_capital(saved: Any) -> Dict[str, float]:
    """Trading capital per venue - positive, finite amounts only."""
    return {str(venue): float(amount)
            for venue, amount in (saved.items() if isinstance(saved, dict) else ())
            if isinstance(amount, (int, float)) and math.isfinite(amount) and amount > 0}
