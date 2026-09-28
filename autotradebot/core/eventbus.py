"""A tiny pub/sub bus from the engine's threads to the dashboard.

The engine publishes domain events (``scan.completed``, ``order.filled``,
``quit.done``, ...). The web layer registers an ``asyncio.Queue`` per
WebSocket client. Nothing here depends on FastAPI.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Set

log = logging.getLogger(__name__)


@dataclass
class Event:
    topic: str
    payload: Dict[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_dict(self) -> Dict[str, Any]:
        return {"topic": self.topic, "ts": self.ts.isoformat(), "payload": self.payload}


class EventBus:
    def __init__(self) -> None:
        self._queues: Set[asyncio.Queue] = set()
        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- wiring ---------------------------------------------------------- #
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called once from the web server so background threads can push
        events into asyncio queues safely."""
        self._loop = loop

    def add_queue(self, q: "asyncio.Queue[Event]") -> None:
        with self._lock:
            self._queues.add(q)

    def remove_queue(self, q: "asyncio.Queue[Event]") -> None:
        with self._lock:
            self._queues.discard(q)

    # -- publishing ---------------------------------------------------- #
    def publish(self, topic: str, **payload: Any) -> None:
        evt = Event(topic=topic, payload=payload)
        with self._lock:
            queues = list(self._queues)
        for q in queues:
            self._offer(q, evt)

    def _offer(self, q: "asyncio.Queue[Event]", evt: Event) -> None:
        loop = self._loop
        if loop is None:
            return
        def _put() -> None:
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                # drop the oldest, keep the stream live
                try:
                    q.get_nowait()
                    q.put_nowait(evt)
                except Exception:  # noqa: BLE001
                    pass
        try:
            loop.call_soon_threadsafe(_put)
        except RuntimeError:
            pass


#: process-wide bus
BUS = EventBus()
