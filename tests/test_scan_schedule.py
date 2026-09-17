"""When the scans run, the day's hot list and sector buffers, and the play board."""

from __future__ import annotations

import datetime as dt

import pytest

from tos_bot.core.enums import Side, StrategyKind, Timeframe
from tos_bot.core.models import Play
from tos_bot.engine.board import PlayBoard
from tos_bot.scanner import schedule
from tos_bot.scanner.heat import DailyMetrics
from tos_bot.scanner.schedule import ScanSettings
from tos_bot.scanner.watchlist import DayWatchlist
from tos_bot.util import clock

MONDAY = dt.date(2026, 9, 14)


def at(day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=clock.NY)


# ---------------------------------------------------------------- the market clock
def test_the_next_session_change_is_exact():
    assert clock.next_session_change(at(MONDAY, 3)) == (at(MONDAY, 4), clock.Session.PRE)
    assert clock.next_session_change(at(MONDAY, 9, 29)) == (at(MONDAY, 9, 30), clock.Session.REGULAR)
    half_day = dt.date(2026, 11, 27)
    assert clock.next_session_change(at(half_day, 12)) == (at(half_day, 13), clock.Session.POST)
    # Friday night -> Tuesday morning, past the weekend and Labor Day
    assert clock.next_session_change(at(dt.date(2026, 9, 4), 20, 30)) == (at(dt.date(2026, 9, 8), 4), clock.Session.PRE)


# ---------------------------------------------------------------- scan settings and schedule
def test_scan_settings_are_validated():
    s = ScanSettings()
    assert s.changed(premarket_time="07:05", cycle_minutes="4").as_dict() == {
        "premarket_time": "07:05", "gapper_time": "09:15", "cycle_minutes": 4, "hot_list_size": 20,
        "sector_queue_size": 25, "wide_minutes": 30, "wide_stocks": 0}
    with pytest.raises(ValueError, match="04:00 to 09:00"):
        s.changed(premarket_time="09:15")
    assert s.changed(gapper_time="08:05").gapper_time == "08:05"
    with pytest.raises(ValueError, match="08:00 to 09:25"):
        s.changed(gapper_time="09:30")
    with pytest.raises(ValueError, match="look like 09:15"):
        s.changed(gapper_time="later")
    with pytest.raises(ValueError, match="look like"):
        s.changed(premarket_time="soon")
    with pytest.raises(ValueError, match="between 3 and 5"):
        s.changed(cycle_minutes=2)
    assert ScanSettings.load({"cycle_minutes": 999}, s) == s               # a bad saved value falls back
    assert ScanSettings.load({"hot_list_size": 30, "bogus": 1}, s).hot_list_size == 30


def test_the_gap_check_runs_once_before_the_open():
    s = ScanSettings(gapper_time="09:15")
    assert not schedule.gap_check_due(at(MONDAY, 9, 10), s, MONDAY, None)          # not yet
    assert schedule.gap_check_due(at(MONDAY, 9, 15), s, MONDAY, None)
    assert schedule.gap_check_due(at(MONDAY, 9, 29), s, MONDAY, None)
    assert not schedule.gap_check_due(at(MONDAY, 9, 30), s, MONDAY, None)          # the open: the cycles take over
    assert not schedule.gap_check_due(at(MONDAY, 9, 20), s, MONDAY, MONDAY)        # done for this session
    assert not schedule.gap_check_due(at(MONDAY, 9, 20), s, dt.date(2026, 9, 11), None)   # Friday's watchlist
    assert not schedule.gap_check_due(at(MONDAY, 9, 20), s, None, None)
    assert not schedule.gap_check_due(at(dt.date(2026, 9, 12), 9, 20), s, dt.date(2026, 9, 12), None)   # Saturday


def test_which_session_a_watchlist_is_for():
    assert schedule.watchlist_session(at(MONDAY, 8)) == MONDAY
    assert schedule.watchlist_session(at(MONDAY, 16, 30)) == dt.date(2026, 9, 15)
    assert schedule.watchlist_session(at(dt.date(2026, 9, 12), 11)) == MONDAY          # Saturday
    assert schedule.watchlist_session(at(dt.date(2026, 9, 7), 10)) == dt.date(2026, 9, 8)   # Labor Day
    assert schedule.last_completed_session(at(MONDAY, 8)) == dt.date(2026, 9, 11)
    assert schedule.last_completed_session(at(MONDAY, 16, 5)) == MONDAY


def test_the_full_scan_runs_once_a_day_before_the_open():
    s, friday = ScanSettings(premarket_time="08:30"), dt.date(2026, 9, 11)
    assert schedule.full_scan_due(at(MONDAY, 2), s, None, have_any=False)        # nothing at all: build one now
    assert not schedule.full_scan_due(at(MONDAY, 7), s, friday, have_any=True)   # not time yet
    assert schedule.full_scan_due(at(MONDAY, 8, 31), s, friday, have_any=True)
    assert schedule.full_scan_due(at(MONDAY, 11), s, friday, have_any=True)      # started late: catch up
    assert not schedule.full_scan_due(at(MONDAY, 11), s, MONDAY, have_any=True)  # already built today
    assert not schedule.full_scan_due(at(MONDAY, 20), s, MONDAY, have_any=True)  # tomorrow's waits for its time
    assert schedule.next_full_scan_at(at(MONDAY, 7), s, friday) == at(MONDAY, 8, 30)
    assert schedule.next_full_scan_at(at(MONDAY, 11), s, MONDAY) == at(dt.date(2026, 9, 15), 8, 30)


# ---------------------------------------------------------------- the day's watchlist
def _metric(symbol: str, heat: float) -> DailyMetrics:
    return DailyMetrics(symbol, 50.0, 1e8, 3.0, 2.0, 1.5, 0.8, heat)


def _sector(symbol: str) -> str:
    return {"T": "Technology", "E": "Energy"}.get(symbol[0], "")


@pytest.fixture
def watchlist():
    ranked = ([_metric(f"T{i}", 1 - i / 100) for i in range(10)] + [_metric("U1", 0.8)]
              + [_metric(f"E{i}", 0.5 - i / 100) for i in range(5)])
    return DayWatchlist.build(MONDAY, dt.date(2026, 9, 11), ranked, _sector, hot_size=6, queue_size=3,
                              universe=100, liquid=16)


def test_the_hot_list_takes_no_more_than_a_third_from_one_sector(watchlist):
    assert watchlist.hot_symbols() == ["T0", "T1", "E0", "E1"]                  # U1 has no sector: skipped
    assert {s: [c.symbol for c in q] for s, q in watchlist.queues.items()} == {
        "Technology": ["T2", "T3", "T4"], "Energy": ["E2", "E3", "E4"]}


def test_a_cycle_adopts_keeps_or_drops_each_buffer_name(watchlist):
    picks = watchlist.next_picks(per_sector=1)
    decisions = watchlist.apply_cycle({"T0": 0.2, "T1": 0.5, "E0": 0.6, "E1": 0.7, "T2": 0.9, "E2": 0.1},
                                      picks, kept_per_sector=1)
    assert {d.symbol: (d.action, d.replaced) for d in decisions} == {"T2": ("adopted", "T0"), "E2": ("kept", "")}
    assert watchlist.hot_symbols() == ["T2", "T1", "E0", "E1"]
    assert watchlist.kept_symbols() == ["E2"] and watchlist.searched == {"Technology": 1, "Energy": 1}
    assert [c.symbol for c in watchlist.queues["Technology"]] == ["T3", "T4"]

    picks = watchlist.next_picks(per_sector=1)                                   # T3 and E3
    decisions = watchlist.apply_cycle({"T1": 0.5, "E0": 0.6, "E1": 0.7, "T2": 0.9, "E2": 0.3, "E3": 0.2},
                                      picks, kept_per_sector=1)
    assert {d.symbol: d.action for d in decisions} == {"T3": "dropped", "E2": "kept", "E3": "dropped"}


def test_a_watchlist_survives_a_restart_and_old_ones_are_pruned(watchlist, tmp_path):
    watchlist.apply_cycle({"T2": 0.9}, watchlist.next_picks(1), kept_per_sector=1)
    watchlist.save(tmp_path)
    back = DayWatchlist.load_latest(tmp_path)
    assert back.state() == watchlist.state() and back.ranked == watchlist.ranked and back.state()["ranked"] == 16
    for day in range(1, 8):
        DayWatchlist.build(dt.date(2026, 8, day), dt.date(2026, 8, day), [], _sector, 5, 10, 0, 0).save(tmp_path)
    assert len(list(tmp_path.glob("watchlist_*.json"))) == 5


# ---------------------------------------------------------------- the play board
def _play(symbol: str, minutes_left: float = 45) -> Play:
    return Play(symbol=symbol, side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
                timeframe=Timeframe.INTRADAY, entry=10.0, stop=9.5, targets=[11.0],
                expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes_left))


def test_a_cycle_replaces_only_the_plays_for_the_symbols_it_scanned():
    board = PlayBoard()
    swing, old_hot, stale = _play("SWG"), _play("HOT"), _play("OLD", minutes_left=-1)
    board.replace([swing, old_hot, stale])
    new_hot = _play("HOT")
    board.replace([new_hot], scanned=["HOT", "BUF"])
    assert set(board.plays) == {swing.id, new_hot.id}                          # the expired one went too
    assert [c.play.symbol for c in board.drop(lambda p: p.symbol != "SWG", "its strategy was switched off")] == ["SWG"]
    board.replace([], scanned=None)                                             # a full scan covers everything
    assert len(board) == 0


def test_settings_saved_under_the_old_limits_are_pulled_into_range():
    defaults = ScanSettings(cycle_minutes=15)                     # an old config.yaml
    assert ScanSettings.load({"cycle_minutes": 15, "hot_list_size": 30}, defaults).as_dict() == {
        "premarket_time": "08:30", "gapper_time": "09:15", "cycle_minutes": 5, "hot_list_size": 30,
        "sector_queue_size": 25, "wide_minutes": 30, "wide_stocks": 0}
    assert ScanSettings.load({"wide_minutes": 5}, defaults).wide_minutes == 15      # too often: pulled into range
    assert ScanSettings.load(None, defaults).cycle_minutes == 5


def test_the_replay_takes_the_full_scans_leaders_as_well_as_the_watchlist(watchlist):
    from tos_bot.research.history import replay_symbols

    assert watchlist.leaders(3) == ["T0", "T1", "T2"] and len(watchlist.leaders(0)) == 16
    day = replay_symbols(watchlist)
    assert day["intraday"] == ["T0", "T1", "E0", "E1"]                              # the hot list, nothing kept yet
    assert set(day["swing"]) == {"T0", "T1", "E0", "E1", "T2", "T3", "T4", "E2", "E3", "E4"}   # plus the queues
    wide = replay_symbols(watchlist, swing_stocks=12)
    assert wide["intraday"] == day["intraday"]                                       # day trades cost requests: unchanged
    assert set(wide["swing"]) == set(day["swing"]) | set(watchlist.leaders(12)) and "U1" in wide["swing"]
    assert set(replay_symbols(watchlist, swing_stocks=0)["swing"]) == set(day["swing"])
    assert replay_symbols(None, 400) == {"intraday": [], "swing": []}


def test_the_wide_scan_runs_every_quarter_hour_at_most_or_not_at_all():
    assert ScanSettings().wide_on and ScanSettings().changed(wide_minutes=0).wide_on is False
    assert ScanSettings().changed(wide_minutes=120, wide_stocks=500).as_dict()["wide_stocks"] == 500
    with pytest.raises(ValueError, match="15 to 120"):
        ScanSettings().changed(wide_minutes=10)
    with pytest.raises(ValueError, match="between 0 and 6000"):
        ScanSettings().changed(wide_stocks=7000)


def test_a_wide_look_refreshes_the_hot_list_and_keeps_the_best_of_the_rest(watchlist):
    heat = {"T0": 0.2, "T1": 0.5, "E0": 0.6, "E1": 0.7,          # the hot list
            "T7": 0.95, "T8": 0.9, "T9": 0.3, "E4": 0.8, "E3": 0.1, "U1": 0.99}
    decisions = watchlist.apply_wide(heat, _sector, kept_per_sector=1)
    by = {d.symbol: (d.action, d.replaced) for d in decisions}
    assert by["T7"] == ("adopted", "T0") and by["T8"] == ("adopted", "T1")      # Technology is full: its own coolest go
    assert by["E4"] == ("adopted", "E0")                                        # Energy is full too: its coolest goes
    assert by["T1"] == ("kept", "") and by["E0"] == ("kept", "")               # the displaced, still the best of the rest
    assert "T9" not in by and "E3" not in by and "U1" not in by                  # no slot left, no sector; no drops recorded
    assert set(watchlist.hot_symbols()) == {"T7", "T8", "E4", "E1"} and set(watchlist.kept_symbols()) == {"T1", "E0"}
    assert all(c.heat is not None for c in watchlist.hot)
    assert "T7" not in [c.symbol for q in watchlist.queues.values() for c in q]   # left its queue

