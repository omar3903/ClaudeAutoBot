"""Running the strategy replay in the background, and keeping its results.

One replay at a time runs on its own thread and reports its progress on the bus.
Replaying is CPU work and every stock is independent, so the stocks are spread
over a few worker processes. The simulated trades are saved, so the records
survive a restart and can be re-read with different Autopilot settings (which
noise flags it skips, how many confirmations it wants) without replaying again.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

import pandas as pd

from ..core.eventbus import BUS
from ..scanner.noise import NoiseSettings
from ..strategies.base import Strategy
from .history import IntradayHistory
from .replay import ReplaySettings, SimTrade, noise_report, replay_intraday, replay_swing, strategy_records

log = logging.getLogger(__name__)


class ReplayRunner:
    def __init__(self, results_path: Path, history: IntradayHistory, bus=BUS, workers: Optional[int] = None) -> None:
        self.results_path = results_path
        self.history = history
        self.bus = bus
        #: worker processes; 1 replays on the runner's own thread
        self.workers = workers if workers is not None else max(1, min(6, (os.cpu_count() or 2) - 1))
        self._thread: Optional[threading.Thread] = None
        self._progress: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()
        self._results: Dict[str, Any] = {}
        self._trades: List[SimTrade] = []
        self._records_key: Optional[tuple] = None
        self._records: Dict[str, Dict[str, Any]] = {}
        self._load()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, *, strategies: Sequence[Strategy], source, daily_frame: Callable[[str], Optional[pd.DataFrame]],
              intraday_symbols: Sequence[str], swing_symbols: Sequence[str], sessions: int, swing_sessions: int,
              settings: ReplaySettings, noise: NoiseSettings,
              con_ids: Optional[Mapping[str, int]] = None) -> Dict[str, Any]:
        with self._lock:
            if self.running:
                return {"ok": False, "reason": "A replay is already running."}
            self._progress = {"stage": "starting", "done": 0, "total": 1}
            self._thread = threading.Thread(
                target=self._run, name="replay", daemon=True,
                args=(list(strategies), source, daily_frame, list(intraday_symbols), list(swing_symbols),
                      sessions, swing_sessions, settings, noise, con_ids or {}))
            self._thread.start()
        return {"ok": True, "note": (f"Replaying the last {sessions} sessions of day-trade setups on "
                                     f"{len(intraday_symbols)} stocks and {swing_sessions} sessions of swing "
                                     f"setups on {len(swing_symbols)}. It runs in the background.")}

    def wait(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self, strategies, source, daily_frame, intraday_symbols, swing_symbols, sessions, swing_sessions,
             settings, noise, con_ids) -> None:
        started = time.monotonic()
        try:
            bars = self.history.load(source, intraday_symbols, sessions, con_ids,
                                     progress=lambda done, total: self._report("5-minute candles", done, total))
            jobs = [("intraday", strategies, s, bars[s], daily, settings, noise, sessions)
                    for s in intraday_symbols if s in bars and (daily := daily_frame(s)) is not None]
            jobs += [("swing", strategies, s, None, daily, settings, noise, swing_sessions)
                     for s in swing_symbols if (daily := daily_frame(s)) is not None]
            trades = self._replay(jobs)
            self._save({
                "ran_at": dt.datetime.now(dt.timezone.utc).isoformat(), "sessions": sessions,
                "swing_sessions": swing_sessions, "intraday_symbols": len(bars), "swing_symbols": len(swing_symbols),
                "elapsed_s": round(time.monotonic() - started, 1), "noise": noise_report(trades),
                "trades": [dataclasses.asdict(t) for t in trades],
            }, trades)
            log.info("replay finished: %d simulated trades in %.0fs", len(trades), time.monotonic() - started)
            self.bus.publish("replay.completed", trades=len(trades))
        except Exception as e:  # noqa: BLE001 - reported to the dashboard, never kills the app
            log.exception("replay failed")
            self.bus.publish("replay.failed", reason=str(e))
        finally:
            self._progress = None

    def _replay(self, jobs: Sequence[tuple]) -> List[SimTrade]:
        trades: List[SimTrade] = []
        if self.workers <= 1 or len(jobs) <= 1:
            for n, job in enumerate(jobs, 1):
                trades += [SimTrade(**t) for t in replay_job(job)]
                self._report("replaying setups", n, len(jobs))
        else:
            with ProcessPoolExecutor(max_workers=self.workers) as pool:
                futures = [pool.submit(replay_job, job) for job in jobs]
                for n, future in enumerate(as_completed(futures), 1):
                    trades += [SimTrade(**t) for t in future.result()]
                    self._report("replaying setups", n, len(jobs))
        return sorted(trades, key=lambda t: (t.exited_at, t.symbol, t.strategy))

    def _report(self, stage: str, done: int, total: int) -> None:
        self._progress = {"stage": stage, "done": done, "total": total}
        self.bus.publish("replay.progress", **self._progress)

    # ---- results --------------------------------------------------------------- #
    def records(self, skip_noise: Iterable[str], min_confirmations: int) -> Dict[str, Dict[str, Any]]:
        """Each strategy's replayed record over the trades Autopilot would have taken.
        Asked for on every play, so kept until the settings or the results change."""
        key = (tuple(sorted(skip_noise)), int(min_confirmations), self._results.get("ran_at"))
        if key != self._records_key:
            self._records, self._records_key = strategy_records(self._trades, skip_noise, min_confirmations), key
        return self._records

    def state(self, skip_noise: Iterable[str], min_confirmations: int) -> Dict[str, Any]:
        meta = {k: v for k, v in self._results.items() if k != "trades"}
        return {**meta, "running": self.running, "progress": self._progress, "trade_count": len(self._trades),
                "records": {"all": strategy_records(self._trades),
                            "autopilot": self.records(skip_noise, min_confirmations)}}

    def _load(self) -> None:
        try:
            data = json.loads(self.results_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self._results, self._trades = data, [SimTrade(**t) for t in data.get("trades", [])]

    def _save(self, data: Dict[str, Any], trades: List[SimTrade]) -> None:
        self.results_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.results_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self.results_path)
        self._results, self._trades = data, trades


def replay_job(job: tuple) -> List[Dict[str, Any]]:
    """One stock's replay. It may run in a worker process, so it takes and returns plain data."""
    kind, strategies, symbol, bars, daily, settings, noise, sessions = job
    trades = (replay_intraday(strategies, symbol, bars, daily, settings, noise, sessions) if kind == "intraday"
              else replay_swing(strategies, symbol, daily, settings, noise, sessions))
    return [dataclasses.asdict(t) for t in trades]
