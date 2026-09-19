"""Running the strategy replay in the background, and keeping its results.

One replay at a time runs on its own thread and reports its progress on the bus.
Replaying is CPU work and every stock is independent, so the stocks are spread
over worker processes - all the machine's cores but two, unless config says
otherwise. A day-trade replay of one stock is cut into chunks of a few sessions
(each chunk carries the sessions before it that the rolling window needs, and a
day trade never spans sessions), so the long jobs don't leave workers idle at
the end; a swing replay carries positions from session to session and stays
one job per stock. The simulated trades are saved, so the records
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
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

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
SESSIONS_PER_JOB = 10
#: sessions of 5-minute candles a day-trade context looks back over (replay.LIVE_INTRADAY_BARS)
LOOKBACK_SESSIONS = 5


class ReplayRunner:
    def __init__(self, results_path: Path, history: IntradayHistory, bus=BUS, workers: Optional[int] = None,
                 sink: Optional[Callable[[Dict[str, Any], List[SimTrade]], Any]] = None) -> None:
        """``sink``: called with the results and the trades once a run is saved - the engine keeps
        them in the database (research/dataset.py); a sink that fails never fails the replay."""
        self.results_path = results_path
        self.history = history
        self.bus = bus
        self.sink = sink
        #: worker processes; 1 replays on the runner's own thread
        self.workers = workers if workers else max(1, (os.cpu_count() or 2) - 2)
        #: sessions per day-trade job (see the module docstring)
        self.sessions_per_job = SESSIONS_PER_JOB
        self._thread: Optional[threading.Thread] = None
        self._progress: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()
        self._results: Dict[str, Any] = {}
        self._trades: List[SimTrade] = []
        self._records_key: Optional[tuple] = None
        self._records: Dict[str, Dict[str, Any]] = {}
        self._taken: Dict[str, List[float]] = {}
        self._all_records: tuple = (None, {})           # (the results they were made from, the records)
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
              benchmark: Optional[str] = None,
              records: Optional[Mapping[str, Mapping[str, Any]]] = None,
              prepare: Optional[Callable[[Callable[[int, int], None]], Any]] = None,
              in_play: Optional[Callable[[Callable[[int, int], None]], Mapping[str, Sequence[dt.date]]]] = None,
              download_budget_s: Optional[float] = None) -> Dict[str, Any]:
        """``prepare``: called first, on the replay's thread, with a progress function - the engine
        downloads the long daily history there; ``in_play``: called next, the same way, for the
        sessions each stock was in play on (research/in_play.py) - day-trade setups are then
        replayed on those stock-days instead of on ``intraday_symbols`` over every session, which
        stay the fallback when it fails or finds nothing; ``market``: the turbulent regime's probability per day, from a model fitted before the
        given first day; ``earnings``: when each stock's earnings filings were accepted; ``news``:
        each stock's stored stories since a first day, for the news checks; ``benchmark``: the
        S&P 500 ETF's symbol, whose candles the market model behind those checks needs; ``records``:
        each strategy's pooled record, so the replayed plays state the same calibrated odds as live.
        All are asked for on the replay's own thread, and the replay goes on without them if they fail."""
        with self._lock:
            if self.running:
                return {"ok": False, "reason": "A replay is already running."}
            self._progress = {"stage": "starting", "done": 0, "total": 1}
            self._thread = threading.Thread(
                target=self._run, name="replay", daemon=True,
                args=(list(strategies), source, daily_frame, list(intraday_symbols), list(swing_symbols),
                      sessions, swing_sessions, settings, noise, con_ids or {}, market, earnings,
                      held_out_fraction, pairs, news, benchmark, dict(records or {}), prepare, in_play,
                      download_budget_s))
            self._thread.start()
        held = f"{held_out_fraction:.0%}"
        where = "the stocks in play each session" if in_play is not None else f"{len(intraday_symbols)} stocks"
        return {"ok": True, "note": (f"Replaying the last {sessions} sessions of day-trade setups on "
                                     f"{where} and {swing_sessions} sessions of swing "
                                     f"setups on {len(swing_symbols)}, holding out the latest {held} to test "
                                     "them on. It runs in the background.")}

    def wait(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self, strategies, source, daily_frame, intraday_symbols, swing_symbols, sessions, swing_sessions,
             settings, noise, con_ids, market, earnings, fraction, pairs=None, news=None, benchmark=None,
             records=None, prepare=None, in_play=None, download_budget_s=None) -> None:
        started = time.monotonic()
        try:
            self._optional("the long daily history", prepare,
                           lambda done, total: self._report("years of daily candles", done, total))
            last = clock.prev_trading_day(clock.session_date())
            days = {s: sorted(v) for s, v in (self._optional(
                "the stocks in play each session", in_play,
                lambda done, total: self._report("the stocks in play each session", done, total)) or {}).items() if v}
            pending = 0
            if days:
                intraday_symbols = list(days)
                report = lambda done, total: self._report("5-minute candles", done, total)   # noqa: E731
                if benchmark:                        # the market model reads the benchmark on every replayed session
                    self.history.load_days(source, {benchmark: clock.last_n_sessions(last, sessions)}, con_ids)
                bars = self.history.load_days(source, days, con_ids, progress=report, budget_s=download_budget_s)
                pending = self.history.pending
                if benchmark and (held := self.history.stored(benchmark)) is not None:
                    bars[benchmark] = held
            else:
                wanted = intraday_symbols + ([benchmark] if benchmark and benchmark not in intraday_symbols else [])
                bars = self.history.load(source, wanted, sessions, con_ids,
                                         progress=lambda done, total: self._report("5-minute candles", done, total))
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
            jobs = [("intraday", strategies, s, chunk, daily, settings, noise, n, regime, reports.get(s, ()),
                     stories.get(s, ()), bench_bars, bench_daily, records)
                    for s in intraday_symbols if s in bars and (daily := daily_frame(s)) is not None
                    for chunk, n in (day_chunks(bars[s], days[s], self.sessions_per_job) if days
                                     else session_chunks(bars[s], sessions, self.sessions_per_job))]
            jobs += [("swing", strategies, s, None, daily, settings, noise, swing_sessions, regime, (),
                      stories.get(s, ()), None, bench_daily, records)
                     for s in swing_symbols if (daily := daily_frame(s)) is not None]
            if pairs:
                # the pair desk forms and trades its pairs on the live store's year, so its replay does too
                frames = {s: f.tail(PAIR_BARS) for s in pairs["groups"] if (f := daily_frame(s)) is not None}
                jobs.append(("pairs", pairs, frames, min(swing_sessions, PAIR_SESSIONS)))
            trades = self._replay(jobs)
            data = {
                "ran_at": dt.datetime.now(dt.timezone.utc).isoformat(), "sessions": sessions,
                "swing_sessions": swing_sessions, "swing_symbols": len(swing_symbols),
                "intraday_symbols": sum(1 for s in intraday_symbols if s in bars),
                # with the stocks chosen session by session: how many stock-days the day-trade setups ran on
                "intraday_stock_days": sum(len(j[7]) for j in jobs if j[0] == "intraday") if days else None,
                "intraday_stock_days_in_play": sum(len(v) for v in days.values()) if days else None,
                "intraday_requests_pending": pending,
                "elapsed_s": round(time.monotonic() - started, 1), "held_out_from": split,
                "costs": {"slippage_bps": settings.slippage_bps, "commission_bps": settings.commission_bps},
                "market_regime_days": len(regime), "earnings_stocks": sum(1 for v in reports.values() if v),
                "news_stocks": sum(1 for v in stories.values() if v), "benchmark": benchmark if bench_daily is not None else None,
                "noise": noise_report(trades, split), "trades": [dataclasses.asdict(t) for t in trades],
            }
            self._save(data, trades)
            self._append_history(data, trades)
            if self.sink is not None:
                try:
                    self.sink(data, trades)
                except Exception:  # noqa: BLE001
                    log.warning("the replayed trades couldn't be kept in the database", exc_info=True)
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

    def records(self, skip_noise: Iterable[str], min_confirmations: int, min_reward_risk: float = 0.0,
                confidence_floors: Optional[Mapping[str, float]] = None) -> Dict[str, Dict[str, Any]]:
        """Each strategy's replayed record over the trades Autopilot would have taken - its skipped
        flags, its confirmations, its reward:risk floor and its confidence floors - with the
        held-out sessions' record. Asked for on every play, so kept until the settings or the
        results change."""
        self._refresh(skip_noise, min_confirmations, min_reward_risk, confidence_floors)
        return self._records

    def r_multiples(self, strategy: str, skip_noise: Iterable[str], min_confirmations: int,
                    min_reward_risk: float = 0.0, confidence_floors: Optional[Mapping[str, float]] = None) -> List[float]:
        """The R of every replayed trade of ``strategy`` that Autopilot would have taken."""
        self._refresh(skip_noise, min_confirmations, min_reward_risk, confidence_floors)
        return self._taken.get(strategy, [])

    def learned_skips(self) -> List[str]:
        return learned_skips(self._results.get("noise"))

    def _refresh(self, skip_noise: Iterable[str], min_confirmations: int, min_reward_risk: float = 0.0,
                 confidence_floors: Optional[Mapping[str, float]] = None) -> None:
        floors = tuple(sorted((confidence_floors or {}).items()))
        key = (tuple(sorted(skip_noise)), int(min_confirmations), float(min_reward_risk or 0.0), floors,
               self._results.get("ran_at"))
        if key == self._records_key:
            return
        chosen = taken(self._trades, key[0], min_confirmations, key[2], dict(floors))
        by_strategy: Dict[str, List[float]] = {}
        for t in chosen:
            by_strategy.setdefault(t.strategy, []).append(t.r)
        self._records = records_by_strategy(chosen, self.split)
        self._taken, self._records_key = by_strategy, key

    def state(self, skip_noise: Iterable[str], min_confirmations: int, min_reward_risk: float = 0.0,
              confidence_floors: Optional[Mapping[str, float]] = None) -> Dict[str, Any]:
        meta = {k: v for k, v in self._results.items() if k != "trades"}
        return {**meta, "running": self.running, "progress": self._progress, "trade_count": len(self._trades),
                "learned_skips": self.learned_skips(),
                "records": {"all": self._records_of_all(),
                            "autopilot": self.records(skip_noise, min_confirmations, min_reward_risk,
                                                      confidence_floors)}}

    def _records_of_all(self) -> Dict[str, Dict[str, Any]]:
        """Every replayed trade's records - judged for luck with a few thousand resamples, so made
        once per replay rather than on every look at the panel."""
        if self._all_records[0] != (self._results.get("ran_at"), len(self._trades)):
            self._all_records = ((self._results.get("ran_at"), len(self._trades)),
                                 strategy_records(self._trades, split=self.split))
        return self._all_records[1]

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


PAIR_BARS, PAIR_SESSIONS = 300, 250


def session_chunks(bars: pd.DataFrame, sessions: int, size: int) -> List[Tuple[pd.DataFrame, int]]:
    """Cut the last ``sessions`` sessions of a stock's 5-minute candles into jobs of ``size``
    sessions. Each job's frame also holds the ``LOOKBACK_SESSIONS`` sessions before its first,
    so every bar's rolling window is what the live scan would see; the job replays its last
    ``n`` sessions only. Returns (frame, n) pairs, oldest first."""
    days = sorted(set(bars.index.date))
    wanted = days[-sessions:] if sessions else days
    out: List[Tuple[pd.DataFrame, int]] = []
    for start in range(0, len(wanted), max(1, size)):
        chunk = wanted[start:start + max(1, size)]
        first = days.index(chunk[0])
        keep = set(days[max(0, first - LOOKBACK_SESSIONS):first]) | set(chunk)
        out.append((bars[[d in keep for d in bars.index.date]], len(chunk)))
    return out


def day_chunks(bars: pd.DataFrame, days: Sequence[dt.date], size: int) -> List[Tuple[pd.DataFrame, Tuple[dt.date, ...]]]:
    """Jobs for a stock replayed only on the sessions it was in play: ``size`` of those sessions a
    job, each with the ``LOOKBACK_SESSIONS`` before it for the rolling windows. Returns (frame,
    the sessions to replay) pairs - sessions the candles don't hold are left out."""
    held = sorted(set(bars.index.date))
    wanted = [d for d in sorted(days) if d in set(held)]
    out: List[Tuple[pd.DataFrame, Tuple[dt.date, ...]]] = []
    for start in range(0, len(wanted), max(1, size)):
        chunk = wanted[start:start + max(1, size)]
        keep = set(chunk)
        for day in chunk:
            at = held.index(day)
            keep.update(held[max(0, at - LOOKBACK_SESSIONS):at])
        out.append((bars[[d in keep for d in bars.index.date]], tuple(chunk)))
    return out


def replay_job(job: tuple) -> List[Dict[str, Any]]:
    """One stock's replay. It may run in a worker process, so it takes and returns plain data."""
    if job[0] == "pairs":
        from ..pairs.backtest import replay_pairs

        _, pairs, frames, sessions = job
        trades = replay_pairs(frames, pairs["groups"], pairs["rules"], pairs["finder"], sessions)
        return [dataclasses.asdict(t) for t in trades]
    (kind, strategies, symbol, bars, daily, settings, noise, sessions, market, earnings, news, bench_bars,
     bench_daily, records) = job
    trades = (replay_intraday(strategies, symbol, bars, daily, settings, noise, sessions, market, earnings,
                              news, bench_bars, bench_daily, records)
              if kind == "intraday" else replay_swing(strategies, symbol, daily, settings, noise, sessions, market,
                                                      news, bench_daily, records))
    return [dataclasses.asdict(t) for t in trades]
