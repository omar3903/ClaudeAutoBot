from __future__ import annotations

import pytest

from tos_bot.core.enums import Side
from tos_bot.data.market_data import MarketDataService, SyntheticProvider
from tos_bot.strategies import REGISTRY, build_context
from tos_bot.strategies.registry import describe_all


@pytest.fixture(scope="module")
def datasvc():
    return MarketDataService(providers=[SyntheticProvider(seed=5)], cache=False,
                             min_interval_between_calls=0.0)


def test_registry_has_all_families():
    keys = set(REGISTRY)
    assert {"opening_range_breakout", "vwap_reclaim", "rsi2_mean_reversion",
            "relative_value_comps", "dcf_fair_value_gap",
            "valuation_football_field"}.issubset(keys)
    assert len(describe_all()) == len(REGISTRY)


def test_every_strategy_runs_and_geometry_is_sane(datasvc):
    total = 0
    for i in range(30):
        sym = f"S{i:02d}"
        ctx = build_context(sym,
                            datasvc.get_price_history(sym, "5m", 10),
                            datasvc.get_price_history(sym, "1d", 400),
                            datasvc.get_quote(sym),
                            params={"valuation": {}})
        for key, cls in REGISTRY.items():
            for p in cls().generate(ctx):
                total += 1
                assert p.entry > 0 and p.stop > 0 and p.targets
                if p.side is Side.LONG:
                    assert p.stop < p.entry < p.targets[0]
                else:
                    assert p.targets[0] < p.entry < p.stop
                assert 0.3 <= p.reward_risk <= 30
                assert abs(p.entry - p.stop) / p.entry <= 0.30
                assert p.explanation and len(p.explanation) > 40
    # synthetic data is noisy; we just need the pipeline to yield *something*
    assert total >= 1
