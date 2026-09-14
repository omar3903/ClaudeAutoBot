"""Exchange rates for accounts kept in a currency other than USD (ECB reference rates)."""

from __future__ import annotations

import pytest

from tos_bot.data import fx

RATES = {"USD": 1.08, "CAD": 1.5, "EUR": 1.0}          # units per 1 EUR


@pytest.fixture(autouse=True)
def _fresh_cache():
    fx.clear_cache()
    yield
    fx.clear_cache()


def test_usd_needs_no_lookup():
    assert fx.usd_per("USD", fetch=lambda: pytest.fail("no lookup for USD")) == 1.0


def test_rates_are_crossed_through_the_euro_and_cached():
    calls = []

    def fetch():
        calls.append(1)
        return RATES

    assert fx.usd_per("cad", fetch=fetch) == pytest.approx(0.72)
    assert fx.usd_per("EUR", fetch=fetch) == pytest.approx(1.08)
    assert fx.usd_per("XYZ", fetch=fetch) is None
    assert len(calls) == 1


def test_a_failed_download_gives_none_and_is_not_retried_straight_away():
    calls = []

    def fetch():
        calls.append(1)
        raise OSError("offline")

    assert fx.usd_per("CAD", fetch=fetch) is None
    assert fx.usd_per("CAD", fetch=fetch) is None
    assert len(calls) == 1


def test_a_stale_rate_beats_none(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(fx.time, "monotonic", lambda: now[0])
    assert fx.usd_per("CAD", fetch=lambda: RATES) == pytest.approx(0.72)
    now[0] += fx._TTL_S + 1
    assert fx.usd_per("CAD", fetch=lambda: {}) == pytest.approx(0.72)
