"""Small helpers the engine and its mixins share."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Mapping, Optional

from ..core.enums import PlayStatus

log = logging.getLogger(__name__)


#: play statuses that must never be executed again
_ACTED_ON = frozenset({PlayStatus.ACCEPTED, PlayStatus.SUBMITTED, PlayStatus.WORKING,
                       PlayStatus.PARTIAL, PlayStatus.FILLED, PlayStatus.ERROR})


def attempt(action: Callable[..., Dict[str, Any]], *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Run an operator action; an unexpected error comes back as a reason instead of escaping."""
    try:
        return action(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        log.exception("%s failed", getattr(action, "__name__", "action"))
        return {"ok": False, "reason": f"It didn't go through: {e}"}


def movers_built(review: Optional[Mapping[str, Any]]) -> bool:
    return bool(((review or {}).get("movers") or {}).get("ok"))


def duration(seconds: float) -> str:
    """A span of time in words: "45 s", "3 min", "1 h 5 min"."""
    s = int(max(0.0, seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    return f"{s // 3600} h {(s % 3600) // 60} min"
