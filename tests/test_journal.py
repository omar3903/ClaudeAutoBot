"""The daily review: the mistakes it finds, the plays not taken followed to their outcome, the
strategies' real records against the replay, the lessons - and where reviews are kept."""

from __future__ import annotations

import datetime as dt

import pytest

from test_replay import EXACT, FLAT, _session
from tos_bot.core.enums import Side, StrategyKind, Timeframe
from tos_bot.core.models import Play
from tos_bot.research.journal import (Journal, build_review, first_sightings, live_records, opened_rows, review_day,
                                      trade_rows)
from tos_bot.util import clock

DAY = dt.date(2026, 9, 10)                     # the day test_replay's candles are for


def _trade(tid, r, *, symbol="JRN", entered="14:00", exited="14:30", mfe=0.2, noise=(), confirmations=2,
           regime=None, exit_reason="stop"):
    return {"id": tid, "symbol": symbol, "side": "LONG", "strategy": "s", "timeframe": "INTRADAY", "broker": "paper",
            "entry_time": f"{DAY}T{entered}:00", "exit_time": f"{DAY}T{exited}:00", "entry_price": 100.0,
            "initial_stop_price": 99.0, "exit_price": 100.0 + r, "quantity": 10.0, "r_multiple": r,
            "realized_pl": 10.0 * r, "exit_reason": exit_reason, "mfe": mfe, "mae": 0.5,
            "play": {"rationale": "x", "evidence": {"spark": [1, 2], "at_entry": {
                "noise": list(noise), "confirmations": confirmations, "market_regime": regime or {}}}}}


def _row(pid, created_at, symbol="RPL", status="PROPOSED", noise=()):
    return {"id": pid, "created_at": f"{DAY}T{created_at}:00", "symbol": symbol, "side": "LONG", "strategy": "s",
            "kind": "TECHNICAL", "timeframe": "INTRADAY", "entry": 100.0, "stop": 99.0, "targets": [102.0],
            "noise": list(noise), "confirmations": 2, "score": 1.0, "confidence": 0.7, "reward_risk": 2.0,
            "status": status}


TURBULENT = {"p_turbulent": 0.8, "regime": "turbulent"}
TRADES = [
    _trade("t1", -1.6),                                                          # jumped its stop
    _trade("t2", -0.4, symbol="GVB", mfe=1.3),                                   # was up 1.3R
    _trade("t3", 2.0, symbol="NSY", noise=["against_trend"], confirmations=1, regime=TURBULENT, exit_reason="target"),
    _trade("t4", -1.0, entered="15:00", exited="15:30"),                         # back into JRN after t1
]
PLAYS = [
    _row("p1", "13:57"),                      # 09:57 in New York: filled at 10:00 and hits its target
    _row("p1-again", "14:20"),                # the same setup seen again - followed once
    _row("p2", "13:40", symbol="DONE"),       # a setup that was taken later on
    _row("p3", "13:57", symbol="DONE", status="FILLED"),
]
BARS = {"RPL": _session(FLAT + [(100.0, 100.1, 99.9, 100.0), (100.2, 102.1, 100.1, 101.9)])}


def _review(**over):
    kw = dict(trades=TRADES, plays=PLAYS, rolling=TRADES,
              replay_records={"s": {"trades": 40, "expectancy_r": 0.6, "out_of_sample": {"trades": 12, "expectancy_r": 0.4}}},
              evidence={"s": {"multiplier": 0.9}}, regime=TURBULENT, bars=BARS, settings=EXACT,
              gates={"skip_noise": ["against_trend"], "min_confirmations": 2, "source": "at the last entry"},
              passes=lambda row: True, styles={"s": "momentum"},
              titles={"s": "Setup S"}, breakeven_at_r=1.3)
    kw.update(over)
    return build_review(DAY, **kw)


def test_the_review_finds_the_mistakes_in_a_session():
    review = _review()
    found = {(m["kind"], m["trade_id"]) for m in review["mistakes"]}
    assert {("loss_beyond_stop", "t1"), ("gave_back_winner", "t2"), ("took_noise", "t3"), ("unconfirmed", "t3"),
            ("against_regime", "t3"), ("reentered_after_loss", "t4")} <= found
    assert review["day"]["trades"] == 4 and review["day"]["total_r"] == -1.0 and review["day"]["realized_pl"] == -10.0
    rows = {t["id"]: t for t in review["trades"]}
    assert rows["t2"]["mfe_r"] == 1.3 and "spark" not in rows["t1"]["evidence"]


def test_the_plays_not_taken_are_followed_to_their_outcome_once_each():
    assert [p["id"] for p in first_sightings(PLAYS)] == ["p1"]
    shadows = _review()["shadows"]
    assert (shadows["followed"], shadows["filled"]) == (1, 1)
    assert shadows["plays"][0]["r"] == 2.0 and shadows["best_missed"][0]["symbol"] == "RPL"
    assert _review(bars=None)["shadows"]["note"].startswith("IB Gateway wasn't connected")


def test_the_lessons_and_the_strategies_real_record_against_the_replay():
    review = _review()
    text = " ".join(review["lessons"])
    assert "more than the planned 1R" in text and "best play not taken" in text and "turbulent" in text
    [row] = review["strategies"]
    assert (row["live_trades"], row["live_r"], row["replay_r"], row["drifting"]) == (4, -0.25, 0.6, False)
    losing = [_trade(f"r{i}", -0.2, symbol=f"L{i}") for i in range(12)]
    [drifting] = _review(rolling=losing)["strategies"]
    assert drifting["drifting"] and live_records(losing)["s"]["trades"] == 12


def test_break_even_is_not_a_loss_and_the_1r_lesson_counts_every_loss_past_1r():
    """A winner that came back to break-even didn't close at a loss. The lesson counts every loss past 1R, and
    apart from it the ones more than 0.2R past the stop - a loss a little past 1R is a stop's slippage and commission."""
    trades = [_trade("b1", 0.0, symbol="AAA", mfe=1.4),                          # up 1.4R, out at break-even
              _trade("b2", -1.05, symbol="BBB"), _trade("b3", -1.19, symbol="CCC"),
              _trade("b4", -1.23, symbol="DDD"),
              _trade("b5", -1.001, symbol="EEE")]                                # -1.00R to the hundredth
    review = _review(trades=trades, rolling=trades)
    kinds = [(m["kind"], m["trade_id"]) for m in review["mistakes"]]
    assert not [tid for kind, tid in kinds if kind == "gave_back_winner"]
    [beyond] = [m for m in review["mistakes"] if m["kind"] == "loss_beyond_stop"]
    assert beyond["trade_id"] == "b4" and beyond["detail"].startswith("lost -1.23R - 0.23R past the planned stop")
    assert review["day"]["lost_over_1r"] == 3
    assert ("3 trades lost more than the planned 1R (worst -1.23R); 1 went more than 0.2R past the stop."
            in " ".join(review["lessons"]))

    # a loss past 1R that stayed within the stop's slippage is still told - and not as a jumped stop
    within = [_trade("w1", -1.05, symbol="FFF")]
    review = _review(trades=within, rolling=within)
    assert all(m["kind"] != "loss_beyond_stop" for m in review["mistakes"])
    assert "1 trade lost more than the planned 1R (-1.05R), none more than 0.2R past the stop" in " ".join(review["lessons"])


def test_fill_times_are_a_true_median_over_the_fills_they_say_and_a_slow_market_exit_is_counted():
    """An even count takes the mean of the two middle fills; the note says which fills the times cover; an
    exit goes out at market, so one that took minutes is counted and told, not set aside like a resting limit."""
    from tos_bot.research.journal import execution_quality

    def timed(went_in, came_out, entered="2026-09-09T14:00:00"):      # stored in UTC
        return {"entry_time": entered, "exit_time": f"{DAY}T15:00:00", "entry_latency_s": went_in,
                "exit_latency_s": came_out}

    trades = [timed(2.0, 1.0, entered="2026-09-09T02:00:00"),              # the evening before in New York
              timed(4.0, 3.0), timed(10.0, 1_500.0), timed(6.0, 2.0),
              timed(16_500.0, None)]                                     # a limit entry that rested for its price
    out = execution_quality(trades, assumed_bps=6.0)
    assert out["entry_latency_s"] == 5.0 and out["exit_latency_s"] == 2.5     # the two middle ones, averaged
    assert (out["entry_fills"], out["exit_fills"], out["since"]) == (4, 4, "2026-09-08")
    assert (out["slowest_exit_s"], out["slow_exits"], out["rested_entries"]) == (1_500.0, 1, 1)
    note = out["latency_note"]
    assert note.startswith("Since 2026-09-08, orders typically filled in 5.0s going in (4 entries, slowest 10s)")
    assert "2.5s coming out (4 exits, slowest 25 min (1,500s)); 1 market exit took longer than 60s" in note
    assert "1 limit entry rested longer than 60s" in note

    # exits alone still say how they went
    only = execution_quality([{"exit_time": f"{DAY}T15:00:00", "exit_latency_s": 9_000.0}], assumed_bps=6.0)
    assert only["entry_latency_s"] is None and only["exit_latency_s"] == 9_000.0
    assert "orders typically filled in 9,000.0s coming out (1 exit, slowest 2.5 h (9,000s))" in only["latency_note"]


def _plays_at(avg, n, spread=1.0):
    """n plays averaging ``avg``R, spread ``spread``R either side of it."""
    rs = [avg + (spread if i % 2 else -spread) for i in range(n - n % 2)] + [avg] * (n % 2)
    return [{"r": r} for r in rs]


def test_a_check_is_called_helped_or_cost_only_on_a_real_difference():
    """A hundredth of an R between the two sides, or a gap well inside one standard error, is no clear
    difference; a gap of tenths of an R with an ordinary spread is."""
    from tos_bot.research.journal import _compare

    same = _compare(_plays_at(0.257, 12), _plays_at(0.258, 101))
    assert same["verdict"].startswith("no clear difference") and abs(same["t"]) < 0.01
    wide = _compare(_plays_at(-0.1, 6, spread=2.0), _plays_at(0.1, 6, spread=2.0))       # 0.2R apart, t about -0.16
    assert wide["verdict"].startswith("no clear difference")
    helped = _compare(_plays_at(-0.3, 10, spread=0.5), _plays_at(0.3, 10, spread=0.5))
    assert helped["verdict"].startswith("helped") and helped["t"] < -1
    assert _compare(_plays_at(0.3, 10, spread=0.5), _plays_at(-0.3, 10, spread=0.5))["verdict"].startswith("cost")
    assert _compare(_plays_at(0.3, 4), _plays_at(-0.3, 10))["verdict"] == "too few plays to tell"
    assert _review()["shadows"]["checks"]["verdict"] == "too few plays to tell"      # one play followed


def test_a_flag_that_made_no_clear_difference_gets_no_lesson():
    from tos_bot.research.journal import lessons

    shadows = {"filled": 113, "summary": {"trades": 113, "expectancy_r": 0.26, "total_r": 29.1},
               "checks": {"passed": {"trades": 11, "expectancy_r": 0.30}, "turned_away": {"trades": 102, "expectancy_r": 0.25},
                          "verdict": "no clear difference", "t": 0.15},
               "by_noise": {"against_trend": {"flagged": 40, "rest": 73, "flagged_avg_r": 0.23, "rest_avg_r": 0.28,
                                              "t": -0.2, "verdict": "no clear difference - about as well as the rest"},
                            "volume_against": {"flagged": 20, "rest": 93, "flagged_avg_r": -0.3, "rest_avg_r": 0.4,
                                               "t": -2.5, "verdict": "helped - the plays it flags did worse"}}}
    text = " ".join(lessons({"trades": 0}, [], shadows, [], None, {}, 1.3))
    assert "against the daily trend" not in text
    assert "Plays flagged \"heavier volume against it\" would have averaged -0.30R" in text and "helped today" in text
    assert ("passed Autopilot's checks would have averaged +0.30R over 11, the ones its checks turned away +0.25R "
            "over 102 - no clear difference; one session proves little.") in text


def _opened(tid, symbol, *, status="OPEN", noise=(), unproven=None, r=None):
    return {"id": tid, "symbol": symbol, "side": "LONG", "strategy": "s", "timeframe": "SWING", "broker": "paper",
            "status": status, "entry_time": f"{DAY}T18:00:00", "entry_price": 100.0, "initial_stop_price": 98.0,
            "stop_price": 98.0, "target_price": 106.0, "quantity": 10.0, "initial_quantity": 10.0, "r_multiple": r,
            "realized_pl": None if r is None else 20.0 * r, "exit_reason": None if r is None else "target",
            "play": {"rationale": "x", "evidence": {"spark": [1], "at_entry": {
                "noise": list(noise), "confirmations": 3, "unproven": unproven}}}}


def test_a_session_whose_entries_are_all_still_open_is_not_a_session_without_trades():
    opened = [_opened("o1", "AAA"), _opened("o2", "BBB", noise=["against_trend"], unproven="only 3 replayed trades"),
              _opened("o3", "CCC"), _opened("o4", "DDD", status="CLOSED", r=2.0)]
    review = _review(trades=[], rolling=[], opened=opened, marks={"AAA": 103.0, "BBB": 99.0, "DDD": 50.0})
    day = review["day"]
    assert (day["trades"], day["opened"], day["still_open"], day["open_r"], day["open_pl"]) == (0, 4, 3, 1.0, 20.0)
    rows = {row["symbol"]: row for row in review["opened"]}
    assert [rows[s]["open_r"] for s in ("AAA", "BBB", "CCC")] == [1.5, -0.5, None]      # no price, no standing
    assert rows["AAA"]["risk"] == 20.0 and rows["AAA"]["still_open"] and "spark" not in rows["AAA"]["evidence"]
    assert (rows["DDD"]["still_open"], rows["DDD"]["r"], rows["DDD"]["open_r"]) == (False, 2.0, None)
    # what an entry was taken on is judged the day it is taken
    assert {(m["trade_id"], m["kind"]) for m in review["mistakes"]} == {("o2", "took_noise"), ("o2", "unproven_strategy")}
    assert review["lessons"][0] == "No trades closed this session."
    assert review["lessons"][1] == "4 positions opened this session; 3 still open, standing at +1.00R in all at the review."
    assert _review()["opened"] == [] and _review()["day"]["opened"] == 0


def test_the_review_is_due_for_today_after_the_close_and_for_the_session_before_until_then():
    def at(stamp):
        return dt.datetime.fromisoformat(stamp).replace(tzinfo=clock.NY)
    assert review_day(at("2026-09-10T16:30:00")) == DAY
    assert review_day(at("2026-09-10T12:00:00")) == dt.date(2026, 9, 9)
    assert review_day(at("2026-09-12T12:00:00")) == dt.date(2026, 9, 11)          # a Saturday


class _Repo:
    def __init__(self):
        self.saved = {}

    def save_review(self, day, review):
        self.saved[day] = review

    def get_review(self, day):
        return self.saved.get(day)

    def list_reviews(self, limit):
        return [{"session": d.isoformat()} for d in sorted(self.saved, reverse=True)][:limit]


def test_reviews_are_kept_in_the_database_and_as_files(tmp_path):
    journal = Journal(tmp_path / "journal", _Repo())
    journal.save({"session": DAY.isoformat(), "day": {"trades": 0}, "mistakes": [], "lessons": ["x"]})
    assert journal.has(DAY) and (tmp_path / "journal" / f"{DAY}.json").exists()
    assert Journal(tmp_path / "journal", _Repo()).get(DAY)["lessons"] == ["x"]      # the file, when the database lacks it
    assert journal.days()[0]["session"] == DAY.isoformat()


def test_the_repository_keeps_what_the_review_needs(repo):
    play = Play(symbol="JRNL", side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
                timeframe=Timeframe.INTRADAY, entry=100.0, stop=99.0, targets=[102.0])
    play.noise, play.confirmations = ["against_trend"], 3
    play.evidence["at_entry"] = {"noise": ["against_trend"], "confirmations": 3}
    repo.record_play(play)
    tid = repo.open_trade(play, 100.0, 10, "paper")
    repo.close_trade(tid, exit_price=101.0, exit_reason="target")
    today = clock.now_ny().date()
    [trade] = [t for t in repo.closed_trades_between(today, today) if t["id"] == tid]
    assert trade["r_multiple"] == 1.0 and trade["play"]["evidence"]["at_entry"]["confirmations"] == 3
    [row] = [r for r in repo.plays_on(today) if r["id"] == play.id]
    assert row["noise"] == ["against_trend"] and row["confirmations"] == 3
    held = Play(symbol="JRNH", side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
                timeframe=Timeframe.SWING, entry=100.0, stop=99.0, targets=[102.0])
    held.evidence["at_entry"] = {"noise": [], "confirmations": 5}
    repo.record_play(held)
    still = repo.open_trade(held, 100.0, 10, "paper")
    entered = {t["id"]: t for t in repo.trades_opened_between(today, today)}
    assert entered[tid]["status"] == "CLOSED" and entered[still]["status"] == "OPEN"
    assert entered[still]["play"]["evidence"]["at_entry"]["confirmations"] == 5
    repo.save_review(today, {"session": today.isoformat(), "mistakes": [], "lessons": ["y"],
                             "day": {"trades": 1, "total_r": 1.0, "realized_pl": 10.0, "opened": 2, "open_r": 0.7}})
    assert repo.get_review(today)["lessons"] == ["y"]
    assert any(r["session"] == today.isoformat() and (r["trades"], r["opened"], r["open_r"]) == (1, 2, 0.7)
               for r in repo.list_reviews())


def test_a_position_taken_off_in_parts_is_reviewed_whole_and_keeps_its_planned_exit_times(repo):
    play = Play(symbol="AAA", side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
                timeframe=Timeframe.INTRADAY, entry=100.0, stop=99.0, targets=[102.0, 104.0],
                expected_hold_typical=40.0, expected_hold_max=90.0)
    repo.record_play(play)
    tid = repo.open_trade(play, 100.0, 10, "paper")
    repo.reduce_trade(tid, 4, 102.0)                                       # the first target: 4 of the 10 off
    repo.close_trade(tid, exit_price=101.0, exit_reason="eod-flatten")      # the other 6 at the close
    today = clock.now_ny().date()
    [trade] = [t for t in repo.closed_trades_between(today, today) if t["id"] == tid]
    assert (trade["exit_avg_price"], trade["exit_parts"], trade["quantity"]) == (101.4, 2, 6)
    assert trade["expected_exit_at"] and trade["overwatch_at"]                  # a closed trade keeps its plan's times
    [row] = trade_rows([trade])
    assert (row["quantity"], row["exit"], row["last_exit"], row["exit_parts"]) == (10, 101.4, 101.0, 2)
    assert row["pl"] == pytest.approx(14.0) and (row["exit"] - row["entry"]) * row["quantity"] == pytest.approx(row["pl"])
    assert row["exit_reason"] == "eod-flatten (last of 2 exits)"
    [old] = trade_rows([{**trade, "exit_avg_price": None, "exit_parts": 0}])            # a record with no exit fills
    assert (old["exit"], old["exit_reason"]) == (101.0, "eod-flatten")
    [entered] = [t for t in repo.trades_opened_between(today, today) if t["id"] == tid]
    assert entered["expected_exit_at"] == trade["expected_exit_at"]
    assert opened_rows([entered], {})[0]["exit_reason"] == "eod-flatten (last of 2 exits)"
    whole = Play(symbol="BBB", side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
                 timeframe=Timeframe.INTRADAY, entry=100.0, stop=99.0, targets=[102.0])
    repo.record_play(whole)
    one = repo.open_trade(whole, 100.0, 10, "paper")
    repo.close_trade(one, exit_price=102.0, exit_reason="target")
    [trade] = [t for t in repo.closed_trades_between(today, today) if t["id"] == one]
    [row] = trade_rows([trade])
    assert (trade["exit_parts"], row["exit"], row["quantity"], row["exit_reason"]) == (1, 102.0, 10, "target")


def test_the_play_log_keeps_the_hold_so_a_play_rebuilt_from_it_has_its_window(repo):
    from tos_bot.execution.executor import _play_from_row
    from tos_bot.research.journal import _play, held_for

    play = Play(symbol="HLD", side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
                timeframe=Timeframe.INTRADAY, entry=100.0, stop=99.0, targets=[102.0],
                expected_hold_typical=40.0, expected_hold_max=90.0)
    repo.record_play(play)
    row = repo.get_play(play.id)
    assert held_for(row) == (40.0, 90.0)
    rebuilt = _play(row)                                                       # a shadow, in the daily review
    assert (rebuilt.expected_hold_typical, rebuilt.expected_hold_max) == (40.0, 90.0)
    adopted = _play_from_row(row)                                              # an entry taken back after a restart
    assert (adopted.expected_hold_typical, adopted.expected_hold_max) == (40.0, 90.0)
    assert held_for({"evidence": {}}) == (0.0, 0.0) and held_for({"evidence": {"expected_hold": "x"}}) == (0.0, 0.0)

