"""Routing table: which connection is held and where orders go."""

from __future__ import annotations

from tos_bot.brokers.venues import (
    PAPER_PLATFORMS, VenuePlan, normalize, plan_venue, venue_id, venue_label,
)


def test_live_mode_always_trades_on_the_live_broker():
    for pp in PAPER_PLATFORMS:
        assert plan_venue("live", pp, "ibkr") == VenuePlan("ibkr", "live", True)
        assert plan_venue("live", pp, "schwab") == VenuePlan("schwab", "live", True)


def test_paper_platforms():
    assert plan_venue("paper", "ibkr", "schwab") == VenuePlan("ibkr", "paper", True)
    # thinkorswim paperMoney has no API: Schwab data, simulated fills
    assert plan_venue("paper", "schwab", "ibkr") == VenuePlan("schwab", "live", False)
    # the simulator borrows the live broker's feed, read-only
    assert plan_venue("paper", "simulator", "ibkr") == VenuePlan("ibkr", "paper", False)
    assert plan_venue("paper", "simulator", "schwab") == VenuePlan("schwab", "live", False)


def test_venue_ids_decide_where_exits_go():
    assert venue_id(plan_venue("paper", "simulator", "ibkr")) == "paper"
    assert venue_id(plan_venue("paper", "schwab", "ibkr")) == "paper"
    assert venue_id(plan_venue("paper", "ibkr", "schwab")) == "ibkr-paper"
    assert venue_id(plan_venue("live", "ibkr", "ibkr")) == "ibkr-live"
    assert venue_id(plan_venue("live", "simulator", "schwab")) == "schwab"


def test_connection_key_separates_readonly_ibkr_but_not_schwab():
    assert VenuePlan("ibkr", "paper", True).key != VenuePlan("ibkr", "paper", False).key
    assert VenuePlan("schwab", "live", True).key == VenuePlan("schwab", "live", False).key
    assert VenuePlan().key is None


def test_normalize_rejects_unknown_values():
    assert normalize("tda", "crypto") == ("simulator", "schwab")
    assert normalize(" IBKR ", "Ibkr") == ("ibkr", "ibkr")


def test_labels():
    assert "simulator" in venue_label("paper")
    assert "IBKR paper" in venue_label("ibkr-paper")
