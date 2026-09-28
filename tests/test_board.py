"""The play board: one play per setup per session, confirmations, and setups that disagree."""

from __future__ import annotations

import datetime as dt
import pickle

import pandas as pd

from autotradebot.core.enums import PlayStatus, Side, StrategyKind, Timeframe
from autotradebot.core.models import Play
from autotradebot.engine.board import PlayBoard


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


def test_the_board_says_which_of_a_scans_plays_it_took():
    board = PlayBoard()
    first = _play()
    board.replace([first])
    again, other = _play(), _play("BBB")
    board.replace([again, other])
    assert board.holds(again) and board.holds(other) and not board.holds(first)   # the newest sighting holds its id
    board.get(again.id).status = PlayStatus.SUBMITTED
    late = _play()
    board.replace([late, other])
    assert not board.holds(late) and board.holds(other)                   # acted on: this session's sighting is left out


# ---------------------------------------------------------------- confirmations counted on candles
OPEN = pd.Timestamp("2026-03-02 09:30", tz="America/New_York")


def _on_candle(minutes, symbol="AAA", timeframe=Timeframe.INTRADAY):
    """A sighting of the setup whose last closed 5-minute candle started ``minutes`` after the open."""
    p = _play(symbol)
    p.timeframe = timeframe
    p.evidence["bar_at"] = (OPEN + pd.Timedelta(minutes=minutes)).isoformat()
    return p


def _count(board):
    [p] = board.plays.values()
    return p.confirmations


def test_a_second_scan_on_the_same_candle_is_not_a_confirmation():
    board = PlayBoard()
    board.replace([_on_candle(0)], scanned={"AAA"}, new_candle=True)
    board.replace([_on_candle(0)], scanned={"AAA"}, new_candle=True)
    board.replace([_on_candle(0)], scanned={"AAA"}, new_candle=True)
    assert _count(board) == 1


def test_a_newer_candle_counts_one_confirmation_even_from_the_quick_recheck():
    board = PlayBoard()
    board.replace([_on_candle(0)], scanned={"AAA"}, new_candle=True)
    board.replace([_on_candle(5)], scanned={"AAA"}, confirm=False, new_candle=True)     # the 15-second re-check
    assert _count(board) == 2
    board.replace([_on_candle(5)], scanned={"AAA"}, new_candle=True)                    # a cycle, same candle
    [p] = board.plays.values()
    assert p.confirmations == 2 and p.evidence["counted_bar"] == (OPEN + pd.Timedelta(minutes=5)).isoformat()


def test_a_sighting_more_than_two_candles_later_starts_counting_again():
    board = PlayBoard()
    board.replace([_on_candle(0)], scanned={"AAA"}, new_candle=True)
    board.replace([_on_candle(10)], scanned={"AAA"}, new_candle=True)     # one candle skipped: a scan landing late
    assert _count(board) == 2
    board.replace([_on_candle(25)], scanned={"AAA"}, new_candle=True)     # three candles on: not "in a row"
    assert _count(board) == 1
    board.replace([_on_candle(30)], scanned={"AAA"}, new_candle=True)
    assert _count(board) == 2


def test_with_the_setting_off_every_scan_counts_as_before():
    board = PlayBoard()
    board.replace([_on_candle(0)], scanned={"AAA"}, new_candle=False)
    board.replace([_on_candle(0)], scanned={"AAA"}, new_candle=False)
    assert _count(board) == 2
    board.replace([_on_candle(5)], scanned={"AAA"}, confirm=False, new_candle=False)   # a quick re-check never counts
    assert _count(board) == 2


def test_swing_plays_count_scans_as_before():
    board = PlayBoard()
    board.replace([_on_candle(0, timeframe=Timeframe.SWING)], scanned={"AAA"}, new_candle=True)
    board.replace([_on_candle(0, timeframe=Timeframe.SWING)], scanned={"AAA"}, new_candle=True)
    assert _count(board) == 2
    board.replace([_on_candle(5, timeframe=Timeframe.SWING)], scanned={"AAA"}, confirm=False, new_candle=True)
    assert _count(board) == 2


def test_a_saved_board_keeps_the_candle_it_last_counted():
    board = PlayBoard()
    board.replace([_on_candle(0)], scanned={"AAA"}, new_candle=True)
    board.replace([_on_candle(5)], scanned={"AAA"}, new_candle=True)
    plays, settled = pickle.loads(pickle.dumps(board.saved()))            # the day's state file pickles them

    again = PlayBoard()
    assert again.restore(plays, settled) == 1
    again.replace([_on_candle(5)], scanned={"AAA"}, new_candle=True)      # the candle it had counted: no more
    assert _count(again) == 2
    again.replace([_on_candle(10)], scanned={"AAA"}, new_candle=True)
    assert _count(again) == 3


def test_a_play_without_a_candle_time_counts_as_before():
    board = PlayBoard()
    board.restore([_play()])                                              # saved before plays knew their candle
    board.replace([_on_candle(0)], scanned={"AAA"}, new_candle=True)
    assert _count(board) == 2
    board.replace([_on_candle(0)], scanned={"AAA"}, new_candle=True)      # from here it counts candles
    assert _count(board) == 2
    board.replace([_play()], scanned={"AAA"}, new_candle=True)            # a sighting that doesn't know its candle
    assert _count(board) == 3
