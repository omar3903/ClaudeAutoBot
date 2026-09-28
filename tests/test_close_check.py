"""The candle-close check seconds after each 5-minute close and the early-mover check between closes: what the
candle loop queues, when the scan loop runs it, what it asks IBKR for, and the switches that leave it all off - and
IBKR's live scans, whose movers join the watch tier the checks read."""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import time

import pandas as pd
import pytest

import fakes
from tos_bot.core.enums import Side, StrategyKind, Timeframe
from tos_bot.core.models import Play
from tos_bot.engine import TradingEngine
from tos_bot.indicators import ta
from tos_bot.scanner.scanner import BENCHMARK
from tos_bot.scanner.watchlist import Candidate, DayWatchlist
from tos_bot.util import clock

DAY = dt.date(2026, 9, 24)                     # a full trading day
WATCH = ["T01", "T02", "T03", "T04", "T05", "T06"]


def _at(hh: int, mm: int, ss: float = 0.0) -> dt.datetime:
    """That New York time on DAY."""
    return dt.datetime.combine(DAY, dt.time(hh, mm), clock.NY) + dt.timedelta(seconds=ss)


class _Setup:
    """One day play on every stock it is shown, at its latest price."""
    key, style, weight = "vwap_reclaim", "reversal", 1.0
    kind, timeframe = StrategyKind.TECHNICAL, Timeframe.INTRADAY

    def generate(self, ctx):
        return [Play(symbol=ctx.symbol, side=Side.LONG, strategy=self.key, kind=StrategyKind.TECHNICAL,
                     timeframe=Timeframe.INTRADAY, entry=ctx.price, stop=ctx.price * 0.99,
                     targets=[ctx.price * 1.03])]


@pytest.fixture
def now(monkeypatch):
    """The pinned New York time - set now["t"] to move it."""
    held = {"t": _at(10, 5, 2)}
    monkeypatch.setattr(clock, "now_ny", lambda: held["t"])
    return held


@pytest.fixture
def gateway():
    gw = fakes.StreamingGateway(fakes.SYMBOLS + [BENCHMARK])
    gw.connect()
    return gw


@pytest.fixture
def engine(tmp_path, gateway, now):
    from tos_bot.persistence.db import DB

    DB.init(url=f"sqlite:///{(tmp_path / 'engine.sqlite').as_posix()}")
    DB.create_all()
    e = TradingEngine(data_dir=tmp_path / "data", runtime_path=tmp_path / "runtime.json",
                      broker_factory=fakes.broker_factory(gateway), port_check=lambda host, p: False,
                      listings=fakes.FakeListings(fakes.SYMBOLS), fundamentals=fakes.NoFundamentals(),
                      init_database=False)
    e._bind()
    e.md.attach(gateway)
    e.scanner.set_strategies([_Setup()])
    # today's watch tier: the hot list, hottest first
    e.scanner.watchlist = DayWatchlist(
        session=DAY, bars_through=clock.prev_trading_day(DAY), built_at="", universe=40, liquid=40,
        hot=[Candidate(s, "Technology", 1.0, heat=float(10 - i)) for i, s in enumerate(WATCH)], queues={}, kept={})
    e.md.update_daily(WATCH + [BENCHMARK], clock.prev_trading_day(DAY))
    yield e
    e.stop()
    DB.engine.dispose()
    DB.init(url=os.environ["DATABASE_URL"])


def _bars_until(gateway, monkeypatch, at: dt.datetime) -> dict:
    """Have the fake Gateway's 5-minute bars end with the one starting at ``at`` - still printing, as IBKR's are a
    moment after a close. Returns the limits: set "at" to move them all, or a symbol's own."""
    limits = {"at": at}
    history = gateway.history_many

    def cut(requests, con_ids=None, end=None, rth=True):
        got = history(requests, con_ids, end, rth)
        return {s: f if requests[s][0] == "1 day" else f[f.index <= limits.get(s, limits["at"])]
                for s, f in got.items()}

    monkeypatch.setattr(gateway, "history_many", cut)
    return limits


def _spy_run_close(engine, monkeypatch) -> list:
    """Every result Scanner.run_close hands the engine, kept for the test to read."""
    got = []
    run_close = engine.scanner.run_close
    monkeypatch.setattr(engine.scanner, "run_close", lambda symbols, since: got.append(run_close(symbols, since))
                        or got[-1])
    return got


def _later(engine, seconds: float) -> None:
    """``seconds`` pass for the checks' 15 s rule - the only part of them on the monotonic clock."""
    engine._close_asked = {s: t - seconds for s, t in engine._close_asked.items()}


def _asked(gateway, since: int):
    """The history requests for the stocks since the ``since``-th - the benchmark's left out."""
    return [r for r in gateway.requests[since:] if r[0] != BENCHMARK]


def _play(symbol):
    return Play(symbol=symbol, side=Side.LONG, strategy="rsi2_mean_reversion", kind=StrategyKind.TECHNICAL,
                timeframe=Timeframe.SWING, entry=100.0, stop=95.0, targets=[110.0])


# ---------------------------------------------------------------- the check itself
def test_the_check_reads_the_last_hour_of_bars_and_leaves_a_stock_whose_new_bar_isnt_in(engine, gateway, monkeypatch):
    limits = _bars_until(gateway, monkeypatch, _at(10, 5))
    limits["T02"] = _at(10, 0)                                   # its 10:05 bar hasn't printed yet
    engine.md.intraday(["T01", "T02"])                           # cached by an earlier cycle
    engine.board.replace([_play("T02")], None)
    got, asked = _spy_run_close(engine, monkeypatch), len(gateway.requests)
    engine._close_due = (_at(10, 5), time.monotonic())
    engine._run_scan("close")

    # one request a stock: the last hour past the cache, a full history for a stock with nothing cached
    assert sorted(_asked(gateway, asked)) == [("T01", "5 mins", "3600 S"), ("T02", "5 mins", "3600 S")] + [
        (s, "5 mins", "5 D") for s in WATCH[2:]]
    [result] = got
    assert result.kind == "close" and result.symbols == ["T01", "T03", "T04", "T05", "T06"]
    assert any(p.symbol == "T02" for p in engine.board.plays.values())            # its play is kept
    assert {p.symbol for p in result.plays} == set(result.symbols)
    for p in result.plays:                                       # the candle that closed at 10:05, recorded as a scan's
        assert pd.Timestamp(p.evidence["bar_at"]) == pd.Timestamp(_at(10, 0)) and p.scan_run_id == result.run_id


def test_a_5_minute_close_queues_the_check_2_s_on_and_it_stands_in_for_the_fast_cycle(engine, gateway, now,
                                                                                         monkeypatch, caplog):
    from tos_bot.persistence.db import session_scope
    from tos_bot.persistence.models_orm import ScanRun

    limits = _bars_until(gateway, monkeypatch, _at(10, 5))
    now["t"] = _at(10, 5, 0.3)
    engine._scan_wake.clear()
    engine._on_minute(_at(10, 5).timestamp())
    boundary, due = engine._close_due
    assert boundary == _at(10, 5) and 1.5 < due - time.monotonic() <= 2.0 and engine._scan_wake.is_set()
    assert engine._next_scan_wait() <= 2.0 and engine._movers_due == {}
    assert engine._due_scan() == "cycle"                         # not due yet: the cycle that is goes first
    engine._last_cycle_at = time.monotonic()
    monkeypatch.setattr(engine, "_autopilot_day_active", lambda: True)
    assert engine._due_scan() is None                            # the fast cycle waits the 2 s for it
    engine._close_due = (boundary, time.monotonic())
    engine._last_cycle_at = float("-inf")
    assert engine._due_scan() == "close"                         # due: ahead of the cycle

    published = []
    publish = engine._publish
    monkeypatch.setattr(engine, "_publish", lambda topic, **kw: published.append(topic) or publish(topic, **kw))
    now["t"] = _at(10, 5, 2.4)
    with caplog.at_level(logging.INFO, logger="tos_bot.engine.engine"):
        engine._run_scan("close")
    assert "plays.updated" in published and "scan.started" not in published and "watchlist.updated" not in published
    assert engine._close_due is None and time.monotonic() - engine._last_fast_at < 5
    engine._last_cycle_at = engine._last_plays_at = time.monotonic()
    assert engine._due_scan() is None                            # it counted as the fast cycle
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("close check")]
    assert len(lines) == 1 and lines[0].startswith("close check 10:05 ET: 6 of 6 stocks read, 6 plays (6 new) - "
                                                   "published 2.4 s after the candle closed (candles ")
    with session_scope() as s:
        assert s.get(ScanRun, engine._last_scans["close"]["run_id"]).kind == "close"

    # the next close reads a newer candle: each day play counts one more confirmation
    before = {p.symbol: p.confirmations for p in engine.board.plays.values()}
    limits["at"], now["t"] = _at(10, 10), _at(10, 10, 2.5)
    _later(engine, 300)
    engine._close_due = (_at(10, 10), time.monotonic())
    engine._run_scan("close")
    assert {p.symbol: p.confirmations for p in engine.board.plays.values()} == {s: n + 1 for s, n in before.items()}


@pytest.mark.parametrize("off", ["close_check", "stream_watch", "delayed", "gateway down", "quit", "16:00",
                                 "09:30"])
def test_nothing_is_queued_while_the_check_is_off_or_cant_run(engine, gateway, now, monkeypatch, off):
    _bars_until(gateway, monkeypatch, _at(10, 5))
    engine.md.intraday(["T01"])
    boundary = {"16:00": _at(16, 0), "09:30": _at(9, 30)}.get(off, _at(10, 5))
    if off == "close_check":
        monkeypatch.setattr(engine.settings.config.scanner, "close_check", False)
    elif off == "stream_watch":
        monkeypatch.setattr(engine.settings.config.execution, "stream_watch", 0)
    elif off == "delayed":
        gateway.delayed = True
    elif off == "gateway down":
        gateway.connected = False                  # IBKR reconnecting by itself: the source stays attached
    elif off == "quit":
        engine.quit_state = {"by": "operator"}
    now["t"] = boundary + dt.timedelta(seconds=0.3)
    answer, asked = engine._due_scan(), len(gateway.requests)
    engine._scan_wake.clear()
    engine._on_minute(boundary.timestamp())
    if off not in ("16:00", "09:30"):                           # and no early mover either
        _big_minute(engine, {}, "T01", _at(10, 6))
        engine._on_minute(_at(10, 7).timestamp())
    assert engine._close_due is None and engine._movers_due == {} and not engine._scan_wake.is_set()
    assert engine._due_scan() == answer and len(gateway.requests) == asked


def test_a_check_that_fails_before_it_starts_is_dropped_and_the_scan_loop_doesnt_spin_on_it(engine, gateway, now,
                                                                                            monkeypatch):
    def broken():
        raise RuntimeError("the database is locked")

    monkeypatch.setattr(engine, "strategy_odds", broken)          # the lead-in fails, before the check itself
    engine._close_due = (_at(10, 5), time.monotonic())
    engine._movers_due, engine._mover_why = {"T01": _at(10, 4)}, {"T01": "a move"}
    engine._last_cycle_at = engine._last_plays_at = time.monotonic()
    asked = len(gateway.requests)
    engine._run_scan("close")
    # nothing left due: the scan loop waits its 5 s and the other scans get their turn - not 'close' again at once
    assert engine._close_due is None and engine._movers_due == {} and engine._mover_why == {}
    assert engine._next_scan_wait() == 5.0 and engine._due_scan() is None and _asked(gateway, asked) == []


def test_an_early_mover_waits_for_the_close_queued_behind_it_until_ibkr_has_finished_the_bar(engine, gateway, now,
                                                                                               monkeypatch):
    limits = _bars_until(gateway, monkeypatch, _at(10, 5))
    engine.md.intraday(WATCH)
    engine._last_cycle_at = engine._last_plays_at = time.monotonic()
    # T01 moved in the 10:08 minute, but the scan thread was busy until just after 10:10, when the close was queued
    engine._movers_due, engine._mover_why = {"T01": _at(10, 9)}, {"T01": "1-minute candle 1.4 ATRs"}
    engine._close_due = (_at(10, 10), time.monotonic() + 1.5)
    now["t"] = _at(10, 10, 0.75)
    assert engine._due_scan() is None and 0 < engine._next_scan_wait() <= 1.5
    asked = len(gateway.requests)
    engine._run_scan("close")                                    # even if it runs, nothing is asked before the grace
    assert _asked(gateway, asked) == [] and engine._close_due is not None and "T01" in engine._movers_due

    # due: one check over the whole tier, T01 among it, on the finished bars
    limits["at"], now["t"] = _at(10, 10), _at(10, 10, 2.3)
    engine._close_due = (_at(10, 10), time.monotonic())
    got = _spy_run_close(engine, monkeypatch)
    assert engine._due_scan() == "close"
    engine._run_scan("close")
    assert sorted(r[0] for r in _asked(gateway, asked)) == sorted(WATCH) and engine._movers_due == {}
    [result] = got
    assert result.symbols == WATCH and engine._close_due is None


# ---------------------------------------------------------------- early movers
def _atr5(engine, symbol) -> float:
    return float(ta.atr(engine.md.cached_intraday(symbol).tail(30), 14).iloc[-1])


def _trades(engine, tape: dict, symbol, minute: dt.datetime, prices, volume: float) -> None:
    """Trades for ``symbol`` in the minute starting at ``minute``: ``prices`` in turn, ``volume`` shares in all.
    ``tape`` holds each stock's cumulative day volume; a stock new to it was streaming before the minute."""
    lc = engine.md.candles
    if symbol not in tape:
        tape[symbol] = 10_000.0
        lc.add(symbol, prices[0], tape[symbol], minute.timestamp() - 10)
    for i, price in enumerate(prices):
        tape[symbol] += volume / len(prices)
        lc.add(symbol, price, tape[symbol], minute.timestamp() + 5 + 50 * i / len(prices))


def _big_minute(engine, tape: dict, symbol, minute: dt.datetime, atrs: float = 1.5) -> None:
    """A 1-minute candle spanning ``atrs`` of the stock's 5-minute ATRs."""
    atr, price = _atr5(engine, symbol), float(engine.md.cached_intraday(symbol)["close"].iloc[-1])
    _trades(engine, tape, symbol, minute, [price, price + atrs * atr, price + atrs * atr * 0.9], 300)


def test_a_minute_spanning_a_5_minute_atr_is_checked_at_once_and_at_most_every_5_minutes(engine, gateway,
                                                                                            monkeypatch):
    _bars_until(gateway, monkeypatch, _at(10, 5))
    engine.md.intraday(WATCH)
    tape: dict = {}
    _big_minute(engine, tape, "T01", _at(10, 6), atrs=1.2)
    _big_minute(engine, tape, "T02", _at(10, 6), atrs=0.5)      # an ordinary minute
    engine._scan_wake.clear()
    engine._on_minute(_at(10, 7).timestamp())
    assert engine._movers_due == {"T01": _at(10, 7)} and engine._scan_wake.is_set() and engine._close_due is None
    assert engine._mover_why == {"T01": "1-minute candle 1.2 ATRs"}

    engine._movers_due.clear()                                   # as the check takes them
    _big_minute(engine, tape, "T01", _at(10, 8))
    engine._on_minute(_at(10, 9).timestamp())
    assert engine._movers_due == {}                              # two minutes on: not again

    _big_minute(engine, tape, "T03", _at(10, 9))
    engine._on_minute(_at(10, 10).timestamp())
    assert engine._movers_due == {} and engine._close_due[0] == _at(10, 10)    # the close covers the whole tier

    monkeypatch.setattr(engine.settings.config.scanner, "mover_atr", 0.0)
    _big_minute(engine, tape, "T03", _at(10, 11))
    engine._on_minute(_at(10, 12).timestamp())
    assert engine._movers_due == {}                              # 0 = off

    monkeypatch.setattr(engine.settings.config.scanner, "mover_atr", 1.0)
    price, atr = float(engine.md.cached_intraday("T04")["close"].iloc[-1]), _atr5(engine, "T04")
    engine.md.candles.add("T04", price, 20_000.0, _at(10, 12, 30).timestamp())     # first seen mid-minute
    engine.md.candles.add("T04", price + 2 * atr, 20_500.0, _at(10, 12, 40).timestamp())
    engine._on_minute(_at(10, 13).timestamp())
    assert engine._movers_due == {}                              # a partial candle proves nothing


def test_a_new_high_of_the_day_on_3x_the_minute_volume_is_checked_at_once(engine, gateway, monkeypatch):
    _bars_until(gateway, monkeypatch, _at(10, 5))
    engine.md.intraday(["T05", "T06"])
    tape: dict = {}
    for s, volume in (("T05", 400), ("T06", 200)):              # 4x and 2x the minutes before it
        frame, atr = engine.md.cached_intraday(s), _atr5(engine, s)
        top = float(frame[frame.index.date == DAY]["high"].max())       # IBKR's high of the day so far
        for m in range(21, 26):                                  # five whole minutes just under it
            _trades(engine, tape, s, _at(10, m), [top - 0.04 * atr, top - 0.02 * atr], 100)
        _trades(engine, tape, s, _at(10, 26), [top + 0.02 * atr, top + 0.03 * atr], volume)
    engine._on_minute(_at(10, 27).timestamp())
    assert engine._movers_due == {"T05": _at(10, 27)} and engine._mover_why == {"T05": "new high on 4.0x volume"}


def test_the_live_candles_day_stats_leave_out_partial_and_later_minutes(engine):
    lc = engine.md.candles
    assert lc.day_stats("AAA", _at(10, 5).timestamp()) is None
    lc.add("AAA", 10.0, 1000.0, _at(10, 0, 30).timestamp())       # first seen mid-minute: 10:00 is partial
    lc.add("AAA", 10.4, 1100.0, _at(10, 1, 10).timestamp())
    lc.add("AAA", 9.8, 1400.0, _at(10, 2, 10).timestamp())
    lc.add("AAA", 12.0, 9000.0, _at(10, 3, 10).timestamp())
    lc.roll(_at(10, 4).timestamp())
    assert lc.day_stats("AAA", _at(10, 3).timestamp()) == (10.4, 9.8, 200.0, 2)
    assert lc.day_stats("AAA", _at(10, 1).timestamp()) is None


# ---------------------------------------------------------------- IBKR's limits
def test_no_stock_is_asked_twice_within_15_s_and_a_late_check_is_dropped(engine, gateway, now, monkeypatch, caplog):
    limits = _bars_until(gateway, monkeypatch, _at(10, 5))
    engine._close_due = (_at(10, 5), time.monotonic())
    engine._run_scan("close")
    asked = len(gateway.requests)
    engine._movers_due, engine._mover_why = {"T01": _at(10, 6)}, {"T01": "a move"}
    now["t"] = _at(10, 6, 0.3)
    engine._run_scan("close")                                    # seconds after the check that asked for it
    assert _asked(gateway, asked) == [] and engine._movers_due == {}
    _later(engine, 15)
    engine._movers_due, engine._mover_why = {"T01": _at(10, 6)}, {"T01": "1-minute candle 1.4 ATRs"}
    with caplog.at_level(logging.INFO, logger="tos_bot.engine.engine"):
        engine._run_scan("close")
    assert _asked(gateway, asked) == [("T01", "5 mins", "3600 S")]
    assert "early-mover check 10:06 ET (T01: 1-minute candle 1.4 ATRs): 1 stock, 1 play - published 0.3 s" in caplog.text

    _later(engine, 300)
    limits["at"], asked = _at(10, 10), len(gateway.requests)
    engine._close_due, now["t"] = (_at(10, 10), time.monotonic()), _at(10, 11, 1)
    with caplog.at_level(logging.DEBUG, logger="tos_bot.engine.engine"):
        engine._run_scan("close")                                # 61 s after its close: a scan held the thread
    assert _asked(gateway, asked) == [] and engine._close_due is None and "check is dropped" in caplog.text
    engine._close_due, now["t"] = (_at(10, 10), time.monotonic()), _at(10, 10, 59)
    engine._run_scan("close")
    assert len(_asked(gateway, asked)) == len(WATCH)


def test_once_a_session_the_live_candles_are_compared_with_ibkrs_bars(engine, gateway, monkeypatch, caplog):
    _bars_until(gateway, monkeypatch, _at(10, 5))
    frames, bar = engine.md.intraday(WATCH), pd.Timestamp(_at(10, 0))
    lc = engine.md.candles
    for s in WATCH:                                              # the stream counts what IBKR's bar does
        close, volume = float(frames[s].at[bar, "close"]), float(frames[s].at[bar, "volume"])
        lc.add(s, close, 50_000.0, _at(9, 59, 50).timestamp())
        for m in range(5):
            lc.add(s, close, 50_000.0 + volume * (m + 1) / 5, _at(10, m, 30).timestamp())
    lc.roll(_at(10, 5).timestamp())
    with caplog.at_level(logging.INFO, logger="tos_bot.scanner.scanner"):
        engine.scanner.run_close(WATCH, _at(10, 5))
        engine.scanner.run_close(WATCH, _at(10, 5))
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("live candles vs IBKR")]
    assert len(lines) == 1 and lines[0].startswith("live candles vs IBKR 5-minute bars at 10:05")
    assert float(re.search(r"median volume ratio ([\d.]+)", lines[0]).group(1)) == pytest.approx(1.0)
    assert "median close difference 0.00%" in lines[0]


# ---------------------------------------------------------------- IBKR's live scans
#: what IBKR's three live scans show: T01 is on the hot list already, T23 is no stock IBKR has, T24 is in a sector the
#: filters leave out and T25 has no daily candles; the rest are names the app has never seen
LIVE = {"TOP_PERC_GAIN": ["T20", "T01", "T21", "T22"], "TOP_PERC_LOSE": ["T23", "T24", "T20"],
        "HOT_BY_VOLUME": ["T25", "T26", "T27"]}


@pytest.fixture
def live(engine, monkeypatch):
    """The engine with IBKR's live scans showing LIVE, 3 live-scan slots and every sector but Utilities (T24's)."""
    from tos_bot.data.sectors import SECTORS
    from tos_bot.scanner.filters import TradeFilters

    monkeypatch.setattr(fakes, "SCANS", {code: list(names) for code, names in LIVE.items()})
    monkeypatch.setattr(engine.settings.config.scanner, "live_scan", 3)
    engine.scanner.filters = TradeFilters(sectors=tuple(s for s in SECTORS if s != "Utilities"))
    engine.scanner.symbols.record({**{s: fakes.contract_details(s) for s in WATCH}, "T23": None})
    engine.md.update_daily(["T20", "T21", "T22", "T24", "T26", "T27"], clock.prev_trading_day(DAY))
    return engine


def test_the_live_scans_put_the_first_passing_names_right_after_the_hot_list(live, gateway, monkeypatch, caplog):
    live._stream_wake.clear()
    with caplog.at_level(logging.INFO, logger="tos_bot.engine.engine"):
        assert live._live_scan_once() == ["T20", "T26", "T21"]        # interleaved by rank, the first 3 that pass
    assert gateway.scan_calls == ["TOP_PERC_GAIN", "TOP_PERC_LOSE", "HOT_BY_VOLUME"]
    assert live.scanner.live_names == ["T20", "T26", "T21"] and live._stream_wake.is_set()
    assert "live scan: +T20 +T26 +T21" in caplog.text
    master = live.scanner.symbols
    assert master.get("T22").found and master.get("T24").sector == "Utilities"      # looked up and kept
    assert not master.get("T23").found
    assert live.scanner.watch_symbols(50) == WATCH + ["T20", "T26", "T21"]
    # once the close check has read them, the hottest first; one it hasn't read yet after them
    live.scanner.live_stats = {"T21": (0.9, 1e8), "T26": (0.2, 1e8)}
    assert live.scanner.watch_symbols(50) == WATCH + ["T21", "T26", "T20"]
    assert live.scanner.watch_symbols(8) == WATCH + ["T21", "T26"]

    # the same names next round: nothing said, the streams left alone
    live._stream_wake.clear()
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="tos_bot.engine.engine"):
        assert live._live_scan_once() == ["T20", "T26", "T21"]
    assert len(gateway.scan_calls) == 6 and not live._stream_wake.is_set() and "live scan" not in caplog.text
    # every scan empty is the scans failing: the names held stay
    monkeypatch.setattr(fakes, "SCANS", {})
    assert live._live_scan_once() == ["T20", "T26", "T21"] and len(gateway.scan_calls) == 9


def test_the_live_scan_slots_default_to_10_within_0_to_20():
    from tos_bot.config import ScannerCfg

    assert ScannerCfg().live_scan == 10
    assert ScannerCfg(live_scan=50).live_scan == 20 and ScannerCfg(live_scan=-1).live_scan == 0


@pytest.mark.parametrize("off", ["live_scan", "stream_watch", "delayed", "closed", "quit", "yesterday"])
def test_no_live_scan_is_asked_for_while_it_is_off_or_cant_run(live, gateway, now, monkeypatch, off):
    live.scanner.set_live_names(["T20"])
    if off == "live_scan":
        monkeypatch.setattr(live.settings.config.scanner, "live_scan", 0)
    elif off == "stream_watch":
        monkeypatch.setattr(live.settings.config.execution, "stream_watch", 0)
    elif off == "delayed":
        gateway.delayed = True
    elif off == "closed":
        now["t"] = _at(16, 5)
    elif off == "quit":
        live.quit_state = {"by": "operator"}
    elif off == "yesterday":
        live.scanner.watchlist.session = clock.prev_trading_day(DAY)
    assert live._live_scan_once() == [] and gateway.scan_calls == []
    assert live.scanner.live_names == []                              # the names held are let go


def test_a_live_name_thin_on_todays_volume_is_left_out_from_the_next_round(live, gateway, now, monkeypatch):
    _bars_until(gateway, monkeypatch, _at(10, 5))
    live._live_scan_once()
    live._close_due = (_at(10, 5), time.monotonic())
    live._run_scan("close")                                           # the live names are in the watch tier it reads
    dollars = {s: live.scanner.live_stats[s][1] for s in ("T20", "T26", "T21")}
    assert live._live_thin == set() and all(v > 0 for v in dollars.values())     # liquid enough at 5M a day

    # a day's floor that, pro rata for 35 minutes of the session, falls between the thinnest and the rest
    low, *rest = sorted(dollars.values())
    per_day = (low + rest[0]) / 2 * 390 / max(30.0, clock.minutes_since_open(now["t"]))
    monkeypatch.setattr(live.settings.config.scanner, "prefilter", {"min_dollar_volume": per_day})
    _later(live, 300)
    live._close_due = (_at(10, 5), time.monotonic())
    live._run_scan("close")
    thin = min(dollars, key=dollars.get)
    assert live._live_thin == {thin}
    assert live._live_scan_once() == [s for s in ("T20", "T26", "T21") if s != thin] + ["T27"]
