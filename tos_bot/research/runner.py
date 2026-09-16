"""Running the strategy replay in the background, and keeping its results.

One replay at a time runs on its own thread and reports its progress on the bus.
Replaying is CPU work and every stock is independent, so the stocks are spread
over a few worker processes. The simulated trades are saved, so the records
survive a restart and can be re-read with different Autopilot settings (which
noise flags it skips, how many confirmations it wants) without replaying again.

Every run also leaves a line in ``replay_runs.jsonl`` next to the results - its
settings, each strategy's record and each noise check's verdict - so how the
setups hold up can be followed from one run to the next.
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
from ..util import clock
from .history import IntradayHistory
from .replay import (HELD_OUT_FRACTION, ReplaySettings, SimTrade, held_out_from, learned_skips, noise_report,
                     records_by_strategy, replay_intraday, replay_swing, strategy_records, taken)

log = logging.getLogger(__name__)

HISTORY_FILE = "replay_runs.jsonl"


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
        self._taken: Dict[str, List[float]] = {}
        self._load()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, *, strategies: Sequence[Strategy], source, daily_frame: Callable[[str], Optional[pd.DataFrame]],
              intraday_symbols: Sequence[str], swing_symbols: Sequence[str], sessions: int, swing_sessions: int,
              settings: ReplaySettings, noise: NoiseSettings, con_ids: Optional[Mapping[str, int]] = None,
              market: Optional[Callable[[dt.date], Mapping[dt.date, float]]] = None,
              earnings: Optional[Callable[[Sequence[str]], Mapping[str, Sequence[str]]]] = None,
              pairs: Optional[Mapping[str, Any]] = None,
              held_out_fraction: float = HELD_OUT_FRACTION,
              news: Optional[Callable[[Sequence[str], dt.date], Mapping[str, Sequence[Mapping[str, Any]]]]] = None,
              benchmark: Optional[str] = None) -> Dict[str, Any]:
        """``market``: the turbulent regime's probability per day, from a model fitted before the
        given first day; ``earnings``: when each stock's earnings filings were accepted; ``news``:
        each stock's stored stories since a first day, for the news checks; ``benchmark``: the
        S&P 500 ETF's symbol, whose candles the market model behind those checks needs. All are
        asked for on the replay's own thread, and the replay goes on without them if they fail."""
        with self._lock:
            if self.running:
                return {"ok": False, "reason": "A replay is already running."}
            self._progress = {"stage": "starting", "done": 0, "total": 1}
            self._thread = threading.Thread(
                target=self._run, name="replay", daemon=True,
                args=(list(strategies), source, daily_frame, list(intraday_symbols), list(swing_symbols),
                      sessions, swing_sessions, settings, noise, con_ids or {}, market, earnings,
                      held_out_fraction, pairs, news, benchmark))
            self._thread.start()
        held = f"{held_out_fraction:.0%}"
        return {"ok": True, "note": (f"Replaying the last {sessions} sessions of day-trade setups on "
                                     f"{len(intraday_symbols)} stocks and {swing_sessions} sessions of swing "
                                     f"setups on {len(swing_symbols)}, holding out the latest {held} to test "
                                     "them on. It runs in the background.")}

    def wait(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self, strategies, source, daily_frame, intraday_symbols, swing_symbols, sessions, swing_sessions,
             settings, noise, con_ids, market, earnings, fraction, pairs=None, news=None, benchmark=None) -> None:
        started = time.monotonic()
        try:
            wanted = intraday_symbols + ([benchmark] if benchmark and benchmark not in intraday_symbols else [])
            bars = self.history.load(source, wanted, sessions, con_ids,
                                     progress=lambda done, total: self._report("5-minute candles", done, total))
            last = clock.prev_trading_day(clock.session_date())
            first = min(clock.last_n_sessions(last, max(sessions, swing_sessions)))
            self._report("market regime, earnings dates and news", 0, 1)
            regime = dict(self._optional("the market regime", market, first) or {})
            reports = {s: tuple(v) for s, v in (self._optional("earnings dates", earnings, list(bars)) or {}).items()}
            everyone = list(dict.fromkeys(list(intraday_symbols) + list(swing_symbols)))
            stories = {s: list(v) for s, v in (self._optional("the news", news, everyone, first) or {}).items()}
            bench_bars = bars.get(benchmark) if benchmark else None
            bench_daily = daily_frame(benchmark) if benchmark else None
            split = {"INTRADAY": held_out_from(last, sessions, fraction),
                     "SWING": held_out_from(last, swing_sessions, fraction)}
            jobs = [("intraday", strategies, s, bars[s], daily, settings, noise, sessions, regime, reports.get(s, ()),
                     stories.get(s, ()), bench_bars, bench_daily)
                    for s in intraday_symbols if s in bars and (daily := daily_frame(s)) is not None]
            jobs += [("swing", strategies, s, None, daily, settings, noise, swing_sessions, regime, (),
                      stories.get(s, ()), None, bench_daily)
                     for s in swing_symbols if (daily := daily_frame(s)) is not None]
            if pairs:
                frames = {s: f for s in pairs["groups"] if (f := daily_frame(s)) is not None}
                jobs.append(("pairs", pairs, frames, swing_sessions))
            trades = self._replay(jobs)
            data = {
                "ran_at": dt.datetime.now(dt.timezone.utc).isoformat(), "sessions": sessions,
                "swing_sessions": swing_sessions, "intraday_symbols": len(bars), "swing_symbols": len(swing_symbols),
                "elapsed_s": round(time.monotonic() - started, 1), "held_out_from": split,
                "costs": {"slippage_bps": settings.slippage_bps, "commission_bps": settings.commission_bps},
                "market_regime_days": len(regime), "earnings_stocks": sum(1 for v in reports.values() if v),
                "news_stocks": sum(1 for v in stories.values() if v), "benchmark": benchmark if bench_daily is not None else None,
                "noise": noise_report(trades, split), "trades": [dataclasses.asdict(t) for t in trades],
            }
            self._save(data, trades)
            self._append_history(data, trades)
            log.info("replay finished: %d simulated trades in %.0fs", len(trades), time.monotonic() - started)
            self.bus.publish("replay.completed", trades=len(trades))
        except Exception as e:  # noqa: BLE001 - reported to the dashboard, never kills the app
            log.exception("replay failed")
            self.bus.publish("replay.failed", reason=str(e))
        finally:
            self._progress = None

    @staticmethod
    def _optional(what: str, source: Optional[Callable], *args):
        if source is None:
            return None
        try:
            return source(*args)
        except Exception:  # noqa: BLE001
            log.warning("replay goes on without %s", what, exc_info=True)
            return None

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
    @property
    def ran_at(self) -> Optional[str]:
        """When the results were made - it changes whenever a replay finishes."""
        return self._results.get("ran_at")

    @property
    def split(self) -> Optional[Dict[str, Optional[str]]]:
        return self._results.get("held_out_from")

    def records(self, skip_noise: Iterable[str], min_confirmations: int) -> Dict[str, Dict[str, Any]]:
        """Each strategy's replayed record over the trades Autopilot would have taken, with its
        held-out sessions' record. Asked for on every play, so kept until the settings or the
        results change."""
        self._refresh(skip_noise, min_confirmations)
        return self._records

    def r_multiples(self, strategy: str, skip_noise: Iterable[str], min_confirmations: int) -> List[float]:
        """The R of every replayed trade of ``strategy`` that Autopilot would have taken."""
        self._refresh(skip_noise, min_confirmations)
        return self._taken.get(strategy, [])

    def learned_skips(self) -> List[str]:
        return learned_skips(self._results.get("noise"))

    def _refresh(self, skip_noise: Iterable[str], min_confirmations: int) -> None:
        key = (tuple(sorted(skip_noise)), int(min_confirmations), self._results.get("ran_at"))
        if key == self._records_key:
            return
        chosen = taken(self._trades, key[0], min_confirmations)
        by_strategy: Dict[str, List[float]] = {}
        for t in chosen:
            by_strategy.setdefault(t.strategy, []).append(t.r)
        self._records = records_by_strategy(chosen, self.split)
        self._taken, self._records_key = by_strategy, key

    def state(self, skip_noise: Iterable[str], min_confirmations: int) -> Dict[str, Any]:
        meta = {k: v for k, v in self._results.items() if k != "trades"}
        return {**meta, "running": self.running, "progress": self._progress, "trade_count": len(self._trades),
                "learned_skips": self.learned_skips(),
                "records": {"all": strategy_records(self._trades, split=self.split),
                            "autopilot": self.records(skip_noise, min_confirmations)}}

    def runs(self, limit: int = 30) -> List[Dict[str, Any]]:
        """The latest runs' summaries, newest first."""
        try:
            lines = (self.results_path.parent / HISTORY_FILE).read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in reversed(lines):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
            if len(out) >= limit:
                break
        return out

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

    def _append_history(self, data: Dict[str, Any], trades: List[SimTrade]) -> None:
        summary = {k: data[k] for k in ("ran_at", "sessions", "swing_sessions", "intraday_symbols", "swing_symbols",
                                         "held_out_from", "costs", "market_regime_days", "earnings_stocks")}
        summary["news_stocks"] = data.get("news_stocks", 0)
        summary["trade_count"] = len(trades)
        summary["records"] = strategy_records(trades, split=data["held_out_from"])
        summary["noise"] = {check: {"removes": row["removes"], "removed_avg_r": row["removed_avg_r"],
                                    "kept_avg_r": row["kept_avg_r"], "verdict": row["verdict"],
                                    "held_out_verdict": (row.get("held_out") or {}).get("verdict")}
                            for check, row in data["noise"].items()}
        try:
            with (self.results_path.parent / HISTORY_FILE).open("a", encoding="utf-8") as f:
                f.write(json.dumps(summary) + "\n")
        except OSError:
            log.warning("could not add the replay to its history", exc_info=True)


def replay_job(job: tuple) -> List[Dict[str, Any]]:
    """One stock's replay. It may run in a worker process, so it takes and returns plain data."""
    if job[0] == "pairs":
        from ..pairs.backtest import replay_pairs

        _, pairs, frames, sessions = job
        trades = replay_pairs(frames, pairs["groups"], pairs["rules"], pairs["finder"], sessions)
        return [dataclasses.asdict(t) for t in trades]
    kind, strategies, symbol, bars, daily, settings, noise, sessions, market, earnings, news, bench_bars, bench_daily = job
    trades = (replay_intraday(strategies, symbol, bars, daily, settings, noise, sessions, market, earnings,
                              news, bench_bars, bench_daily)
              if kind == "intraday" else replay_swing(strategies, symbol, daily, settings, noise, sessions, market,
                                                      news, bench_daily))
    return [dataclasses.asdict(t) for t in trades]
