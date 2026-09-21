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
from tos_bot.data.market_data import quote_from_price
from tos_bot.strategies.base import Strategy, StrategyContext

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


class _LongTwoTargets(_LongAtBar):
    """The same, with a second target 4 above."""

    key = "long_two_targets"

    def generate(self, ctx):
        today = ctx.today_intraday()
        if today is None or len(today) != self.at_bar + 1:
            return []
        entry = float(today["close"].iloc[-1])
        play = self._mk_play(ctx, Side.LONG, entry, entry - 1.0, [entry + 2.0, entry + 4.0], 0.7, "r", "d", {},
                             tags=["intraday"])
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


def test_half_comes_off_at_the_first_target_and_the_rest_runs_on():
    bars = _session(FLAT + [(100.0, 100.1, 99.9, 100.0), (100.2, 102.1, 100.1, 101.9), (102.0, 104.2, 101.9, 104.0)])
    [t] = replay_intraday([_LongTwoTargets()], "RPL", bars, _daily(), EXACT, QUIET)
    assert (t.exit_reason, t.scaled, t.r) == ("target", True, 3.0)          # half at +2R, half at +4R
    # after the first target the stop sits at the entry: a fall back costs nothing on the rest
    bars = _session(FLAT + [(100.0, 100.1, 99.9, 100.0), (100.2, 102.1, 100.1, 101.9), (101.5, 101.6, 99.5, 99.6)])
    [t] = replay_intraday([_LongTwoTargets()], "RPL", bars, _daily(), EXACT, QUIET)
    assert (t.exit_reason, t.scaled, t.r) == ("trailing-stop", True, 1.0)
    # switched off, the position exits whole at the first target
    whole = ReplaySettings(slippage_bps=0.0, commission_bps=0.0, breakeven_at_r=0.0, trail_start_r=0.0, scale_out_pct=0.0)
    bars = _session(FLAT + [(100.0, 100.1, 99.9, 100.0), (100.2, 102.1, 100.1, 101.9), (102.0, 104.2, 101.9, 104.0)])
    [t] = replay_intraday([_LongTwoTargets()], "RPL", bars, _daily(), whole, QUIET)
    assert (t.exit_reason, t.scaled, t.r) == ("target", False, 2.0)


def test_a_stop_hit_books_minus_one_r_and_a_bar_touching_both_counts_as_the_stop():
    [stopped] = _replay([(100.0, 100.1, 99.9, 100.0), (99.8, 99.9, 98.9, 99.0)])
    assert (stopped.exit_reason, stopped.r) == ("stop", -1.0)
    [both] = _replay([(100.0, 100.1, 99.9, 100.0), (100.0, 102.5, 98.5, 101.0)])
    assert (both.exit_reason, both.r) == ("stop", -1.0)


def test_an_open_day_trade_is_flattened_before_the_close():
    import dataclasses

    no_time_stop = dataclasses.replace(EXACT, intraday_time_stop=False)      # the flatten alone
    [t] = replay_intraday([_LongAtBar()], "RPL", _session(FLAT + [(100.0, 100.1, 99.9, 100.0)]
                                                          + [(100.5, 100.6, 100.4, 100.5)] * 3),
                          _daily(), no_time_stop, QUIET)
    assert t.exit_reason == "eod-flatten" and t.r == 0.5 and t.exited_at.startswith(f"{DAY}T15:50")


class _LongHeld(_LongAtBar):
    """The same play, stated to take up to half an hour: six 5-minute bars."""

    key = "long_held"
    expected_hold = (15.0, 30.0)


def test_a_day_trade_that_isnt_working_is_closed_once_its_window_has_passed():
    # the signal at 100 (bar 5), filled at bar 6's open; then it drifts sideways, never near its target
    drift = [(100.0, 100.3, 99.6, 100.1)] * 12
    [t] = replay_intraday([_LongHeld()], "RPL", _session(FLAT + drift), _daily(), EXACT, QUIET)
    assert t.exit_reason == "time-stop"
    held = (pd.Timestamp(t.exited_at) - pd.Timestamp(t.entered_at)).total_seconds() / 60.0
    assert held == 30.0 and t.r == pytest.approx(0.1)                        # its half hour, at the bar's close


def test_a_day_trade_that_is_working_keeps_its_trail_past_its_window():
    import dataclasses

    # +1.5R early moves its stop past the entry (break-even at 1R); then it idles well past its half hour
    working = [(100.0, 101.5, 100.0, 101.2)] + [(101.2, 101.4, 101.1, 101.2)] * 20
    settings = dataclasses.replace(EXACT, breakeven_at_r=1.0, breakeven_lock_r=0.2)
    [t] = replay_intraday([_LongHeld()], "RPL", _session(FLAT + working), _daily(), settings, QUIET)
    assert t.exit_reason == "eod-flatten"                                     # held on: it can't lose any more
    off = dataclasses.replace(EXACT, intraday_time_stop=False)
    [idle] = replay_intraday([_LongHeld()], "RPL", _session(FLAT + [(100.0, 100.3, 99.6, 100.1)] * 12), _daily(),
                             off, QUIET)
    assert idle.exit_reason == "eod-flatten"                                  # switched off: the flatten as before


def _spy(day_prices):
    """The S&P 500 ETF: daily closes with a little noise, and a flat session of 5-minute candles."""
    rng = np.random.default_rng(3)
    idx = pd.bdate_range(end=pd.Timestamp(DAY - dt.timedelta(days=1)), periods=45, tz=NY)
    c = 500 * np.exp(np.cumsum(rng.normal(0.0, 0.006, len(idx))))
    daily = pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": np.full(len(idx), 5e7)}, index=idx)
    bars = _session([(day_prices, day_prices + 0.1, day_prices - 0.1, day_prices)] * 6)
    return daily, bars


def _noisy_daily():
    rng = np.random.default_rng(5)
    idx = pd.bdate_range(end=pd.Timestamp(DAY - dt.timedelta(days=1)), periods=45, tz=NY)
    c = 100 * np.exp(np.cumsum(rng.normal(0.0, 0.004, len(idx))))
    return pd.DataFrame({"open": c, "high": c + 0.5, "low": c - 0.5, "close": c, "volume": np.full(len(idx), 3e6)},
                        index=idx)


def test_the_news_checks_see_the_stories_out_by_each_bar_and_the_market_model():
    daily = _noisy_daily()
    prev = float(daily["close"].iloc[-1])
    jump = round(prev * 1.06, 2)                                            # today the stock is up 6% on its own
    spy_daily, spy_bars = _spy(500.0)
    session = _session([(jump, jump + 0.05, jump - 0.05, jump)] * 6 + [(jump, jump + 0.1, jump - 0.1, jump),
                                                                        (jump, jump + 2.5, jump - 0.1, jump + 2.2)])
    kw = dict(settings=EXACT, noise=NoiseSettings(min_expected_r=-99.0, abnormal_z=2.0), benchmark_bars=spy_bars,
              benchmark_daily=spy_daily)
    # no stories out: a momentum long chasing the move is flagged as a move without news
    [quiet] = replay_intraday([_LongAtBar()], "NWS", session, daily, news=[{"at": f"{DAY}T18:00:00+00:00"}], **kw)
    assert "move_without_news" in quiet.noise
    # a story published before the bar: the move came with news, so the flag is off
    [told] = replay_intraday([_LongAtBar()], "NWS", session, daily, news=[{"at": f"{DAY}T12:00:00+00:00"}], **kw)
    assert "move_without_news" not in told.noise
    # no stories given at all: as live, the check stays silent
    [unknown] = replay_intraday([_LongAtBar()], "NWS", session, daily, **kw)
    assert "move_without_news" not in unknown.noise
    # and without the benchmark there is no market model to read the move against
    [blind] = replay_intraday([_LongAtBar()], "NWS", session, daily, settings=EXACT, noise=QUIET,
                              news=[{"at": f"{DAY}T18:00:00+00:00"}])
    assert "move_without_news" not in blind.noise


def test_session_chunks_carry_the_lookback_and_replay_only_their_own_sessions():
    import fakes
    from tos_bot.research.runner import LOOKBACK_SESSIONS, session_chunks

    bars = fakes.intraday_bars("CHK")                            # five sessions
    days = sorted(set(bars.index.date))
    chunks = session_chunks(bars, sessions=4, size=3)
    assert [n for _, n in chunks] == [3, 1]
    first, last = chunks[0][0], chunks[1][0]
    assert sorted(set(first.index.date)) == days[max(0, 1 - LOOKBACK_SESSIONS):4]   # its 3 sessions and the ones before
    assert sorted(set(last.index.date))[-1] == days[-1] and len(set(last.index.date)) == 5
    # the chunks replay exactly what one job over the same sessions does
    whole = replay_intraday([_LongAtBar()], "CHK", bars, fakes.daily_bars("CHK"), EXACT, QUIET, sessions=4)
    parts = [t for frame, n in chunks
             for t in replay_intraday([_LongAtBar()], "CHK", frame, fakes.daily_bars("CHK"), EXACT, QUIET, sessions=n)]
    assert [(t.entered_at, t.r) for t in parts] == [(t.entered_at, t.r) for t in whole]


def test_series_shared_for_a_session_equal_the_ones_computed_per_bar():
    import fakes
    from tos_bot.indicators import ta
    from tos_bot.research.replay import session_series
    from tos_bot.strategies.technical import OpeningRangeBreakout

    history = fakes.intraday_bars("SHR")
    shared = session_series(history, [OpeningRangeBreakout()])
    assert set(shared) == {"session_vwap", "opening_range_5"}
    window = history.iloc[-200:-40]                                  # starts mid-session, like a rolling window
    ctx = StrategyContext(symbol="SHR", intraday=window, daily=fakes.daily_bars("SHR"),
                          quote=quote_from_price("SHR", float(window["close"].iloc[-1])), shared=shared)
    today = window.index.date == window.index[-1].date()             # the session the setups read: complete
    pd.testing.assert_series_equal(ctx.vwap_series[today], ta.session_vwap(window)[today], check_names=False)
    pd.testing.assert_frame_equal(ctx.opening_range(5)[today], ta.opening_range(window, 5)[today])
    # the window's first session is cut off at the front, and there the shared series is the true one
    first = window.index.date == window.index[0].date()
    assert not ctx.vwap_series[first].equals(ta.session_vwap(window)[first])
    plain = StrategyContext(symbol="SHR", intraday=window, daily=fakes.daily_bars("SHR"),
                            quote=quote_from_price("SHR", float(window["close"].iloc[-1])))
    pd.testing.assert_series_equal(plain.vwap_series[today], ctx.vwap_series[today], check_names=False)


def test_the_replay_leaves_out_the_plays_the_board_would_never_show():
    import fakes

    bars, daily = fakes.intraday_bars("FLR"), fakes.daily_bars("FLR")
    shown = replay_intraday([_LongAtBar()], "FLR", bars, daily, EXACT, QUIET, sessions=2)      # 2:1 plays
    assert shown
    floor = ReplaySettings(slippage_bps=0.0, commission_bps=0.0, breakeven_at_r=0.0, trail_start_r=0.0,
                           min_reward_risk=3.0)
    assert replay_intraday([_LongAtBar()], "FLR", bars, daily, floor, QUIET, sessions=2) == []
    from tos_bot.config import get_settings
    assert ReplaySettings.from_exit_rules(get_settings().config.exit_manager, None, 1.5).min_reward_risk == 1.5


def test_the_records_count_only_what_autopilot_would_take():
    from tos_bot.research.replay import taken

    def sim(rr, conf, tf="INTRADAY", noise=(), confirmed=True):
        return SimTrade(strategy="s", symbol="TKN", side="LONG", timeframe=tf, entered_at="2026-09-10T14:00:00",
                        exited_at="2026-09-10T15:00:00", entry=100.0, exit=101.0, r=1.0, exit_reason="target",
                        noise=list(noise), confirmed=confirmed, features={"reward_risk": rr, "confidence": conf})

    trades = [sim(2.5, 0.7), sim(1.2, 0.7), sim(2.5, 0.4), sim(2.5, 0.55, tf="SWING"), sim(2.5, 0.7, noise=["against_gap"]),
              SimTrade(strategy="s", symbol="OLD", side="LONG", timeframe="INTRADAY", entered_at="2026-09-10T14:00:00",
                       exited_at="2026-09-10T15:00:00", entry=100.0, exit=101.0, r=1.0, exit_reason="target")]
    assert len(taken(trades)) == 6                                                     # no floors: everything
    kept = taken(trades, skip_noise=["against_gap"], min_reward_risk=2.0,
                 confidence_floors={"INTRADAY": 0.6, "SWING": 0.5})
    assert [(t.symbol, t.features.get("reward_risk")) for t in kept] == [("TKN", 2.5), ("TKN", 2.5), ("OLD", None)]
    assert kept[1].timeframe == "SWING"                                                # 0.55 clears the swing floor


def test_the_replayed_plays_state_the_calibrated_odds_like_live():
    import fakes

    bars, daily = fakes.intraday_bars("ODD"), fakes.daily_bars("ODD")
    plain = replay_intraday([_LongAtBar()], "ODD", bars, daily, EXACT, QUIET, sessions=2)
    leaning = replay_intraday([_LongAtBar()], "ODD", bars, daily, EXACT, QUIET, sessions=2,
                              records={"long_at_bar": {"trades": 300, "win_rate": 0.3}})
    assert plain and len(plain) == len(leaning)
    assert plain[0].features["probability"] > leaning[0].features["probability"]      # shrunk toward the record
    assert plain[0].features["confidence"] == leaning[0].features["confidence"]       # the setup's own view is unchanged


def test_no_fill_when_the_next_open_has_already_run_away():
    assert _replay([(101.0, 101.1, 100.9, 101.0)]) == []


def _t(r, noise=(), confirmed=True, strategy="s", entry_rule="first"):
    return SimTrade(strategy=strategy, symbol="X", side="LONG", timeframe="INTRADAY", entered_at="",
                    exited_at="", entry=1.0, exit=1.0, r=r, exit_reason="x", noise=list(noise), confirmed=confirmed,
                    entry_rule=entry_rule)


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
    on_sight = [_t(-1.0, ["against_trend"]), _t(1.0), _t(-1.0, confirmed=False), _t(2.0, strategy="other")]
    its_way = [_t(0.5, entry_rule="second"), _t(-0.5, ["against_trend"], entry_rule="second")]
    trades = on_sight + its_way
    # asking for two scans in a row, Autopilot's record is the entries made after two bars - not the ones on sight
    records = strategy_records(trades, skip_noise=["against_trend"], min_confirmations=2)
    assert records["s"]["trades"] == 1 and records["s"]["expectancy_r"] == 0.5 and "other" not in records
    # taking plays on sight, it is the entries on sight; and the record of every trade counts each setup once
    assert strategy_records(trades, skip_noise=["against_trend"])["s"]["expectancy_r"] == 0.0
    assert strategy_records(trades)["s"]["trades"] == 3 and strategy_records(trades)["other"]["trades"] == 1
    swing = SimTrade(strategy="w", symbol="X", side="LONG", timeframe="SWING", entered_at="", exited_at="", entry=1.0,
                     exit=1.0, r=1.0, exit_reason="x")
    assert strategy_records([swing], min_confirmations=2)["w"]["trades"] == 1        # a swing trade has one way in
    assert noise_report(trades)["against_trend"]["removes"] == 1                      # measured on the entries on sight


class _LongWhileQuiet(Strategy):
    """Shows on every bar from the fifth on: stop 1 below the close, target 2 above."""

    key, kind, timeframe, title, thesis = "long_while_quiet", StrategyKind.TECHNICAL, Timeframe.INTRADAY, "Test", "t"

    def generate(self, ctx):
        today = ctx.today_intraday()
        if today is None or len(today) < 6:
            return []
        entry = float(today["close"].iloc[-1])
        play = self._mk_play(ctx, Side.LONG, entry, entry - 1.0, [entry + 2.0], 0.7, "r", "d", {}, tags=["intraday"])
        return [play] if play else []


def test_a_day_setup_is_also_entered_the_way_autopilot_enters_it_after_two_bars_running():
    # the setup shows at the close of bars 5, 6, 7...: on sight it fills at bar 6's open, Autopilot's way at bar 7's
    bars = FLAT + [(100.0, 100.3, 99.9, 100.2), (100.4, 100.6, 100.3, 100.5), (100.5, 103.0, 100.4, 102.8)]
    trades = replay_intraday([_LongWhileQuiet()], "RPL", _session(bars), _daily(), EXACT, QUIET)
    first = [t for t in trades if t.entry_rule == "first"]
    second = [t for t in trades if t.entry_rule == "second"]
    assert first[0].entry == pytest.approx(100.0) and first[0].features["confirmations"] == 1
    assert len(second) == 1                                                   # once a session, like a settled setup
    assert second[0].entry == pytest.approx(100.4) and second[0].features["confirmations"] == 2 and second[0].confirmed
    assert pd.Timestamp(second[0].entered_at) > pd.Timestamp(first[0].entered_at)
    # a setup that shows on one bar only is never entered Autopilot's way
    once = replay_intraday([_LongAtBar()], "RPL", _session(bars), _daily(), EXACT, QUIET)
    assert [t.entry_rule for t in once] == ["first"]


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


def test_however_long_the_history_a_replayed_swing_setup_sees_what_it_would_see_live():
    import fakes
    from tos_bot.research.replay import LIVE_DAILY_BARS, replay_swing

    class _Watcher(Strategy):
        key, kind, timeframe, title, thesis = "watcher", StrategyKind.TECHNICAL, Timeframe.SWING, "Test", "t"
        seen: list = []

        def generate(self, ctx):
            self.seen.append(len(ctx.daily))
            return []

    watcher = _Watcher()
    replay_swing([watcher], "LNG", fakes.daily_bars("LNG", 756), EXACT, QUIET, sessions=700)
    assert len(watcher.seen) == 756 - 60 - 1                                    # three years of sessions replayed...
    assert max(watcher.seen) == LIVE_DAILY_BARS and watcher.seen[0] == 61       # ...each on the window the scans have


def test_the_replay_prepares_its_long_history_first_and_runs_on_without_it_if_that_fails(tmp_path):
    from types import SimpleNamespace

    import fakes
    from tos_bot.research.history import IntradayHistory
    from tos_bot.research.runner import ReplayRunner

    silent = SimpleNamespace(publish=lambda *a, **k: None)
    calls = []

    def prepare(progress):
        progress(1, 1)
        calls.append("prepared")

    def broken(progress):
        raise RuntimeError("no connection")

    for n, step in enumerate((prepare, broken)):
        runner = ReplayRunner(tmp_path / f"replay{n}.json", IntradayHistory(tmp_path / f"intraday{n}"), bus=silent,
                              workers=1)
        runner.start(strategies=[_LongAtBar()], source=fakes.FakeGateway(["PRP"]), daily_frame=fakes.daily_bars,
                     intraday_symbols=["PRP"], swing_symbols=["PRP"], sessions=3, swing_sessions=30,
                     settings=EXACT, noise=QUIET, prepare=step)
        runner.wait(60)
        assert runner.state([], 1)["ran_at"]
    assert calls == ["prepared"]
