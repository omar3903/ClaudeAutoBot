"""The daily review: the mistakes it finds, the plays not taken followed to their outcome, the
strategies' real records against the replay, the lessons - and where reviews are kept."""

from __future__ import annotations

import datetime as dt

from test_replay import EXACT, FLAT, _session
from tos_bot.core.enums import Side, StrategyKind, Timeframe
from tos_bot.core.models import Play
from tos_bot.research.journal import Journal, build_review, first_sightings, live_records, review_day
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
              skip_noise=["against_trend"], min_confirmations=2, passes=lambda row: True, styles={"s": "momentum"},
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
    repo.save_review(today, {"session": today.isoformat(), "day": {"trades": 1, "total_r": 1.0, "realized_pl": 10.0},
                             "mistakes": [], "lessons": ["y"]})
    assert repo.get_review(today)["lessons"] == ["y"]
    assert any(r["session"] == today.isoformat() and r["trades"] == 1 for r in repo.list_reviews())
