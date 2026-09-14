"""Sector names and the sector filter."""

from __future__ import annotations

from tos_bot.data.sectors import SECTORS, canonical_sector, clean_sector_list, sector_allowed, sector_from_ibkr


def test_canonical_names():
    assert canonical_sector("Consumer Cyclical") == "Consumer Discretionary"
    assert canonical_sector("consumer defensive") == "Consumer Staples"
    assert canonical_sector("Financial Services") == "Financials"
    assert canonical_sector("Basic Materials") == "Materials"
    assert canonical_sector("technology") == "Technology"
    assert canonical_sector(None) == ""


def test_selecting_everything_or_nothing_means_no_filter():
    assert clean_sector_list(SECTORS) == []
    assert clean_sector_list([]) == []
    assert clean_sector_list(["energy", "Health Care", "bogus"]) == ["Healthcare", "Energy"]


def test_sector_allowed():
    assert sector_allowed("", [])                        # no filter
    assert sector_allowed("Consumer Cyclical", ["Consumer Discretionary"])
    assert not sector_allowed("Energy", ["Technology"])
    assert not sector_allowed("", ["Technology"])        # unknown is excluded while filtering


def test_ibkr_industries_map_onto_the_sectors():
    for industry in ("Technology", "Communications", "Consumer, Cyclical", "Consumer, Non-cyclical",
                     "Financial", "Industrial", "Energy", "Utilities", "Basic Materials"):
        assert sector_from_ibkr(industry, "") in SECTORS
    # a few categories override their industry
    assert sector_from_ibkr("Consumer, Non-cyclical", "Biotechnology") == "Healthcare"
    assert sector_from_ibkr("Financial", "REITS") == "Real Estate"
    assert sector_from_ibkr("Funds", "Equity Fund") == ""
