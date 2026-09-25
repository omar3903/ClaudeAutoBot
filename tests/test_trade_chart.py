"""The chart behind a trade record: the candles around it, its levels and marks, and how it stands."""

from __future__ import annotations

import datetime as dt

import pytest

import fakes
from test_engine import _connect, engine, gateway, port  # noqa: F401  (the engine on a synthetic Gateway)
from tos_bot.core.enums import Side, StrategyKind, Timeframe
from tos_bot.core.models import Play
from tos_bot.engine.chart import stop_moves, trade_standing
from tos_bot.persistence.db import session_scope
from tos_bot.persistence.models_orm import Trade
from tos_bot.util import clock


def _play(symbol="T01", side=Side.LONG, timeframe=Timeframe.INTRADAY, stop=95.0, target=110.0):
    return Play(symbol=symbol, side=side, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
                timeframe=timeframe, entry=100.0, stop=stop, targets=[target])


def _open(engine, play, qty=5):
    engine.repo.record_play(play)
    return engine.repo.open_trade(play, 100.0, qty, "paper")


def _entered_sessions_ago(tid, n):
    """Move the trade's entry back to 10:00 New York, ``n`` sessions before today's."""
    day = clock.last_n_sessions(clock.session_date(), n + 1)[0]
    at = dt.datetime.combine(day, dt.time(10, 0), tzinfo=clock.NY).astimezone(dt.timezone.utc).replace(tzinfo=None)
    with session_scope() as s:
        s.get(Trade, tid).entry_time = at


def _candle_requests(gateway, symbol="T01"):
    return [(bar, span) for s, bar, span in gateway.requests if s == symbol and bar == "5 mins"]


def test_an_open_day_trade_shows_five_minute_candles_from_the_session_before_its_entry(engine, port, gateway):
    _connect(engine, port)
    tid = _open(engine, _play())
    chart = engine.trade_chart(tid)
    assert chart["ok"] and chart["bars"].startswith("5-minute") and chart["status"] == "OPEN"
    # the scans' cached candles serve it: five sessions of 78, cut to the session before the entry and today's
    assert _candle_requests(gateway) == [("5 mins", "5 D")] and len(chart["candles"]) == 156
    first = dt.datetime.fromisoformat(chart["candles"][0]["t"]).date()
    assert first == clock.prev_trading_day(clock.session_date())
    assert chart["levels"] == {"entry": 100.0, "initial_stop": 95.0, "stop": 95.0, "targets": [110.0]}
    [entry] = chart["marks"]
    fill = engine.repo.trade_record(tid)["fills"][0]
    assert entry["kind"] == "entry" and entry["price"] == 100.0
    assert dt.datetime.fromisoformat(entry["t"]) == dt.datetime.fromisoformat(fill["ts"]).replace(tzinfo=dt.timezone.utc)
    standing = chart["standing"]
    assert standing["price"] and standing["open_r"] is not None and standing["unrealized_pl"] is not None
    assert (standing["at_stop_pl"], standing["at_target_pl"]) == (-25.0, 50.0) and standing["held"]
    assert chart["stop_moves"] == [] and [r["key"] for r in chart["routes"]][:2] == ["target", "stop"]


def test_a_swing_trade_past_the_cached_window_gets_one_request_of_its_own(engine, port, gateway):
    _connect(engine, port)
    tid = _open(engine, _play(timeframe=Timeframe.SWING))
    _entered_sessions_ago(tid, 6)                      # today is its seventh session
    first = engine.trade_chart(tid)
    again = engine.trade_chart(tid)
    # the session before the entry plus one to spare - asked once, the second click reuses the candles
    assert _candle_requests(gateway) == [("5 mins", "9 D")]
    assert first["ok"] and first["bars"].startswith("5-minute") and first["candles"] == again["candles"]
    assert first["standing"]["held"] == "7 sessions"


def test_a_trade_past_the_intraday_window_shows_daily_candles_without_asking_ibkr(engine, port, gateway):
    _connect(engine, port)
    engine.md.bars.merge("T01", fakes.daily_bars("T01"), clock.session_date())
    tid = _open(engine, _play(timeframe=Timeframe.SWING))
    _entered_sessions_ago(tid, 11)                     # today is its twelfth session
    chart = engine.trade_chart(tid)
    assert chart["ok"] and chart["bars"].startswith("daily") and len(chart["candles"]) == 120
    # closed since: still older than any intraday window, so still the daily candles on hand
    engine.repo.close_trade(tid, 104.0, exit_reason="time")
    closed = engine.trade_chart(tid)
    assert closed["bars"].startswith("daily") and closed["standing"]["held"] == "12 sessions"
    assert _candle_requests(gateway) == []


def test_a_closed_trade_carries_its_exit_mark_and_result(engine, port):
    _connect(engine, port)
    tid = _open(engine, _play())
    engine.repo.close_trade(tid, 104.0, exit_reason="target")
    chart = engine.trade_chart(tid)
    assert chart["status"] == "CLOSED" and [m["kind"] for m in chart["marks"]] == ["entry", "exit"]
    exit_mark = chart["marks"][-1]
    assert exit_mark["price"] == 104.0 and exit_mark["r"] == pytest.approx(0.8) and "target" in exit_mark["title"]
    standing = chart["standing"]
    assert standing["r_multiple"] == pytest.approx(0.8) and standing["realized_pl"] == 20.0
    assert standing["exit_reason"] == "target" and "price" not in standing


def test_a_short_trade_stands_the_other_way_round():
    # the same distances as the long's, so a sign slip would show this gain as a loss and the stop as a profit
    t = {"side": "SHORT", "status": "OPEN", "entry_price": 100.0, "initial_stop_price": 105.0,
         "stop_price": 105.0, "target_price": 90.0, "quantity": 5}
    standing = trade_standing(t, (98.0, None))
    assert (standing["price"], standing["open_r"], standing["unrealized_pl"]) == (98.0, 0.4, 10.0)
    assert (standing["at_stop_pl"], standing["at_target_pl"]) == (-25.0, 50.0)
    assert standing["open_r"] == -trade_standing({**t, "side": "LONG"}, (98.0, None))["open_r"]


def test_a_closed_short_carries_its_loss_with_the_sign_of_a_short(engine, port):
    _connect(engine, port)
    tid = _open(engine, _play(side=Side.SHORT, stop=105.0, target=90.0))
    engine.repo.close_trade(tid, 104.0, exit_reason="stop")            # covered higher: a loss for a short
    chart = engine.trade_chart(tid)
    assert chart["side"] == "SHORT" and chart["marks"][-1]["kind"] == "exit"
    assert chart["marks"][-1]["r"] == pytest.approx(-0.8) and "-0.80R" in chart["marks"][-1]["title"]
    standing = chart["standing"]
    assert standing["r_multiple"] == pytest.approx(-0.8) and standing["realized_pl"] == -20.0


def _stop_placed(engine, tid, stop, qty=4):
    """A protective stop order sent to the broker, as the stop keeper audits one."""
    engine.repo.record_order_audit("PLACE", {"symbol": "T01", "side": "SHORT", "qty": qty, "type": "STOP",
                                             "stop": stop, "is_entry": False}, {}, True, "ibkr",
                                   trade_id=tid, message="protective stop")


def test_the_marks_show_the_parts_taken_off_the_best_point_and_every_stop_move(engine, port):
    _connect(engine, port)
    tid = _open(engine, _play(), qty=6)
    engine.repo.update_trade_risk(tid, mfe=3.0, hwm_price=103.0)          # the exit manager saw +0.6R
    engine.repo.reduce_trade(tid, 2, 103.0)                                # the scale-out
    _stop_placed(engine, tid, 95.0)                                        # the stop at the broker
    # the exit manager moved it by modifying the resting order, which leaves no order row - only its note
    engine.repo.update_trade_risk(tid, stop_price=101.0, note_append="stop->101.00 @ 1.4R")
    _stop_placed(engine, tid, 101.0)                                       # lost later, and placed again
    chart = engine.trade_chart(tid)
    assert [(m["kind"], m["price"]) for m in chart["marks"]] == [("entry", 100.0), ("part", 103.0), ("best", 103.0)]
    assert "+0.6R" in chart["marks"][-1]["title"]
    # the move is the note's; the placement again is not it, so it lends the move no time
    assert chart["stop_moves"] == [{"t": None, "price": 101.0, "r": 1.4}]
    assert (chart["levels"]["initial_stop"], chart["levels"]["stop"], chart["quantity"]) == (95.0, 101.0, 4.0)
    assert chart["standing"]["mfe_r"] == 0.6


def test_a_stop_placed_again_after_two_moves_hides_neither_move(engine, port):
    _connect(engine, port)
    tid = _open(engine, _play())
    _stop_placed(engine, tid, 95.0)
    engine.repo.update_trade_risk(tid, stop_price=100.1, note_append="stop->100.10 @ 1.3R")
    engine.repo.update_trade_risk(tid, stop_price=101.5, note_append="stop->101.50 @ 1.9R")
    _stop_placed(engine, tid, 101.5)                                       # placed again where the stop now stands
    assert engine.trade_chart(tid)["stop_moves"] == [{"t": None, "price": 100.1, "r": 1.3},
                                                     {"t": None, "price": 101.5, "r": 1.9}]


def test_the_stop_moves_come_from_the_notes_without_times():
    t = {"initial_stop_price": 95.0, "stop_price": 101.5, "notes": "stop->100.10 @ 1.3R | stop->101.50 @ 1.9R"}
    assert stop_moves(t) == [{"t": None, "price": 100.1, "r": 1.3}, {"t": None, "price": 101.5, "r": 1.9}]
    assert stop_moves({"stop_price": 95.0, "notes": ""}) == []


def test_without_the_gateway_there_are_no_candles(engine):
    tid = _open(engine, _play())
    assert engine.trade_chart(tid) == {"ok": False, "reason": "IB Gateway isn't connected, so there are no candles."}
    assert engine.trade_chart("trd_missing")["ok"] is False


def test_the_routes_are_the_ones_the_play_chart_lists_for_the_same_levels(engine, port):
    _connect(engine, port)
    p = _play(timeframe=Timeframe.SWING)
    engine.board.replace([p], None)
    tid = _open(engine, p)
    routes = engine.trade_chart(tid)["routes"]
    assert routes and routes == engine.play_chart(p.id)["routes"]
