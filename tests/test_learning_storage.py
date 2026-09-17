"""What the app keeps for learning: the features a play is described by, kept next to what
happened to it - for the trades taken, the plays shown and not taken, and the replay's trades."""
from __future__ import annotations

import datetime as dt

from test_journal import _review
from test_replay import EXACT, QUIET, _LongAtBar
from tos_bot.core.enums import Side, StrategyKind, Timeframe
from tos_bot.core.models import Play
from tos_bot.research import dataset
from tos_bot.research.features import FEATURE_KEYS, FEATURE_SCHEMA, activity_summary, play_features
from tos_bot.research.replay import SimTrade, replay_intraday
from tos_bot.util import clock

DAY = dt.date(2026, 9, 10)                                    # a Thursday, an ordinary session
UTC = dt.timezone.utc


def _play(**kw):
    d = dict(symbol="FEA", side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
             timeframe=Timeframe.INTRADAY, entry=100.0, stop=98.0, targets=[104.0])
    d.update(kw)
    p = Play(**d)
    p.suggested_qty = 10
    return p


def _rich_play():
    p = _play(sector="Technology")
    p.probability, p.score, p.confidence = 0.58, 1.25, 0.7
    p.tags, p.noise, p.confirmations = ["gap"], ["against_trend"], 2
    p.evidence = {"expected_r": 0.4, "evidence_weight": 1.1,
                  "activity": {"rvol": 2.5, "gap_pct": 3.1, "atr_pct": 2.0},
                  "market_regime": {"p_turbulent": 0.2, "regime": "calm"},
                  "vol_forecast": {"vol": 0.02, "recent_vol": 0.015, "ratio": 1.33, "model": "garch"},
                  "price_character": {"character": "trending", "hurst": 0.6, "variance_ratio_z": 2.1,
                                      "half_life_bars": None},
                  "market_move": {"z": 1.5, "beta": 1.1, "move_pct": 2.0, "news": 1},
                  "next_earnings": {"sessions": 4}, "signal_nudge": 0.05}
    return p


# ---------------------------------------------------------------- the features
def test_every_play_gets_the_same_feature_keys_in_the_same_order():
    f = play_features(_rich_play(), now=dt.datetime(2026, 9, 10, 10, 15, tzinfo=clock.NY), by="autopilot")
    assert tuple(f) == FEATURE_KEYS and f["schema"] == FEATURE_SCHEMA
    assert (f["strategy"], f["kind"], f["timeframe"], f["side"], f["sector"]) == (
        "vwap_reclaim", "TECHNICAL", "INTRADAY", "LONG", "Technology")
    assert (f["confidence"], f["probability"], f["score"], f["expected_r"], f["evidence_weight"]) == (0.7, 0.58, 1.25, 0.4, 1.1)
    assert (f["stop_pct"], f["target_pct"], f["n_targets"]) == (2.0, 4.0, 1)
    assert f["noise"] == ["against_trend"] and f["n_noise"] == 1 and f["confirmations"] == 2
    assert f["tags"] == ["gap"] and f["has_catalyst"] is True
    assert (f["minutes_since_open"], f["time_of_day"], f["weekday"]) == (45.0, "OPEN", 3)
    assert (f["p_turbulent"], f["regime"], f["rvol"], f["gap_pct"], f["heat"]) == (0.2, "calm", 2.5, 3.1, None)
    assert (f["vol"], f["vol_ratio"], f["vol_model"], f["price_character"], f["hurst"]) == (0.02, 1.33, "garch", "trending", 0.6)
    assert f["half_life_bars"] is None and (f["market_z"], f["news_since_move"], f["sessions_to_earnings"]) == (1.5, 1, 4)
    assert f["signal_nudge"] == 0.05 and f["replay_trades"] is None and f["by"] == "autopilot"


def test_a_play_row_from_the_database_gives_the_same_features():
    row = {"symbol": "ROW", "side": "SHORT", "strategy": "s", "kind": "TECHNICAL", "timeframe": "SWING",
           "entry": 50.0, "stop": 52.0, "targets": [46.0, 44.0], "confidence": 0.6, "score": 1.2, "reward_risk": 2.0,
           "noise": [], "confirmations": 1, "created_at": "2026-09-10T18:00:00",       # 14:00 in New York
           "evidence": {"at_entry": {"replay_record": {"trades": 40, "expectancy_r": 0.3,
                                                       "out_of_sample": {"expectancy_r": 0.1}},
                                     "risk_pct": 0.5, "unproven": False, "by": "operator"}}}
    f = play_features(row)
    assert (f["side"], f["stop_pct"], f["target_pct"], f["n_targets"]) == ("SHORT", 4.0, 8.0, 2)
    assert (f["time_of_day"], f["minutes_since_open"], f["weekday"]) == ("MIDDAY", 270.0, 3)
    assert (f["replay_trades"], f["replay_expectancy_r"], f["held_out_r"], f["risk_pct"], f["unproven"], f["by"]) == (
        40, 0.3, 0.1, 0.5, False, "operator")
    assert f["probability"] is None and f["rvol"] is None and f["has_catalyst"] is False
    again = play_features(row, noise=["conflict"], confirmations=3)      # the replay flags a play separately
    assert (again["noise"], again["n_noise"], again["confirmations"]) == (["conflict"], 1, 3)
    assert play_features({"symbol": "BARE"})["schema"] == FEATURE_SCHEMA  # nothing known: every key, all None


def test_the_activity_summary_keeps_the_metrics_and_drops_the_rest():
    from tos_bot.scanner.heat import IntradayMetrics

    m = IntradayMetrics(symbol="ACT", rvol=2.0, change_pct=1.0, gap_pct=0.5, range_atr=1.2, atr_pct=3.0, heat=0.4)
    summary = activity_summary(m)
    assert "symbol" not in summary and summary["rvol"] == 2.0 and summary["heat"] == 0.4
    assert play_features(_play(), activity=m)["range_atr"] == 1.2


# ---------------------------------------------------------------- the database
def test_a_trade_keeps_its_features_at_the_fill_and_when_the_order_went_out(repo):
    p = _rich_play()
    p.symbol = "CTX"
    repo.record_play(p)
    assert repo.get_play(p.id)["probability"] == 0.58
    sent = dt.datetime(2026, 9, 10, 14, 0, tzinfo=UTC)
    context = play_features(p, now=dt.datetime(2026, 9, 10, 10, 0, tzinfo=clock.NY), by="autopilot")
    tid = repo.open_trade(p, 100.0, 10, "paper", entry_context=context, submitted_at=sent)
    t = repo.get_trade(tid)
    assert t["entry_context"]["schema"] == FEATURE_SCHEMA and t["entry_context"]["by"] == "autopilot"
    assert t["submitted_at"] == "2026-09-10T14:00:00" and t["mfe_at"] is None

    repo.update_trade_risk(tid, mfe=1.0)
    first = repo.get_trade(tid)["mfe_at"]
    assert first
    repo.update_trade_risk(tid, mfe=0.5)                      # not a new high - the time stays
    assert repo.get_trade(tid)["mfe_at"] == first
    repo.update_trade_risk(tid, mfe=2.0)
    assert repo.get_trade(tid)["mfe_at"] >= first
    assert repo.close_trade(tid, 104.0, "target")["r_multiple"] == 2.0


def _sim(entered, r, **kw):
    d = dict(strategy="s", symbol="SIM", side="LONG", timeframe="INTRADAY", entered_at=entered,
             exited_at=entered.replace("10:00", "11:00"), entry=100.0, exit=100.0 + r, r=r, exit_reason="target",
             features={"schema": FEATURE_SCHEMA, "confidence": 0.7})
    d.update(kw)
    return SimTrade(**d)


def test_the_replays_trades_are_kept_per_run_and_replaced_when_it_runs_again(repo):
    trades = [_sim("2026-09-10T10:00:00-04:00", 2.0), _sim("2026-09-14T10:00:00-04:00", -1.0)]
    run = repo.save_sim_trades("2026-09-16T16:06:53.593823+00:00", trades, {"INTRADAY": "2026-09-12", "SWING": None})
    assert run == "rpl_20260916160653"
    rows = repo.sim_trades(run)
    assert [(r["r"], r["held_out"]) for r in rows] == [(2.0, False), (-1.0, True)]
    assert rows[0]["entered_at"] == "2026-09-10T14:00:00" and rows[0]["exit"] == 102.0    # kept as UTC
    assert rows[0]["features"]["confidence"] == 0.7 and rows[0]["schema"] == FEATURE_SCHEMA
    assert repo.save_sim_trades("2026-09-16T16:06:53.593823+00:00", trades[:1], None) == run
    assert len(repo.sim_trades(run)) == 1                       # the same run saved again replaces its rows
    assert run in [r["run_id"] for r in repo.sim_runs()]


def _shadow(pid, filled=True):
    return {"play_id": pid, "symbol": "SHD", "side": "LONG", "strategy": "s", "timeframe": "INTRADAY",
            "seen_at": "2026-09-10T09:57:00-04:00", "passed_checks": True, "filled": filled,
            "entered_at": "2026-09-10T10:00:00-04:00" if filled else None,
            "exited_at": "2026-09-10T10:30:00-04:00" if filled else None,
            "entry": 100.0 if filled else None, "exit": 102.0 if filled else None, "r": 2.0 if filled else None,
            "mfe_r": 2.0 if filled else None,
            "exit_reason": "target" if filled else "no fill - the price had already moved away",
            "noise": [], "confirmations": 2, "features": {"schema": FEATURE_SCHEMA, "confidence": 0.7}}


def test_the_plays_not_taken_are_kept_once_each(repo):
    rows = [_shadow("play_shadow_a"), _shadow("play_shadow_b", filled=False)]
    assert repo.save_shadow_trades(DAY, rows) == 2
    assert repo.save_shadow_trades(DAY, rows) == 2                 # saved again: the same two rows
    kept = {r["play_id"]: r for r in repo.shadow_trades(DAY)}
    assert kept["play_shadow_a"]["r"] == 2.0 and kept["play_shadow_a"]["features"]["confidence"] == 0.7
    assert kept["play_shadow_a"]["entered_at"] == "2026-09-10T14:00:00" and kept["play_shadow_a"]["schema"] == 1
    assert kept["play_shadow_b"]["filled"] is False and kept["play_shadow_b"]["r"] is None


# ---------------------------------------------------------------- where the rows come from
def test_replayed_trades_carry_the_features_the_play_had_at_the_signal():
    import dataclasses

    import fakes

    bars = fakes.intraday_bars("FTR")
    trades = replay_intraday([_LongAtBar()], "FTR", bars, fakes.daily_bars("FTR"), EXACT, QUIET, sessions=2)
    assert trades
    f = trades[0].features
    assert tuple(f) == FEATURE_KEYS and f["schema"] == FEATURE_SCHEMA
    assert (f["strategy"], f["timeframe"], f["side"]) == ("long_at_bar", "INTRADAY", "LONG")
    assert f["confirmations"] in (1, 2) and f["time_of_day"] in ("OPEN", "LATE_MORNING", "MIDDAY", "CLOSE")
    assert f["expected_r"] is not None and f["rvol"] is not None       # the readings the replay has
    back = SimTrade(**dataclasses.asdict(trades[0]))                     # the way it crosses worker processes
    assert back.features == f


def test_the_runner_hands_its_trades_to_the_sink(tmp_path):
    from types import SimpleNamespace

    import fakes
    from tos_bot.research.history import IntradayHistory
    from tos_bot.research.runner import ReplayRunner

    kept = []
    runner = ReplayRunner(tmp_path / "replay.json", IntradayHistory(tmp_path / "intraday"),
                          bus=SimpleNamespace(publish=lambda *a, **k: None), workers=1,
                          sink=lambda data, trades: kept.append((data["ran_at"], len(trades))))
    runner.start(strategies=[_LongAtBar()], source=fakes.FakeGateway(["SNK"]), daily_frame=fakes.daily_bars,
                 intraday_symbols=["SNK"], swing_symbols=[], sessions=3, swing_sessions=30, settings=EXACT, noise=QUIET)
    runner.wait(60)
    state = runner.state([], 1)
    assert kept == [(state["ran_at"], state["trade_count"])] and state["trade_count"] > 0


def test_the_review_follows_the_plays_not_taken_with_their_features():
    row = _review()["shadows"]["plays"][0]
    assert row["timeframe"] == "INTRADAY" and row["filled"] and row["entered_at"] and row["exit"] > row["entry"]
    assert row["features"]["schema"] == FEATURE_SCHEMA and row["features"]["time_of_day"] == "OPEN"
    assert row["features"]["confidence"] == 0.7 and row["features"]["stop_pct"] == 1.0


# ---------------------------------------------------------------- the training set
def test_the_training_set_joins_the_three_populations(repo, tmp_path):
    p = _rich_play()
    p.symbol = "SET"
    repo.record_play(p)
    tid = repo.open_trade(p, 100.0, 10, "paper", entry_context=play_features(p, now=dt.datetime.now(UTC), by="operator"))
    repo.update_trade_risk(tid, mfe=3.0)
    repo.close_trade(tid, 104.0, "target")
    repo.save_shadow_trades(DAY, [_shadow("play_shadow_set"), _shadow("play_shadow_set2", filled=False)])
    run = repo.save_sim_trades("2031-01-05T10:00:00+00:00", [_sim("2030-12-20T10:00:00-05:00", 1.5)], None)

    rows = dataset.training_rows(repo)
    by_id = {(r["source"], r["id"]): r for r in rows}
    live = by_id[("live", tid)]
    assert (live["r"], live["win"], live["mfe_r"], live["confidence"], live["schema"]) == (2.0, True, 1.5, 0.7, FEATURE_SCHEMA)
    shadow = by_id[("shadow", "play_shadow_set")]
    assert shadow["r"] == 2.0 and shadow["win"] is True and ("shadow", "play_shadow_set2") not in by_id   # no fill, no row
    replay = [r for r in rows if r["source"] == "replay"]
    assert replay and all(r["run_id"] == run for r in replay) and replay[0]["r"] == 1.5      # the latest run only
    assert list(rows[0]) == list(dataset.COLUMNS) and dataset.counts(rows)["live"] >= 1

    path = dataset.write_csv(rows, tmp_path / "set.csv")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].split(",") == list(dataset.COLUMNS) and len(lines) == len(rows) + 1
