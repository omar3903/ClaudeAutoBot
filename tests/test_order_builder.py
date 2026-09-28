from __future__ import annotations

from types import SimpleNamespace

from autotradebot.core.enums import Side, StrategyKind, Timeframe
from autotradebot.core.models import Play
from autotradebot.execution.order_builder import plan_order
from autotradebot.util.clock import Session

CFG = SimpleNamespace(default_order_type="LIMIT", limit_offset_bps=5, time_in_force="DAY",
                      bracket_orders=True, allow_extended_hours=True)


def _play(ext=False, tf=Timeframe.SWING):
    return Play(symbol="AAA", side=Side.LONG, strategy="x", kind=StrategyKind.TECHNICAL,
                timeframe=tf, entry=100.0, stop=98.0, targets=[104.0], extended_hours_ok=ext)


def test_regular_session_limit_with_native_bracket():
    p = plan_order(_play(), Session.REGULAR, CFG)
    assert p["executable"] and p["order_type"] == "LIMIT"
    assert p["bracket_mode"] == "native"
    assert p["limit_price"] > 100.0                      # marketable-limit crosses up for a long


def test_closed_market_blocks():
    p = plan_order(_play(ext=True), Session.CLOSED, CFG)
    assert not p["executable"] and "closed" in p["reason"].lower()


def test_premarket_requires_extended_flag():
    blocked = plan_order(_play(ext=False), Session.PRE, CFG)
    assert not blocked["executable"] and "regular-hours" in blocked["reason"]

    ok = plan_order(_play(ext=True), Session.PRE, CFG)
    assert ok["executable"] and ok["order_type"] == "LIMIT"
    assert ok["order_session"] == "EXTENDED"
    assert ok["bracket_mode"] == "managed"               # no native stop in ext hours
    assert "pre-market" in ok["session_label"]


def test_market_order_type_regular():
    cfg = SimpleNamespace(**{**CFG.__dict__, "default_order_type": "MARKET"})
    p = plan_order(_play(), Session.REGULAR, cfg)
    assert p["order_type"] == "MARKET" and p["limit_price"] is None
    assert "market" in p["session_label"]
