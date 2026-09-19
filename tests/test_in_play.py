"""The day-trade replay's stocks: chosen session by session the way the morning scan would have
(research/in_play.py), their candles downloaded for those sessions only, and replayed on them alone."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from test_replay import EXACT, QUIET, _LongAtBar
from tos_bot.research.history import IntradayHistory
from tos_bot.research.in_play import in_play, metrics_history
from tos_bot.research.runner import ReplayRunner, day_chunks
from tos_bot.scanner.heat import daily_metrics, liquid, rank_by_daily_heat
from tos_bot.util import clock

NY = "America/New_York"
LAST = dt.date(2026, 9, 10)
SESSIONS = list(clock.last_n_sessions(LAST, 90))


def _daily(seed: int, *, price=50.0, volume=2e6, burst_on: dt.date = None, gap_on: dt.date = None) -> pd.DataFrame:
    """Ninety quiet sessions; ``burst_on``: five times the volume and a big move that day;
    ``gap_on``: that day opens 6 % up."""
    rng = np.random.default_rng(seed)
    close = price * np.exp(np.cumsum(rng.normal(0, 0.01, len(SESSIONS))))
    frame = pd.DataFrame({"open": close * (1 + rng.normal(0, 0.002, len(SESSIONS))), "close": close,
                          "volume": volume * (1 + rng.uniform(-0.2, 0.2, len(SESSIONS)))},
                         index=pd.DatetimeIndex([pd.Timestamp(d, tz=NY) for d in SESSIONS]))
    if burst_on is not None:
        at = SESSIONS.index(burst_on)
        frame.iloc[at, frame.columns.get_loc("volume")] *= 5
        frame.iloc[at:, frame.columns.get_loc("close")] *= 1.08
        frame.iloc[at + 1:, frame.columns.get_loc("open")] *= 1.08
    if gap_on is not None:
        frame.iloc[SESSIONS.index(gap_on), frame.columns.get_loc("open")] = frame["close"].iloc[SESSIONS.index(gap_on) - 1] * 1.06
    frame["high"] = frame[["open", "close"]].max(axis=1) * 1.01
    frame["low"] = frame[["open", "close"]].min(axis=1) * 0.99
    return frame[["open", "high", "low", "close", "volume"]]


def test_every_sessions_metrics_are_the_ones_the_scan_would_have_read_that_evening():
    daily = _daily(1, burst_on=SESSIONS[70])
    history = metrics_history(daily)
    assert history.index[0] == SESSIONS[29]                                 # thirty sessions before it is ranked
    for day in (SESSIONS[29], SESSIONS[45], SESSIONS[70], SESSIONS[-1]):
        live = daily_metrics("X", daily[daily.index.date <= day])
        row = history.loc[day]
        for field in ("price", "dollar_volume", "atr_pct", "rvol", "move_atr", "extreme"):
            assert row[field] == pytest.approx(getattr(live, field), rel=1e-9), (day, field)
    assert metrics_history(daily.head(20)) is None


def test_a_sessions_stocks_are_picked_from_what_was_known_before_its_open():
    burst, gap = SESSIONS[80], SESSIONS[84]
    frames = {f"Q{i}": _daily(10 + i) for i in range(12)}
    frames["HOT"] = _daily(3, burst_on=burst)                               # explodes ON the burst session
    frames["GAP"] = _daily(4, gap_on=gap)
    frames["THIN"] = _daily(5, volume=2e4, burst_on=burst)                  # the same burst, but too thin to trade
    replayed = SESSIONS[78:88]
    picked = in_play(frames.get, frames, replayed, hot=2, gappers=1, min_gap_pct=2.0,
                     prefilter={"min_dollar_volume": 5e6, "min_price": 3.0})
    after = SESSIONS[81]
    assert burst not in picked["HOT"] and after in picked["HOT"]            # seen the morning after, never the day itself
    assert picked["GAP"] == [gap] or gap in picked["GAP"]                   # its open is known at 09:30
    assert "THIN" not in picked
    per_day = {d: [s for s, days in picked.items() if d in days] for d in replayed}
    assert all(2 <= len(v) <= 3 for v in per_day.values())                  # the two hottest, and a gapper when there is one

    # the same morning, ranked the scanner's own way
    day = after
    ranked = rank_by_daily_heat([m for s, f in frames.items()
                                 if (m := daily_metrics(s, f[f.index.date < day])) is not None
                                 and liquid(m, {"min_dollar_volume": 5e6, "min_price": 3.0})])
    assert {m.symbol for m in ranked[:2]} == set(in_play(frames.get, frames, [day], hot=2,
                                                         prefilter={"min_dollar_volume": 5e6, "min_price": 3.0}))
    assert in_play(frames.get, frames, replayed, hot=0) == {}


class _Candles:
    """Serves five sessions of 5-minute candles ending at ``end``; ``nothing_for`` has none at all."""

    is_connected = True

    def __init__(self, nothing_for=()):
        self.requests, self.nothing_for = [], set(nothing_for)

    def history_many(self, requests, con_ids=None, end=None, rth=True):
        out = {}
        for symbol, (bar, duration) in requests.items():
            self.requests.append((symbol, end.date(), duration))
            if symbol in self.nothing_for:
                continue
            frames = []
            for day in clock.last_n_sessions(end.date(), 5):
                idx = pd.date_range(pd.Timestamp(f"{day} 09:30", tz=NY), periods=78, freq="5min")
                c = np.full(78, 100.0)
                frames.append(pd.DataFrame({"open": c, "high": c + 0.05, "low": c - 0.05, "close": c,
                                            "volume": np.full(78, 5e5)}, index=idx))
            out[symbol] = pd.concat(frames)
        return out


def test_candles_are_downloaded_for_the_sessions_in_play_and_the_four_before_once(tmp_path):
    history, source = IntradayHistory(tmp_path), _Candles(nothing_for={"GONE"})
    lone, run = [SESSIONS[60]], SESSIONS[70:77]                             # one session; seven in a row
    bars = history.load_days(source, {"AAA": lone, "BBB": run, "GONE": lone})
    asked = {}
    for symbol, end, duration in source.requests:
        asked.setdefault(symbol, []).append(end)
        assert duration == "5 D"
    assert asked["AAA"] == [SESSIONS[60]]                                   # the session and the four before: one request
    assert set(bars["AAA"].index.date) == set(SESSIONS[56:61])
    assert len(asked["BBB"]) == 3 and set(SESSIONS[66:77]) <= set(bars["BBB"].index.date)   # eleven sessions, three requests
    assert "GONE" not in bars

    again = _Candles(nothing_for={"GONE"})
    history.load_days(again, {"AAA": lone, "BBB": run, "GONE": lone})
    assert again.requests == []                                             # on disk - and what IBKR lacked isn't asked again
    history.load_days(again, {"AAA": [SESSIONS[62]]})
    assert [(s, e) for s, e, _ in again.requests] == [("AAA", SESSIONS[62])]  # only the sessions still missing


def test_a_stock_is_replayed_on_its_sessions_in_play_with_the_sessions_before_to_look_back_over(tmp_path):
    source = _Candles()
    bars = IntradayHistory(tmp_path).load_days(source, {"AAA": [SESSIONS[60], SESSIONS[70], SESSIONS[71]]})["AAA"]
    jobs = day_chunks(bars, [SESSIONS[60], SESSIONS[70], SESSIONS[71], SESSIONS[5]], size=2)
    assert [days for _, days in jobs] == [(SESSIONS[60], SESSIONS[70]), (SESSIONS[71],)]    # no candles, no replay
    assert set(jobs[1][0].index.date) == set(SESSIONS[66:72])                # the session and the ones held before it

    from tos_bot.research.replay import replay_intraday

    daily = pd.DataFrame({"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 3e6},
                         index=pd.DatetimeIndex([pd.Timestamp(d, tz=NY) for d in SESSIONS]))
    trades = replay_intraday([_LongAtBar()], "AAA", jobs[0][0], daily, EXACT, QUIET, sessions=jobs[0][1])
    assert sorted({pd.Timestamp(t.entered_at).date() for t in trades}) == [SESSIONS[60], SESSIONS[70]]


def test_the_runner_replays_day_trades_on_the_stocks_in_play_and_falls_back_when_it_cant_tell(tmp_path, monkeypatch):
    import fakes

    real = clock.session_date                    # "now" is the session after LAST; a candle's own time still tells its day
    monkeypatch.setattr(clock, "session_date", lambda ts=None: real(ts) if ts is not None else clock.next_trading_day(LAST))
    silent = SimpleNamespace(publish=lambda *a, **k: None)
    daily = pd.DataFrame({"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 3e6},
                         index=pd.DatetimeIndex([pd.Timestamp(d, tz=NY) for d in SESSIONS]))
    source = _Candles()
    runner = ReplayRunner(tmp_path / "replay.json", IntradayHistory(tmp_path / "bars"), bus=silent, workers=1)
    plan = {"AAA": [SESSIONS[-1], SESSIONS[-3]], "BBB": [SESSIONS[-2]]}
    started = runner.start(strategies=[_LongAtBar()], source=source, daily_frame=lambda s: daily,
                           intraday_symbols=["ZZZ"], swing_symbols=[], sessions=5, swing_sessions=20,
                           settings=EXACT, noise=QUIET, in_play=lambda progress: plan)
    runner.wait(60)
    state = runner.state([], 1)
    assert started["ok"] and "in play" in started["note"]
    assert (state["intraday_symbols"], state["intraday_stock_days"]) == (2, 3)
    assert state["records"]["all"]["long_at_bar"]["trades"] == 3             # one a stock-day, none on ZZZ
    assert not any(symbol == "ZZZ" for symbol, _, _ in source.requests)

    def broken(progress):
        raise RuntimeError("no daily candles")

    gateway = fakes.FakeGateway(["RPA"])
    fallback = ReplayRunner(tmp_path / "replay2.json", IntradayHistory(tmp_path / "bars2"), bus=silent, workers=1)
    fallback.start(strategies=[_LongAtBar()], source=gateway, daily_frame=fakes.daily_bars, intraday_symbols=["RPA"],
                   swing_symbols=[], sessions=3, swing_sessions=20, settings=EXACT, noise=QUIET, in_play=broken)
    fallback.wait(60)
    state = fallback.state([], 1)
    assert state["ran_at"] and state["intraday_symbols"] == 1 and state["intraday_stock_days"] is None
