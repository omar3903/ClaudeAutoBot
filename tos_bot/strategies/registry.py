from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Type

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


def _config_specs(settings) -> Dict[str, Dict[str, Any]]:
    specs: Dict[str, Dict[str, Any]] = {}
    for family in ("technical", "fundamental"):
        for key, spec in settings.strategy_entries(family).items():
            if key not in REGISTRY:
                log.warning("config mentions unknown strategy '%s'", key)
                continue
            specs[key] = spec or {}
    return specs


def _effective(key: str, specs: Dict[str, Dict[str, Any]],
               overrides: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    spec = specs.get(key)
    cfg_enabled = (bool(spec.get("enabled", True)) if spec is not None
                   else bool(getattr(REGISTRY.get(key), "enabled_by_default", False)))
    cfg_weight = float((spec or {}).get("weight", 1.0))
    ov = overrides.get(key) or {}
    return {
        "enabled": bool(ov.get("enabled", cfg_enabled)),
        "weight": float(ov.get("weight", cfg_weight)),
        "params": (spec or {}).get("params", {}) or {},
        "default_enabled": cfg_enabled,
        "default_weight": cfg_weight,
        "customized": bool(ov),
    }


def build_strategies(settings, overrides: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Strategy]:
    """Instantiate the enabled strategies with their params and blend weight.
    ``overrides`` come from the dashboard's strategy panel and switch setups
    on/off or change their weight on top of config.yaml."""
    specs, overrides = _config_specs(settings), overrides or {}
    out: List[Strategy] = []
    for key, cls in REGISTRY.items():
        eff = _effective(key, specs, overrides)
        if eff["enabled"]:
            out.append(cls(params=eff["params"], weight=eff["weight"]))
    log.info("loaded %d strategies: %s", len(out), ", ".join(s.key for s in out))
    return out


def build_enabled_strategies(settings) -> List[Strategy]:
    """config.yaml only (scripts and tests)."""
    return build_strategies(settings)


def strategy_catalog(settings, overrides: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Every registered setup with its effective on/off state and weight."""
    specs, overrides = _config_specs(settings), overrides or {}
    rows = []
    for key, cls in REGISTRY.items():
        eff = _effective(key, specs, overrides)
        eff.pop("params")
        rows.append({**cls().describe(), **eff})
    return rows


def describe_all() -> List[Dict[str, str]]:
    return [cls().describe() for cls in REGISTRY.values()]
