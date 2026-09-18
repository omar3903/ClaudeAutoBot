"""A record judged for luck (Aronson), quality (Tharp), drift and costs (Carver)."""

from __future__ import annotations

import numpy as np

from tos_bot.research import significance as sig
from tos_bot.research.replay import SimTrade, drift_per_bar, records_by_strategy


def _rs(mean, n, seed, spread=1.0):
    return list(np.random.default_rng(seed).normal(mean, spread, n))


def test_the_quality_number_is_the_mean_over_its_spread_by_the_root_of_the_count():
    rs = [1.0, -1.0, 2.0, -1.0, 1.5, -0.5, 0.5, 1.0, -1.0, 2.0]
    arr = np.array(rs)
    assert sig.sqn(rs) == round(np.sqrt(10) * arr.mean() / arr.std(ddof=1), 2)
    many = rs * 40                                                   # 400 trades count as 100
    assert sig.sqn(many) == round(10 * np.mean(many) / np.std(many, ddof=1), 2)
    assert sig.sqn([1.0, 2.0]) is None and sig.sqn([1.0] * 10) is None   # too few; no spread to speak of
    assert sig.sqn_grade(1.2) == "hard to trade" and sig.sqn_grade(2.6) == "good" and sig.sqn_grade(None) == ""


def test_a_real_edge_is_told_from_luck_and_the_same_trades_always_give_the_same_answer():
    edge, noise = _rs(0.35, 200, 1), _rs(0.0, 200, 2)
    real, none = sig.luck_test(edge), sig.luck_test(noise)
    assert real["p_value"] < 0.01 and real["ci_low"] > 0
    assert none["p_value"] > 0.10 and none["ci_low"] < 0 < none["ci_high"]
    assert sig.luck_test(edge) == real                               # seeded
    assert sig.luck_test([0.5, 1.0]) is None


def test_the_best_of_many_setups_is_tested_against_the_best_that_luck_makes():
    groups = {f"noise_{i}": _rs(0.0, 60, 10 + i) for i in range(19)}
    lucky = max(groups, key=lambda k: np.mean(groups[k]))
    alone = sig.luck_test(groups[lucky])["p_value"]
    adjusted = sig.reality_check(groups)
    assert adjusted[lucky] > alone and adjusted[lucky] > 0.10        # the winner of 19 coin flips proves nothing
    groups["real"] = _rs(0.45, 150, 99)
    adjusted = sig.reality_check(groups)
    assert adjusted["real"] < 0.05 and min(v for k, v in adjusted.items() if k != "real") > 0.10
    assert sig.reality_check({"thin": [1.0, 2.0]}) == {}


def test_the_marble_bag_shows_the_drawdown_to_expect():
    bag = sig.marble_bag([2.0, -1.0, -1.0, 1.5, -1.0, -1.0, 3.0, -1.0], trades=100)
    assert bag["p95_drawdown_r"] <= bag["median_drawdown_r"] < 0 and 0 <= bag["p_losing_run"] <= 1
    assert sig.marble_bag([1.0]) is None


def test_costs_are_measured_against_the_edge_before_them():
    assert sig.cost_share(0.05, 0.04) == 0.444 and sig.cost_share(0.05, 0.04) > sig.SPEED_LIMIT
    assert sig.cost_share(0.30, 0.04) < sig.SPEED_LIMIT
    assert sig.cost_share(-0.10, 0.04) is None                       # no edge to take a share of


def _trade(strategy, r, drift=0.0, cost=0.04, day=1):
    return SimTrade(strategy=strategy, symbol="AAA", side="LONG", timeframe="SWING",
                    entered_at=f"2026-03-{day:02d}T09:35:00-04:00", exited_at=f"2026-03-{day:02d}T15:00:00-04:00",
                    entry=100.0, exit=101.0, r=r, exit_reason="target", drift_r=drift, cost_r=cost)


def test_a_strategys_record_is_judged_net_of_drift_and_against_every_setup_tried():
    good = [_trade("good", r, drift=0.05, day=1 + i % 28) for i, r in enumerate(_rs(0.5, 80, 5))]
    flat = [_trade("flat", r, day=1 + i % 28) for i, r in enumerate(_rs(0.0, 80, 6))]
    records = records_by_strategy(good + flat)
    rec = records["good"]
    assert abs(rec["edge_r"] - (rec["expectancy_r"] - 0.05)) < 0.002   # the drift is not the setup's doing
    assert rec["p_adjusted"] < 0.05 < records["flat"]["p_adjusted"] and rec["setups_tested"] == 2
    assert rec["sqn"] > 2 and rec["cost_r"] == 0.04 and rec["cost_share"] < sig.SPEED_LIMIT
    assert rec["drawdown"]["median_drawdown_r"] < 0


def test_the_drift_of_a_stock_is_its_average_move_per_bar():
    import pandas as pd

    days = pd.date_range("2026-03-02", periods=50, freq="B", tz="America/New_York")
    daily = pd.DataFrame({"open": 100.0, "close": [100.0 * 1.002 ** i for i in range(50)]}, index=days)
    assert abs(drift_per_bar(daily, intraday=False) - np.log(1.002) * 49 / 50) < 1e-9
    assert drift_per_bar(None, intraday=False) == 0.0 and drift_per_bar(daily.iloc[:1], intraday=True) == 0.0
    bars = pd.DataFrame({"open": [100.0, 100.5, 102.0, 102.2], "close": [100.5, 101.0, 102.2, 102.0]},
                        index=pd.to_datetime(["2026-03-02 09:30", "2026-03-02 09:35", "2026-03-03 09:30",
                                              "2026-03-03 09:35"]).tz_localize("America/New_York"))
    inside = np.log(101.0 / 100.0) + np.log(102.0 / 102.0)          # the overnight gap is not a day trade's drift
    assert abs(drift_per_bar(bars, intraday=True) - inside / 4) < 1e-9
