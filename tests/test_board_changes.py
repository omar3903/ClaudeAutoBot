"""The play board says what it added and removed, and why."""

from __future__ import annotations

import datetime as dt

from tos_bot.core.enums import Side, StrategyKind, Timeframe
from tos_bot.core.models import Play
from tos_bot.engine.board import PlayBoard

NOW = dt.datetime(2026, 9, 15, 15, 0, tzinfo=dt.timezone.utc)


def _play(symbol="AAA", strategy="vwap_reclaim", side=Side.LONG, kind=StrategyKind.TECHNICAL,
          timeframe=Timeframe.INTRADAY, **kw):
    return Play(symbol=symbol, side=side, strategy=strategy, kind=kind, timeframe=timeframe, entry=10.0, stop=9.5,
                targets=[11.0], rationale=f"{strategy} fired on {symbol}", created_at=NOW, **kw)


def _changes(changes):
    return [(c.kind, c.play.symbol, c.why) for c in changes]


def test_new_setups_are_announced_and_vanished_ones_say_why():
    board = PlayBoard()
    assert _changes(board.replace([_play("AAA"), _play("BBB")], None, NOW)) == [
        ("added", "AAA", "vwap_reclaim fired on AAA"), ("added", "BBB", "vwap_reclaim fired on BBB")]
    changes = board.replace([_play("AAA")], scanned=["AAA", "BBB"], now=NOW)
    assert _changes(changes) == [("removed", "BBB", "the setup no longer shows on the latest candles")]
    assert [p.confirmations for p in board.ranked()] == [2]


def test_expired_plays_and_plays_the_full_scan_didnt_find_again_say_so():
    board = PlayBoard()
    board.replace([_play("OLD", expires_at=NOW - dt.timedelta(minutes=1)), _play("AAA")], None, NOW)
    assert _changes(board.replace([_play("AAA")], scanned=["AAA"], now=NOW)) == [
        ("removed", "OLD", "it expired - the setup is too old to act on")]
    assert _changes(board.replace([], None, NOW)) == [("removed", "AAA", "the full scan didn't find the setup again")]


def test_a_quick_refresh_keeps_valuation_plays_and_doesnt_count_as_a_confirmation():
    board = PlayBoard()
    valuation = _play("AAA", "dcf_fair_value_gap", kind=StrategyKind.FUNDAMENTAL, timeframe=Timeframe.SWING)
    board.replace([valuation, _play("AAA")], None, NOW)
    changes = board.replace([_play("AAA")], ["AAA"], NOW, keep=lambda p: p.kind is StrategyKind.FUNDAMENTAL,
                            confirm=False)
    assert changes == []
    assert sorted((p.strategy, p.confirmations) for p in board.ranked()) == [("dcf_fair_value_gap", 1),
                                                                             ("vwap_reclaim", 1)]


def test_plays_dropped_by_a_filter_say_why():
    board = PlayBoard()
    board.replace([_play("AAA"), _play("BBB", side=Side.SHORT)], None, NOW)
    gone = board.drop(lambda p: p.side is Side.LONG, "the Short filter is off")
    assert _changes(gone) == [("removed", "BBB", "the Short filter is off")]
    assert [p.symbol for p in board.ranked()] == ["AAA"]
