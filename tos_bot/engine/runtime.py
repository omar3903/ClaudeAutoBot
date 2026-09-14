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


def load_capital(saved: Any) -> Dict[str, float]:
    """Trading capital per venue - positive, finite amounts only."""
    return {str(venue): float(amount)
            for venue, amount in (saved.items() if isinstance(saved, dict) else ())
            if isinstance(amount, (int, float)) and math.isfinite(amount) and amount > 0}
