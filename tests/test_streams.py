"""Real-time streams: which stocks stream within the line budget, and when quote() serves a streamed price
instead of asking for a snapshot."""

from __future__ import annotations

import datetime as dt
import time

import pytest

import fakes
from tos_bot.config import ExecutionCfg
from tos_bot.data.bars import DailyBarStore
from tos_bot.data.market_data import MarketData, NoDataSource, quote_from_price
from tos_bot.data.streams import StreamManager
from tos_bot.util import clock


def _streaming(tmp_path, symbols=("AAA", "BBB"), **kwargs):
    gateway, md = fakes.StreamingGateway(list(symbols), **kwargs), MarketData(DailyBarStore(tmp_path))
    gateway.connect()
    md.attach(gateway)
    return gateway, md


def _snapshot_price(symbol: str) -> float:
    return pytest.approx(float(fakes.intraday_bars(symbol)["close"].iloc[-1]), abs=1e-4)


# ---------------------------------------------------------------- quote() and the stream
def test_a_fresh_stream_is_served_without_asking_for_a_snapshot(tmp_path):
    gateway, md = _streaming(tmp_path)
    assert md.streams.sync(["AAA"], [], 5) == ["AAA"]
    streamed = gateway.tick("AAA", 10.0, age_s=1.5)
    q = md.quote("AAA")
    assert q == streamed and q.source == "stream" and gateway.snapshots == 0
    assert "AAA" not in md._quotes                      # never kept: the fallback when a snapshot fails is a snapshot


def test_a_stream_quiet_for_over_two_seconds_or_a_stock_not_streamed_costs_one_snapshot(tmp_path):
    gateway, md = _streaming(tmp_path)
    md.streams.sync(["AAA"], [], 5)
    gateway.tick("AAA", 10.0, age_s=2.5)
    assert md.quote("AAA").last == _snapshot_price("AAA") and gateway.snapshots == 1
    assert md.quote("BBB").last == _snapshot_price("BBB") and gateway.snapshots == 2


def test_on_delayed_data_quotes_come_from_candles_and_no_stream_is_read(tmp_path, monkeypatch):
    gateway, md = _streaming(tmp_path, delayed=True)
    read = []
    monkeypatch.setattr(gateway, "streamed_quote", lambda symbol: read.append(symbol))
    assert md.streams.sync(["AAA"], [], 5) == [] and gateway.streams == []
    md.quote("AAA")
    assert [bar for _, bar, _ in gateway.requests] == ["1 min"] and gateway.snapshots == 0 and read == []


def test_a_source_that_cant_stream_prices_as_before(tmp_path):
    gateway, md = fakes.FakeGateway(["AAA"]), MarketData(DailyBarStore(tmp_path))
    md.attach(gateway)
    assert md.streams.sync(["AAA"], ["BBB"], 60) == [] and md.streams.latest("AAA") is None
    assert md.quote("AAA").last == _snapshot_price("AAA") and md.last_seen("AAA")[2] < 5


# ---------------------------------------------------------------- which stocks stream
def test_the_positions_come_first_and_a_stock_counts_once_within_the_lines(tmp_path):
    gateway, md = _streaming(tmp_path, fakes.SYMBOLS)
    assert md.streams.sync(["T01", "T02", "T03"], ["T04"], 2) == ["T01", "T02"]
    assert md.streams.sync(["T01", "T01"], ["T01", "T04", "T04"], 2) == ["T01", "T04"]
    assert gateway.stream_calls[-1] == (["T01", "T04"], 2)
    assert md.streams.sync(["T01"], ["T04"], 0) == [] and gateway.streams == []


def test_a_play_keeps_its_stream_a_minute_while_its_on_offer(tmp_path):
    gateway, md = _streaming(tmp_path, fakes.SYMBOLS)
    held = ["T01"]
    assert md.streams.sync(held, ["T02", "T03"], 2) == ["T01", "T02"]
    assert md.streams.sync(held, ["T03", "T02"], 2) == ["T01", "T02"]       # re-ranked: T02 streamed for under a minute
    assert md.streams.sync(["T01", "T05"], ["T02"], 2) == ["T01", "T05"]    # ...but a position always goes first
    assert md.streams.sync(held, ["T03", "T02"], 2) == ["T01", "T03"]       # T02 lost its line: it starts over

    md.streams._since["T03"] -= StreamManager.PLAY_MIN_HOLD_S                # a minute on
    assert md.streams.sync(held, ["T02", "T03"], 2) == ["T01", "T02"]       # the better play takes the line
    assert md.streams.sync(held, ["T04"], 2) == ["T01", "T04"]              # T02 off the board: its line goes at once


def test_the_plays_opened_on_the_dashboard_go_first_then_the_ones_autopilot_would_take(tmp_path):
    gateway, md = _streaming(tmp_path, fakes.SYMBOLS)
    plays = ["T02", "T03", "T04", "T05"]
    assert md.streams.sync(["T01"], plays, 3) == ["T01", "T02", "T03"]
    # Autopilot would take T05: it goes ahead of the plays streaming for under a minute; T09 is off the board
    assert md.streams.sync(["T01"], plays, 3, candidates=["T05", "T09"]) == ["T01", "T05", "T02"]
    md.streams.prefer("T04")                                                # the operator opened a play on T04...
    md.streams.prefer("T09")                                                # ...one that has left the board since...
    md.streams.prefer("T01")                                                # ...and one on a stock held
    assert md.streams.sync(["T01"], plays, 3, candidates=["T05"]) == ["T01", "T04", "T05"]
    assert gateway.stream_calls[-1] == (["T01", "T04", "T05", "T02", "T03"], 3)   # T01 once, T09 not at all

    for symbol in ("T06", "T07", "T08", "T09", "T10", "T11"):
        md.streams.prefer(symbol)
    assert md.streams.preferred() == ["T11", "T10", "T09", "T08", "T07"]    # the five opened last, the latest first
    md.streams._preferred["T11"] -= StreamManager.PREFER_S                  # five minutes on
    assert md.streams.preferred() == ["T10", "T09", "T08", "T07"]
    md.streams.prefer("T08")                                                # opened again: the latest
    assert md.streams.preferred() == ["T08", "T10", "T09", "T07"]


def test_the_watch_tier_streams_after_the_plays_within_the_lines(tmp_path):
    gateway, md = _streaming(tmp_path, fakes.SYMBOLS)
    # T03 is a play already: it streams once, as a play; the tier fills the lines left, in the order given
    assert md.streams.sync(["T01"], ["T02", "T03"], 5, watch=["T03", "T04", "", "T05", "T06"]) == [
        "T01", "T02", "T03", "T04", "T05"]
    assert gateway.stream_calls[-1] == (["T01", "T02", "T03", "T04", "T05", "T06"], 5)
    assert gateway.protect == 1                                             # 101 never takes the position's line
    # a watch name has no minute's hold: the tier re-ranked, T04 gives its line up at once
    assert md.streams.sync(["T01"], ["T02", "T03"], 5, watch=["T06", "T05", "T04"]) == [
        "T01", "T02", "T03", "T06", "T05"]
    # no watch tier: the same call as without one
    md.streams.sync(["T01", "T07"], ["T02", "T03"], 5)
    without = gateway.stream_calls[-1]
    md.streams.sync(["T01", "T07"], ["T02", "T03"], 5, watch=())
    assert gateway.stream_calls[-1] == without == (["T01", "T07", "T02", "T03"], 5) and gateway.protect == 2


def test_a_dropped_connection_prices_as_before_and_the_streams_are_asked_for_again(tmp_path):
    gateway, md = _streaming(tmp_path)
    md.streams.sync(["AAA"], [], 5)
    gateway.tick("AAA", 10.0)
    gateway.close()
    assert md.quote("AAA").last == _snapshot_price("AAA") and gateway.snapshots == 1
    assert md.streams.sync(["AAA"], [], 5) == []
    gateway.connect()
    assert md.streams.sync(["AAA"], [], 5) == ["AAA"]
    assert md.quote("AAA").last == _snapshot_price("AAA")                   # no price streamed yet: a snapshot

    md.detach()
    assert gateway.on_tick is None
    with pytest.raises(NoDataSource):
        md.quote("AAA")
    md.attach(gateway)
    md.streams.sync(["AAA"], [], 5)
    gateway.tick("AAA", 12.0)
    assert md.quote("AAA").last == 12.0


def test_a_swapped_source_never_serves_the_old_ones_stream(tmp_path):
    old, md = _streaming(tmp_path, ["AAA"])
    md.streams.sync(["AAA"], [], 5)
    old.tick("AAA", 10.0)
    new = fakes.StreamingGateway(["AAA"])
    new.connect()
    md.attach(new)
    assert old.on_tick is None and md.streams.latest("AAA") is None and md.last_seen("AAA") is None
    assert md.quote("AAA").last == _snapshot_price("AAA") and (old.snapshots, new.snapshots) == (0, 1)
    md.streams.sync(["AAA"], [], 5)
    assert new.on_tick == md.streams._on_ticks
    new.tick("AAA", 11.0)
    assert md.quote("AAA").last == 11.0


def test_ticks_reach_every_listener_even_when_one_fails(tmp_path):
    gateway, md = _streaming(tmp_path)
    heard = []

    def broken(symbols):
        raise RuntimeError("a listener with a bug")

    md.streams.add_listener(broken)
    md.streams.add_listener(heard.append)
    md.streams.sync(["AAA"], [], 5)
    gateway.tick("AAA", 10.0)
    assert heard == [frozenset({"AAA"})] and md.streams._moved == {"AAA"}
    md.attach(gateway)                                   # a new connection starts clean
    assert md.streams._moved == set()


def test_the_streamed_ticks_build_live_candles_and_a_stock_no_longer_streamed_is_forgotten(tmp_path):
    gateway, md = _streaming(tmp_path, fakes.SYMBOLS)
    md.streams.sync(["T01"], ["T02"], 5)
    at = dt.datetime(2026, 9, 24, 10, 0, 20, tzinfo=clock.NY)
    assert clock.is_trading_day(at.date())

    def ago(sec: float) -> float:                        # the age that stamps a tick ``sec`` after ``at``
        return time.time() - (at.timestamp() + sec)

    gateway.tick("T01", 10.0, age_s=ago(0), volume=1000)
    gateway.tick("T01", 10.4, age_s=ago(10), volume=1500)
    gateway.tick("T01", 10.4, age_s=ago(15), volume=1500, bid=10.3, ask=10.5)    # only the book moved
    gateway.tick("T02", 20.0, age_s=ago(5), volume=300)
    c = md.candles.forming("T01")
    assert (c.close, c.high, c.volume, c.at) == (10.4, 10.4, 500.0, at.replace(second=0))
    assert md.candles.forming("T02").close == 20.0
    md.streams.sync(["T01"], [], 5)                      # T02's play went: its stream ends, and its candles
    assert md.candles.symbols() == ["T01"] and md.candles.forming("T02") is None
    md.detach()
    assert md.candles.symbols() == []


# ---------------------------------------------------------------- the latest price, for the dashboard
def test_the_latest_price_takes_a_streamed_quote_when_its_the_newest(tmp_path):
    gateway, md = _streaming(tmp_path)
    md.streams.sync(["AAA"], [], 5)
    q = gateway.tick("AAA", 10.0, age_s=30)                # older than a quote() would serve, fine to show
    price, at, age = md.last_seen("AAA")
    assert (price, at) == (10.0, q.ts) and 29 < age < 35

    earlier = q.ts - dt.timedelta(minutes=1)
    md._quotes["AAA"] = (time.monotonic(), quote_from_price("AAA", 9.0, ts=earlier))
    assert md.last_seen("AAA")[0] == 10.0                  # the stream's tick is the later price
    later = q.ts + dt.timedelta(seconds=10)
    md._quotes["AAA"] = (time.monotonic(), quote_from_price("AAA", 9.5, ts=later))
    assert md.last_seen("AAA")[:2] == (9.5, later)


def test_the_dashboard_is_sent_each_streamed_price_that_moved_once_the_latest_of_several(tmp_path):
    gateway, md = _streaming(tmp_path)
    md.streams.sync(["AAA", "BBB"], [], 5)
    assert md.streams.take_moves() == {}                   # nothing ticked
    gateway.tick("AAA", 10.0)
    a = gateway.tick("AAA", 10.25)                         # two ticks before the push: the later one goes
    b = gateway.tick("BBB", 20.0)
    assert md.streams.take_moves() == {"AAA": (10.25, a.ts), "BBB": (20.0, b.ts)}
    assert md.streams.take_moves() == {}                   # sent once
    gateway.tick("AAA", 10.25, bid=10.2, ask=10.3)          # the bid and ask moved, the price shown didn't
    assert md.streams.take_moves() == {}
    c = gateway.tick("AAA", 10.3)
    assert md.streams.take_moves() == {"AAA": (10.3, c.ts)}


def test_no_price_is_sent_once_it_cant_stream_and_a_new_connection_or_stream_sends_afresh(tmp_path):
    gateway, md = _streaming(tmp_path)
    md.streams.sync(["AAA"], [], 5)
    gateway.tick("AAA", 10.0)
    gateway.delayed = True                                 # the data went delayed before the push
    assert md.streams.take_moves() == {}
    gateway.delayed = False
    gateway.tick("AAA", 10.0)
    gateway.close()                                        # ...or the connection dropped
    assert md.streams.take_moves() == {}

    gateway.connect()
    md.streams.sync(["AAA"], [], 5)
    gateway.tick("AAA", 10.0)
    assert list(md.streams.take_moves()) == ["AAA"]
    md.attach(gateway)                                     # a new connection: its first price is sent
    md.streams.sync(["AAA"], [], 5)
    gateway.tick("AAA", 10.0)
    assert list(md.streams.take_moves()) == ["AAA"]
    md.streams.sync(["BBB"], [], 1)                        # AAA stops streaming - a snapshot may be shown meanwhile
    md.streams.sync(["AAA"], [], 1)
    gateway.tick("AAA", 10.0)
    assert list(md.streams.take_moves()) == ["AAA"]


# ---------------------------------------------------------------- the setting
def test_the_line_budget_stays_within_what_ibkr_allows():
    assert ExecutionCfg().stream_lines == 80 and ExecutionCfg().stream_watch == 50
    assert ExecutionCfg(stream_lines=500).stream_lines == 90
    assert ExecutionCfg(stream_lines=-3).stream_lines == 0
    assert ExecutionCfg(stream_watch=500).stream_watch == 80
    assert ExecutionCfg(stream_watch=-3).stream_watch == 0
