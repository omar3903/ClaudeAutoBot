"""The play board: one play per setup per session, confirmations, and setups that disagree."""

from __future__ import annotations

import datetime as dt

from tos_bot.core.enums import PlayStatus, Side, StrategyKind, Timeframe
from tos_bot.core.models import Play
from tos_bot.engine.board import PlayBoard


def _play(symbol="AAA", side=Side.LONG, strategy="abcd_pattern", entry=100.0):
    stop, target = (entry - 1, entry + 2) if side is Side.LONG else (entry + 1, entry - 2)
    return Play(symbol=symbol, side=side, strategy=strategy, kind=StrategyKind.TECHNICAL,
                timeframe=Timeframe.INTRADAY, entry=entry, stop=stop, targets=[target])


def test_a_setup_found_again_keeps_its_play_and_counts_a_confirmation():
    board = PlayBoard()
    first = _play()
    board.replace([first], scanned={"AAA"})
    board.replace([_play(entry=100.4)], scanned={"AAA"})
    [p] = board.plays.values()
    assert p.id == first.id and p.confirmations == 2 and p.entry == 100.4


def test_a_setup_that_drops_out_between_scans_starts_counting_again():
    board = PlayBoard()
    board.replace([_play()], scanned={"AAA"})
    board.replace([], scanned={"AAA"})
    board.replace([_play()], scanned={"AAA"})
    assert [p.confirmations for p in board.plays.values()] == [1]


def test_a_setup_acted_on_isnt_offered_again_this_session():
    board = PlayBoard()
    taken = _play()
    board.replace([taken], scanned={"AAA"})
    taken.status = PlayStatus.FILLED
    board.replace([_play()], scanned={"AAA"})
    assert list(board.plays) == [taken.id]
    board.replace([], scanned={"AAA"})                     # gone from the board...
    board.replace([_play()], scanned={"AAA"})
    assert not board.plays                                 # ...and still not offered again


def test_opposite_setups_on_one_stock_are_both_flagged_until_one_goes():
    board = PlayBoard()
    board.replace([_play(), _play(side=Side.SHORT, strategy="gap_and_go")], scanned={"AAA"})
    assert all("conflict" in p.noise for p in board.plays.values())
    board.replace([_play()], scanned={"AAA"})
    [p] = board.plays.values()
    assert "conflict" not in p.noise


def test_a_saved_board_comes_back_with_its_plays_and_what_was_settled():
    board = PlayBoard()
    offered, taken = _play("AAA"), _play("BBB")
    board.replace([offered, taken], scanned=None)
    board.replace([_play("AAA", entry=100.2), _play("BBB")], scanned=None)      # found twice
    board.get(taken.id).status = PlayStatus.FILLED
    plays, settled = board.saved()

    again = PlayBoard()
    assert again.restore(plays, settled) == 2
    assert again.get(offered.id).confirmations == 2 and again.get(taken.id).status is PlayStatus.FILLED
    again.replace([_play("AAA"), _play("BBB")], scanned={"AAA", "BBB"})
    assert again.get(offered.id).confirmations == 3                             # the count carries on
    assert [p.status for p in again.plays.values() if p.symbol == "BBB"] == [PlayStatus.FILLED]   # not offered again


def test_only_this_sessions_unexpired_plays_come_back_but_a_settled_setup_stays_settled():
    now = dt.datetime.now(dt.timezone.utc)
    stale, old, done = _play("AAA"), _play("BBB"), _play("CCC")
    stale.expires_at = now - dt.timedelta(minutes=1)
    old.created_at = now - dt.timedelta(days=9)
    done.status, done.expires_at = PlayStatus.REJECTED, now - dt.timedelta(minutes=1)
    fresh = _play("DDD")
    fresh.expires_at = now + dt.timedelta(minutes=30)

    board = PlayBoard()
    assert board.restore([stale, old, done, fresh], now=now) == 1 and list(board.plays) == [fresh.id]
    board.replace([_play("AAA"), _play("CCC")], scanned={"AAA", "CCC"}, now=now)
    assert sorted(p.symbol for p in board.plays.values()) == ["AAA", "DDD"]     # the dismissed one isn't offered again
