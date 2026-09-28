"""The two models Finnhub's data feeds: the earnings calendar (post-earnings drift, earnings ahead of a
swing trade) and the market model with the news (moves on news keep going, moves without it get taken
back). Made-up companies only."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from test_engine import engine, gateway, port  # noqa: F401 - pytest fixtures
from test_quant_wiring import DAY, NY, _ctx, _daily_frame, _gap_day
from autotradebot.core.enums import Side, StrategyKind, Timeframe
from autotradebot.core.models import Play
from autotradebot.quant.market_model import abnormal_move
from autotradebot.scanner.noise import NoiseSettings, event_flags
from autotradebot.signals import service as service_module
from autotradebot.signals.book import SignalBook
from autotradebot.engine import engine as engine_module
from autotradebot.signals.calendar import (EarningsCalendarFile, EarningsEvent, next_report, report_before_open,
                                      sessions_until, surprise)
from autotradebot.signals.finnhub import CALENDAR_URL, FinnhubNews
from autotradebot.strategies import REGISTRY
from autotradebot.util import clock

PREV = clock.prev_trading_day(DAY)                     # DAY is a Thursday
NEXT = clock.next_trading_day(DAY)


def _event(symbol="ERN", day=DAY, hour="bmo", est=1.0, act=None):
    return EarningsEvent(symbol, day.isoformat(), hour, est, act).as_dict()


# ---------------------------------------------------------------- the calendar
def test_the_calendar_is_read_in_one_request_and_kept_by_stock(tmp_path):
    payload = {"earningsCalendar": [
        {"symbol": "ERN.B", "date": DAY.isoformat(), "hour": "BMO", "epsEstimate": 1.2, "epsActual": 1.5,
         "revenueEstimate": 1e9, "revenueActual": 1.1e9, "quarter": 3, "year": 2026},
        {"symbol": "ERQ", "date": NEXT.isoformat(), "hour": "amc", "epsEstimate": None, "epsActual": None},
        {"symbol": "", "date": DAY.isoformat()}]}
    asked = []

    class Session:
        def get(self, url, params=None, headers=None, timeout=None):
            asked.append((url, params, headers))
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)

    events = FinnhubNews("demo-key", session=Session()).earnings_calendar(PREV, NEXT)
    assert asked == [(CALENDAR_URL, {"from": PREV.isoformat(), "to": NEXT.isoformat()}, {"X-Finnhub-Token": "demo-key"})]
    assert [(e.symbol, e.hour) for e in events] == [("ERN B", "bmo"), ("ERQ", "amc")]
    assert surprise(events[0].as_dict()) == 0.25 and surprise(events[1].as_dict()) is None
    assert FinnhubNews("").earnings_calendar(PREV, NEXT) == []

    kept = EarningsCalendarFile(tmp_path / "calendar.json")
    assert kept.merge(events, DAY) == 2
    kept.save()
    again = EarningsCalendarFile(tmp_path / "calendar.json")
    assert len(again) == 2 and again.by_symbol()["ERN B"][0]["surprise"] == 0.25 and again.fetched_at


def test_which_report_came_before_the_open_and_which_is_still_to_come():
    before = pd.Timestamp(f"{DAY} 09:40", tz=NY).to_pydatetime()
    assert report_before_open([_event(hour="bmo")], DAY)["date"] == DAY.isoformat()
    assert report_before_open([_event(day=PREV, hour="amc")], DAY)["date"] == PREV.isoformat()
    assert report_before_open([_event(day=PREV, hour="", act=1.1)], DAY) is not None      # hour not given, numbers out
    for missing in ([_event(hour="amc")], [_event(day=PREV, hour="bmo")], [_event(day=PREV, hour="")], None):
        assert report_before_open(missing, DAY) is None
    assert next_report([_event(hour="bmo")], before) is None                             # already out
    tonight = next_report([_event(hour="bmo"), _event(day=DAY, hour="amc")], before)
    assert tonight["hour"] == "amc" and sessions_until(tonight, before) == 0
    assert sessions_until(_event(day=clock.next_trading_day(NEXT)), before) == 2


# ---------------------------------------------------------------- the market model
def _market(n=80, seed=7):
    rng = np.random.default_rng(seed)
    market = 0.01 * rng.standard_normal(n)
    stock = 1.5 * market + 0.004 * rng.standard_normal(n)
    return market, stock


def test_the_market_model_separates_a_stock_s_own_move_from_the_market_s():
    market, stock = _market()
    market[-1], stock[-1] = 0.01, 0.015 + 0.06                  # the market explains 1.5%, the rest is its own
    m = _daily_frame(100 * np.cumprod(1 + market))
    s = _daily_frame(50 * np.cumprod(1 + stock))
    move = abnormal_move(s["close"], m["close"])
    assert move["beta"] == pytest.approx(1.5, abs=0.15) and move["z"] > 10
    assert move["expected_pct"] == pytest.approx(1.5, abs=0.2) and move["day"] == PREV.isoformat()
    assert abnormal_move(m["close"] * 2, m["close"]) is None                # moves exactly with the market: no own moves to size
    assert abnormal_move(s["close"], m["close"].iloc[:-1]) is None           # the market's close for that day is missing
    assert abnormal_move(s["close"].iloc[-20:], m["close"]) is None         # too few sessions to fit on


# ---------------------------------------------------------------- news and earnings as noise checks
def _moving_ctx(news_rows, checked_minutes_ago=5, now="11:00", earnings=None, jump=0.06):
    market, stock = _market()
    market[-1], stock[-1] = 0.0, jump
    daily = _daily_frame(50 * np.cumprod(1 + stock), through=DAY)
    ctx = _ctx(None, daily, now=now)
    ctx.benchmark = _daily_frame(100 * np.cumprod(1 + market), through=DAY)["close"]
    at = pd.Timestamp(f"{DAY} {now}", tz=NY)
    ctx.news = None if news_rows is None else {"checked_at": (at - pd.Timedelta(minutes=checked_minutes_ago)).tz_convert("UTC").to_pydatetime(),
                                                "stories": news_rows}
    ctx.earnings = earnings
    return ctx


def _play(side, tf=Timeframe.INTRADAY):
    return Play(symbol="QNT", side=side, strategy="x", kind=StrategyKind.TECHNICAL, timeframe=tf,
                entry=50.0, stop=49.0 if side is Side.LONG else 51.0, targets=[52.0 if side is Side.LONG else 48.0])


def test_big_moves_on_news_aren_t_faded_and_big_moves_without_news_aren_t_chased():
    s = NoiseSettings()
    story = [{"at": f"{DAY}T12:30:00+00:00", "kind": "news", "headline": "QNT wins a contract", "source": "Reuters"}]
    old = [{"at": f"{clock.prev_trading_day(PREV)}T12:30:00+00:00", "kind": "news", "headline": "old", "source": "x"}]
    on_news, quiet = _moving_ctx(story), _moving_ctx([])
    assert on_news.news_since_move() == 1 and quiet.news_since_move() == 0 and _moving_ctx(old).news_since_move() == 0
    assert event_flags(_play(Side.SHORT), on_news, "reversal", s) == ["news_driven_move"]
    assert event_flags(_play(Side.LONG), on_news, "momentum", s) == []
    assert event_flags(_play(Side.LONG), quiet, "momentum", s) == ["move_without_news"]
    assert event_flags(_play(Side.SHORT), quiet, "reversal", s) == []
    for unknown in (_moving_ctx(None), _moving_ctx([], checked_minutes_ago=120)):     # not followed, or read too long ago
        assert event_flags(_play(Side.LONG), unknown, "momentum", s) == []
    small = _moving_ctx([], jump=0.001)
    assert event_flags(_play(Side.LONG), small, "momentum", s) == []
    reported = _moving_ctx([], earnings=[_event("QNT", hour="bmo")])                  # an earnings report counts as news
    assert reported.news_since_move() == 1 and event_flags(_play(Side.LONG), reported, "momentum", s) == []
    assert event_flags(_play(Side.LONG), on_news, "value", s) == []


def test_a_swing_trade_with_earnings_due_within_its_hold_is_flagged():
    s = NoiseSettings()
    soon = _moving_ctx(None, earnings=[_event("QNT", day=NEXT, hour="amc")], jump=0.001)
    far = _moving_ctx(None, earnings=[_event("QNT", day=DAY + dt.timedelta(days=30), hour="amc")], jump=0.001)
    assert event_flags(_play(Side.LONG, Timeframe.SWING), soon, "momentum", s) == ["earnings_ahead"]
    assert event_flags(_play(Side.LONG, Timeframe.INTRADAY), soon, "momentum", s) == []    # closed before the bell
    assert event_flags(_play(Side.LONG, Timeframe.SWING), far, "momentum", s) == []


# ---------------------------------------------------------------- post-earnings drift with the calendar
def test_post_earnings_drift_takes_a_report_from_the_calendar_unless_the_surprise_disagrees():
    daily, bars, _ = _gap_day(1.02, 1.025, sd_overnight=0.005)
    drift = REGISTRY["earnings_drift"]()

    def ctx(event):
        c = _ctx(bars, daily, now="09:45")
        c.earnings = [event]
        return c
    [play] = drift.generate(ctx(_event("QNT", hour="bmo", est=1.0, act=1.2)))
    assert play.side is Side.LONG and play.evidence["earnings_source"] == "Finnhub calendar"
    assert play.evidence["eps_surprise_pct"] == 20.0
    [unknown] = drift.generate(ctx(_event("QNT", hour="bmo", est=1.0, act=None)))           # numbers not out yet
    assert "eps_surprise_pct" not in unknown.evidence
    assert drift.generate(ctx(_event("QNT", hour="bmo", est=1.0, act=0.8))) == []           # gapped up on a miss
    assert drift.generate(ctx(_event("QNT", hour="amc", est=1.0, act=1.2))) == []           # tonight's, not this morning's


# ---------------------------------------------------------------- the service and the engine
def test_the_service_reads_the_calendar_and_keeps_each_stock_s_stories(tmp_path, monkeypatch):
    from test_signal_service import FakeSec, _service

    class Feed:
        def __init__(self, key):
            self.configured = bool(key)

        def earnings_calendar(self, since, until):
            return [EarningsEvent("SVCC", DAY.isoformat(), "amc", 0.5)]
    monkeypatch.setattr(service_module, "FinnhubNews", Feed)
    book = SignalBook()
    service = _service(FakeSec({}), tmp_path, book)
    service.poll_calendar()                                      # no key: nothing read
    assert book.earnings_for("SVCC") == []
    service._finnhub_key = lambda: "demo-key"
    service.poll_calendar()
    assert book.earnings_for("SVCC")[0]["hour"] == "amc" and service.report["calendar"]["kept"] == 1
    assert service.symbol_state("SVCC")["earnings"][0]["date"] == DAY.isoformat()
    reloaded = SignalBook()
    _service(FakeSec({}), tmp_path, reloaded)                   # the next start begins with what was read
    assert reloaded.earnings_for("SVCC")

    now = dt.datetime(2026, 9, 10, 15, tzinfo=dt.timezone.utc)
    book.set_stories({"SVCC": [{"at": now.isoformat(), "kind": "news", "headline": "h", "source": "s"}]}, now)
    assert book.news_reading("SVCC")["stories"][0]["headline"] == "h" and book.news_reading("OTHER") is None


def test_a_swing_position_held_into_a_report_is_warned_about_once(engine, monkeypatch):
    heard = []
    monkeypatch.setattr(engine_module, "BUS", SimpleNamespace(publish=lambda topic, **p: heard.append((topic, p))))
    monkeypatch.setattr(engine.settings.config.signals, "enabled", True)
    swing = Play(symbol="ERNS", side=Side.LONG, strategy="week52_breakout", kind=StrategyKind.TECHNICAL,
                 timeframe=Timeframe.SWING, entry=20.0, stop=19.0, targets=[23.0], suggested_qty=5)
    engine.repo.record_play(swing)
    engine.repo.open_trade(swing, 20.0, 5, "paper")
    today = clock.now_ny().date()
    engine.signal_book.set_earnings({"ERNS": [_event("ERNS", day=clock.next_trading_day(today), hour="bmo")]})
    engine._warn_earnings_ahead()
    engine._warn_earnings_ahead()
    assert [t for t, _ in heard] == ["position.earnings_ahead"] and "gap the price" in heard[0][1]["note"]
    engine.signal_book.set_earnings({"ERNS": [_event("ERNS", day=today + dt.timedelta(days=20), hour="bmo")]})
    heard.clear()
    engine._warn_earnings_ahead()
    assert heard == []
