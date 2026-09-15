"""The strategy replay on synthetic candles: fills, stops, targets, the flatten,
and what the records and the noise report say."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from tos_bot.core.enums import Side, StrategyKind, Timeframe
from tos_bot.research.replay import (ReplaySettings, SimTrade, noise_report, replay_intraday, strategy_records,
                                     summarize)
from tos_bot.scanner.noise import NoiseSettings
from tos_bot.strategies.base import Strategy

NY = "America/New_York"
DAY = dt.date(2026, 9, 10)                     # an ordinary full session
EXACT = ReplaySettings(slippage_bps=0.0, commission_bps=0.0, breakeven_at_r=0.0, trail_start_r=0.0)
QUIET = NoiseSettings(min_expected_r=-99.0)


class _LongAtBar(Strategy):
    """Buys at the close of one chosen bar of the session: stop 1 below, target 2 above."""

    key, kind, timeframe, title, thesis = "long_at_bar", StrategyKind.TECHNICAL, Timeframe.INTRADAY, "Test", "t"

    def __init__(self, at_bar: int = 5) -> None:
        super().__init__()
        self.at_bar = at_bar

    def generate(self, ctx):
        today = ctx.today_intraday()
        if today is None or len(today) != self.at_bar + 1:
            return []
        entry = float(today["close"].iloc[-1])
        play = self._mk_play(ctx, Side.LONG, entry, entry - 1.0, [entry + 2.0], 0.7, "r", "d", {}, tags=["intraday"])
        return [play] if play else []


def _session(bars):
    """A full session of (open, high, low, close) bars; after the given ones it goes quiet at the last close."""
    bars = list(bars)
    last = bars[-1][3]
    bars += [(last, last + 0.05, last - 0.05, last)] * (78 - len(bars))
    idx = pd.date_range(pd.Timestamp(f"{DAY} 09:30", tz=NY), periods=78, freq="5min")
    frame = pd.DataFrame(bars, columns=["open", "high", "low", "close"], index=idx)
    frame["volume"] = 5e5
    return frame


def _daily():
    idx = pd.bdate_range(end=pd.Timestamp(DAY - dt.timedelta(days=1)), periods=30, tz=NY)
    c = np.full(len(idx), 100.0)
    return pd.DataFrame({"open": c, "high": c + 0.5, "low": c - 0.5, "close": c, "volume": np.full(len(idx), 3e6)},
                        index=idx)


FLAT = [(100.0, 100.05, 99.95, 100.0)] * 6                 # the signal comes at the close of bar 5, at 100


def _replay(after_signal):
    return replay_intraday([_LongAtBar()], "RPL", _session(FLAT + after_signal), _daily(), EXACT, QUIET)


def test_a_target_hit_books_the_planned_reward():
    [t] = _replay([(100.0, 100.1, 99.9, 100.0), (100.2, 102.1, 100.1, 101.9)])
    assert (t.exit_reason, t.entry, t.exit, t.r) == ("target", 100.0, 102.0, 2.0)


def test_a_stop_hit_books_minus_one_r_and_a_bar_touching_both_counts_as_the_stop():
    [stopped] = _replay([(100.0, 100.1, 99.9, 100.0), (99.8, 99.9, 98.9, 99.0)])
    assert (stopped.exit_reason, stopped.r) == ("stop", -1.0)
    [both] = _replay([(100.0, 100.1, 99.9, 100.0), (100.0, 102.5, 98.5, 101.0)])
    assert (both.exit_reason, both.r) == ("stop", -1.0)


def test_an_open_day_trade_is_flattened_before_the_close():
    [t] = _replay([(100.0, 100.1, 99.9, 100.0)] + [(100.5, 100.6, 100.4, 100.5)] * 3)
    assert t.exit_reason == "eod-flatten" and t.r == 0.5 and t.exited_at.startswith(f"{DAY}T15:50")


def test_no_fill_when_the_next_open_has_already_run_away():
    assert _replay([(101.0, 101.1, 100.9, 101.0)]) == []


def _t(r, noise=(), confirmed=True, strategy="s"):
    return SimTrade(strategy=strategy, symbol="X", side="LONG", timeframe="INTRADAY", entered_at="",
                    exited_at="", entry=1.0, exit=1.0, r=r, exit_reason="x", noise=list(noise), confirmed=confirmed)


def test_a_record_shows_win_rate_expectancy_profit_factor_and_the_worst_drawdown():
    record = summarize([_t(2.0), _t(-1.0), _t(-1.0), _t(2.0)])
    assert (record["trades"], record["win_rate"], record["expectancy_r"], record["profit_factor"],
            record["worst_drawdown_r"]) == (4, 0.5, 0.5, 2.0, -2.0)


def test_the_noise_report_tells_a_check_that_removes_losers_from_one_that_removes_winners():
    useful = [_t(-1.0, ["against_trend"]) for _ in range(12)] + [_t(0.8) for _ in range(20)]
    report = noise_report(useful)
    assert report["against_trend"]["removes"] == 12 and report["against_trend"]["verdict"].startswith("helps")
    assert report["volume_against"]["verdict"] == "too few trades to tell"
    harmful = [_t(1.5, ["conflict"]) for _ in range(12)] + [_t(-0.2) for _ in range(20)]
    assert noise_report(harmful)["conflict"]["verdict"].startswith("hurts")


def test_a_strategy_record_counts_only_the_trades_autopilot_would_have_taken():
    trades = [_t(-1.0, ["against_trend"]), _t(1.0), _t(-1.0, confirmed=False), _t(2.0, strategy="other")]
    records = strategy_records(trades, skip_noise=["against_trend"], min_confirmations=2)
    assert records["s"]["trades"] == 1 and records["s"]["expectancy_r"] == 1.0
    assert strategy_records(trades)["s"]["trades"] == 3


def test_the_replay_runs_in_the_background_downloads_each_session_once_and_keeps_its_results(tmp_path):
    from types import SimpleNamespace

    import fakes
    from tos_bot.research.history import IntradayHistory
    from tos_bot.research.runner import ReplayRunner

    silent = SimpleNamespace(publish=lambda *a, **k: None)
    gateway = fakes.FakeGateway(["RPA", "RPB"])
    history = IntradayHistory(tmp_path / "intraday")
    runner = ReplayRunner(tmp_path / "replay.json", history, bus=silent, workers=1)
    started = runner.start(strategies=[_LongAtBar()], source=gateway, daily_frame=fakes.daily_bars,
                           intraday_symbols=["RPA", "RPB"], swing_symbols=["RPA"], sessions=3, swing_sessions=30,
                           settings=EXACT, noise=QUIET)
    runner.wait(60)
    state = runner.state([], 1)
    assert started["ok"] and state["ran_at"] and not state["running"] and "noise" in state

    asked = len(gateway.requests)
    history.load(gateway, ["RPA", "RPB"], 3)
    assert len(gateway.requests) == asked                                       # every session was on disk
    assert ReplayRunner(tmp_path / "replay.json", history, bus=silent, workers=1).state([], 1)["ran_at"] == state["ran_at"]


@pytest.mark.slow
def test_worker_processes_replay_exactly_what_one_process_does(tmp_path):
    from types import SimpleNamespace

    import fakes
    from tos_bot.research.history import IntradayHistory
    from tos_bot.research.runner import ReplayRunner
    from tos_bot.strategies import REGISTRY

    silent = SimpleNamespace(publish=lambda *a, **k: None)
    strategies = [REGISTRY[k]() for k in ("abcd_pattern", "vwap_reclaim", "rsi2_mean_reversion")]
    results = []
    for workers in (1, 2):
        runner = ReplayRunner(tmp_path / f"replay{workers}.json", IntradayHistory(tmp_path / f"bars{workers}"),
                              bus=silent, workers=workers)
        runner.start(strategies=strategies, source=fakes.FakeGateway(["WPA", "WPB"]), daily_frame=fakes.daily_bars,
                     intraday_symbols=["WPA", "WPB"], swing_symbols=["WPA", "WPB"], sessions=3, swing_sessions=40,
                     settings=ReplaySettings(), noise=NoiseSettings())
        runner.wait(300)
        results.append(runner.state([], 1))
    assert results[0]["ran_at"] and results[1]["ran_at"]
    assert results[0]["records"] == results[1]["records"] and results[0]["noise"] == results[1]["noise"]
