"""Sector names and the scan-time sector filter."""

from __future__ import annotations

from types import SimpleNamespace

from tos_bot.data.sectors import SECTORS, canonical_sector, clean_sector_list, sector_allowed
from tos_bot.scanner.scanner import Scanner


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


class _Lookup:
    def __init__(self, known, online):
        self.known, self.online, self.fetched = known, online, []

    def peek(self, sym):
        return self.known.get(sym)

    def get(self, sym):
        self.fetched.append(sym)
        return self.online.get(sym, "")


def test_scanner_filters_the_slice_within_a_lookup_budget():
    look = _Lookup({"AAPL": "Technology", "XOM": "Energy"},
                   {"NEW1": "Technology", "NEW2": "Technology"})
    scanner = SimpleNamespace(_sectors=look, SECTOR_LOOKUPS_PER_CYCLE=1)
    kept = Scanner._in_sectors(scanner, ["AAPL", "XOM", "NEW1", "NEW2"], ["Technology"])
    assert kept == ["AAPL", "NEW1"]                      # NEW2 is resolved on a later cycle
    assert look.fetched == ["NEW1"]
