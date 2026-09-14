"""Routing table: which IB Gateway connection is held and where orders go."""

from __future__ import annotations

from tos_bot.brokers.venues import PAPER_PLATFORMS, VenuePlan, normalize_platform, plan_venue, venue_id, venue_label


def test_live_always_trades_on_the_live_account():
    for platform in (*PAPER_PLATFORMS, None, "bogus"):
        assert plan_venue("live", platform) == VenuePlan("live", trade=True)


def test_paper_platforms():
    assert plan_venue("paper", "ibkr") == VenuePlan("paper", trade=True)
    # the simulator fills on the paper account's prices; that connection is read-only
    assert plan_venue("paper", "simulator") == VenuePlan("paper", trade=False)


def test_venue_ids_decide_where_exits_go():
    assert venue_id(plan_venue("paper", "simulator")) == "paper"
    assert venue_id(plan_venue("paper", "ibkr")) == "ibkr-paper"
    assert venue_id(plan_venue("live", "simulator")) == "ibkr-live"


def test_a_read_only_connection_is_a_different_connection():
    assert VenuePlan("paper", True).key != VenuePlan("paper", False).key


def test_unknown_platforms_fall_back_to_ibkr():
    assert normalize_platform("schwab") == "ibkr" and normalize_platform(None) == "ibkr"
    assert normalize_platform(" Simulator ") == "simulator"


def test_labels():
    assert "simulator" in venue_label("paper")
    assert "IBKR paper" in venue_label("ibkr-paper")
