from __future__ import annotations

import logging
from typing import Dict, List, Type

from .base import Strategy

log = logging.getLogger(__name__)

REGISTRY: Dict[str, Type[Strategy]] = {}


def register(cls: Type[Strategy]) -> Type[Strategy]:
    if not getattr(cls, "key", None) or cls.key == "base":
        raise ValueError(f"{cls.__name__} needs a unique .key")
    if cls.key in REGISTRY:
        raise ValueError(f"duplicate strategy key '{cls.key}'")
    REGISTRY[cls.key] = cls
    return cls


def build_enabled_strategies(settings) -> List[Strategy]:
    """Instantiate the strategies switched on in config, with their params
    and blend weight."""
    out: List[Strategy] = []
    for family in ("technical", "fundamental"):
        for key, spec in settings.strategy_entries(family).items():
            spec = spec or {}
            if not spec.get("enabled", True):
                continue
            cls = REGISTRY.get(key)
            if cls is None:
                log.warning("config enables unknown strategy '%s'", key)
                continue
            out.append(cls(params=spec.get("params", {}) or {},
                           weight=float(spec.get("weight", 1.0))))
    log.info("loaded %d strategies: %s", len(out), ", ".join(s.key for s in out))
    return out


def describe_all() -> List[Dict[str, str]]:
    return [cls().describe() for cls in REGISTRY.values()]
