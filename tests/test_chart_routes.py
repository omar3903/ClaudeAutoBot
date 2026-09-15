"""The chart behind a play and the ways its trade can end."""

from __future__ import annotations

import numpy as np
import pandas as pd

from tos_bot.config import ExitManagerCfg
from tos_bot.core.enums import Side, StrategyKind, Timeframe
from tos_bot.core.models import Play
from tos_bot.engine.chart import candles, chart_payload, exit_routes
from tos_bot.util import clock


def _play(side=Side.LONG, timeframe=Timeframe.INTRADAY, entry=100.0, stop=98.0, target=106.0):
    return Play(symbol="AAA", side=side, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL, timeframe=timeframe,
                entry=entry, stop=stop, targets=[target])


def test_a_long_day_trade_can_end_five_ways():
    routes = {r["key"]: r for r in exit_routes(_play(), ExitManagerCfg())}
    assert list(routes) == ["target", "stop", "breakeven", "trail", "time"]
    assert (routes["target"]["price"], routes["target"]["r"]) == (106.0, 3.0)
    assert (routes["stop"]["price"], routes["stop"]["r"]) == (98.0, -1.0)
    assert (routes["breakeven"]["trigger"], routes["breakeven"]["price"]) == (102.6, 100.65)   # +1.3R, then +0.3R + 5bp
    assert (routes["trail"]["trigger"], routes["trail"]["price"], routes["trail"]["r"]) == (104.0, 102.0, 1.0)
    assert routes["time"]["label"] == "Before the close" and routes["time"]["price"] is None


def test_a_short_swing_trade_reads_the_other_way_and_ends_on_a_time_stop():
    routes = {r["key"]: r for r in exit_routes(_play(Side.SHORT, Timeframe.SWING, 50.0, 52.0, 47.0), ExitManagerCfg())}
    assert list(routes) == ["target", "stop", "breakeven", "time"]          # a 1.5R target ends before trailing starts
    assert (routes["breakeven"]["trigger"], routes["breakeven"]["price"]) == (47.4, 49.375)
    assert "bought back for about +1.5R" in routes["target"]["how"]
    assert routes["time"]["label"] == "Time stop" and "after 10 days" in routes["time"]["how"]


def test_with_automatic_exits_off_only_the_stop_and_target_apply():
    assert [r["key"] for r in exit_routes(_play(), ExitManagerCfg(enabled=False))] == ["target", "stop"]


def test_the_chart_carries_the_latest_candles_and_the_play_levels():
    index = pd.date_range("2026-09-14 09:30", periods=200, freq="5min", tz=clock.NY)
    close = np.linspace(99.0, 101.0, 200)
    frame = pd.DataFrame({"open": close, "high": close + 0.2, "low": close - 0.2, "close": close,
                          "volume": np.full(200, 1000.0)}, index=index)
    rows = candles(frame, 156)
    assert len(rows) == 156 and rows[-1]["t"] == index[-1].isoformat() and set(rows[0]) == {"t", "o", "h", "l", "c", "v"}
    payload = chart_payload(_play(), frame, True, ExitManagerCfg())
    assert payload["ok"] and payload["levels"] == {"entry": 100.0, "stop": 98.0, "targets": [106.0]}
    assert len(payload["candles"]) == 156 and payload["routes"]
