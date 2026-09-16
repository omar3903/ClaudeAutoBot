"""The report on a session's biggest movers: the market ranked by the session's move, why each stock
moved, and what the bot made of it."""

from __future__ import annotations

import datetime as dt

import pandas as pd

from tos_bot.research.movers import (Move, build_movers, explain, read_session, rolling_capture, session_bounds,
                                     top_movers)
from tos_bot.util import clock

DAY = dt.date(2026, 9, 15)
NY = "America/New_York"
PREFILTER = {"min_price": 3.0, "max_price": 600.0, "min_dollar_volume": 5e6, "min_atr_pct": 1.0}
SECTORS = {"GAPR": "Technology", "CLMB": "Technology", "DROP": "Energy", "FLAT": "Technology", "THIN": "Health Care",
           "SPY": ""}


def _frame(open_, high, low, close, volume, *, before_volume=1e6, eve=0.0):
    """Forty quiet sessions at 50, then the session itself. The more ``eve``, the livelier the session
    before - its volume and range - and the higher the morning's ranking puts the stock."""
    days = clock.last_n_sessions(DAY, 41)
    rows = [{"open": 50.0, "high": 50.5, "low": 49.5, "close": 50.0, "volume": before_volume}] * 39
    rows.append({**rows[0], "high": 50.5 + eve, "low": 49.5 - eve, "volume": before_volume * (1 + 5 * eve)})
    rows.append({"open": open_, "high": high, "low": low, "close": close, "volume": volume})
    return pd.DataFrame(rows, index=pd.DatetimeIndex([pd.Timestamp(d) for d in days]).tz_localize(NY))


def _market():
    frames = {
        "GAPR": _frame(56.0, 57.0, 55.5, 56.5, 4e6, eve=0.6),                # gapped and held
        "CLMB": _frame(50.2, 54.8, 50.0, 54.5, 3e6, eve=0.4),                # climbed all session
        "DROP": _frame(49.0, 49.2, 44.5, 45.0, 3e6, eve=0.2),
        "FLAT": _frame(50.0, 50.4, 49.8, 50.1, 1e6),
        "THIN": _frame(52.0, 66.0, 51.0, 65.0, 2e6, before_volume=20_000),  # too thin until the day itself
        "SPY": _frame(50.0, 50.4, 49.9, 50.25, 1e6),
    }
    return read_session(frames.items(), DAY, sector_of=SECTORS.get, prefilter=PREFILTER, sectors_allowed=None)


def _story(at, kind="news", headline="A story", items=""):
    return {"published_at": at.isoformat(), "kind": kind, "headline": headline, "items": items, "source": "ibkr",
            "provider": "BRFG", "url": "", "sentiment": None}


def test_the_market_is_ranked_by_the_sessions_move_with_how_each_stock_moved():
    market = _market()
    gainers, losers = top_movers(market.moves, 3)
    assert [m.symbol for m in gainers] == ["THIN", "GAPR", "CLMB"] and [m.symbol for m in losers] == ["DROP"]
    assert market.market_pct == 0.5 and market.thin == {"THIN"} and market.scanned == 5
    assert [m.symbol for m in market.ranked] == ["GAPR", "CLMB", "DROP", "FLAT"]
    _, opened, _ = session_bounds(DAY)
    gapper, climber = (explain(m, [], {}, opened) for m in gainers[1:])
    assert gapper["before_open"] and gapper["reasons"][0].startswith("gapped +12.0% at the open")
    assert not climber["before_open"] and climber["reasons"][0].startswith("moved +8.6% during the session")
    assert gainers[1].rvol == 3.48 and gainers[1].extreme == "high" and gapper["catalyst"]["kind"] == "none"


def test_why_it_moved_puts_earnings_and_filings_before_analysts_and_plain_news():
    m = Move("NEWS", "Technology", close=55.0, prev_close=50.0, change_pct=10.0, gap_pct=8.0, session_pct=1.85,
             rvol=3.0, move_atr=5.0, atr_pct=2.0, dollar_volume=1e8)
    _, opened, _ = session_bounds(DAY)
    hour = dt.timedelta(hours=1)
    news = _story(opened - 2 * hour, headline="Peers rally")
    exhibits = _story(opened - hour, kind="filing", headline="8-K: financial statements and exhibits", items="9.01")
    analyst = _story(opened + hour, kind="analyst", headline="A broker upgrades NEWS to Buy")
    earnings = _story(opened - hour / 2, kind="filing", headline="8-K: results of operations (earnings)",
                      items="2.02,9.01")
    both = explain(m, [news, exhibits, earnings, analyst], {}, opened)
    assert both["catalyst"] == {"kind": "earnings", "label": "8-K: results of operations (earnings)", "before_open": True}
    assert len(both["stories"]) == 3                                     # the exhibits-only 8-K says nothing
    assert explain(m, [news, analyst], {}, opened)["catalyst"]["kind"] == "analyst"
    assert explain(m, [news], {}, opened)["catalyst"]["kind"] == "news"
    assert explain(m, [_story(opened, headline="NEWS beats estimates")], {}, opened)["catalyst"]["kind"] == "earnings"
    sector = explain(m, [], {"Technology": (6.0, 12)}, opened)
    assert sector["with_sector"] and sector["catalyst"]["kind"] == "sector"
    assert explain(m, [], {"Technology": (1.5, 12)}, opened)["catalyst"]["kind"] == "none"   # it outran its sector
    assert explain(m, None, {}, opened)["catalyst"]["kind"] == "unchecked"


def test_what_the_bot_made_of_each_mover():
    market = _market()
    trades = [{"symbol": "GAPR", "side": "LONG", "strategy": "gap_and_go", "entry_time": "2026-09-15T14:00:00",
               "entry_price": 56.2, "exit_time": "2026-09-15T15:00:00", "exit_price": 57.5, "r_multiple": 1.5,
               "realized_pl": 300.0, "status": "CLOSED"}]
    offered = {"symbol": "CLMB", "side": "LONG", "strategy": "vwap_reclaim", "entry": 51.0, "stop": 50.5,
               "timeframe": "INTRADAY", "status": "PROPOSED"}
    plays = [{**offered, "id": "p1", "created_at": "2026-09-15T15:10:00"},
             {**offered, "id": "p2", "created_at": "2026-09-15T15:30:00"}]            # the same setup again
    saved = {"hot": [{"symbol": "GAPR"}, {"symbol": "DROP"}], "queues": {"Technology": [{"symbol": "FLAT"}]},
             "kept": {}, "decisions": []}
    out = build_movers(market, per_side=3, sector_of=SECTORS.get, news={}, trades=trades, plays=plays,
                       shadows=[{"play_id": "p1", "symbol": "CLMB", "r": 2.1, "filled": True}], saved_watchlist=saved,
                       hot_size=2, queue_size=5, prefilter=PREFILTER)
    bot = {r["symbol"]: r["bot"] for r in out["gainers"] + out["losers"]}
    assert bot["GAPR"]["status"] == "traded" and bot["GAPR"]["r"] == 1.5 and bot["GAPR"]["trades"][0]["with_move"]
    assert bot["GAPR"]["trades"][0]["entry_time"] == "2026-09-15T14:00:00+00:00"
    assert bot["CLMB"]["status"] == "offered" and bot["CLMB"]["r"] == 2.1 and len(bot["CLMB"]["plays"]) == 1
    assert bot["CLMB"]["watched"] == "scanned from the Technology buffer"
    assert bot["DROP"]["status"] == "watched" and bot["DROP"]["detail"] == "on the hot list - no setup triggered"
    assert bot["THIN"]["status"] == "missed" and bot["THIN"]["detail"].startswith("too thin or too quiet")
    s = out["summary"]
    assert (s["movers"], s["traded"], s["offered"], s["watched"], s["missed"], s["thin"]) == (4, 1, 1, 1, 1, 1)
    assert (s["in_watchlist"], s["on_hot_list"], s["before_open"]) == (3, 2, 1)
    assert out["lessons"][0] == ("The morning's watchlist held 3 of the 4 biggest movers (2 on the hot list). "
                                 "The bot traded 1 of them for +1.50R.")

    idle = build_movers(market, per_side=3, sector_of=SECTORS.get, news=None, trades=[], plays=[], shadows=[],
                        saved_watchlist=None, hot_size=2, queue_size=5, prefilter=PREFILTER)
    rows = idle["gainers"] + idle["losers"]
    assert {r["bot"]["status"] for r in rows} == {"offline"} and idle["lessons"][0].startswith("The app wasn't scanning")
    assert next(r for r in rows if r["symbol"] == "GAPR")["bot"]["detail"].endswith("put it on the hot list)")


def test_the_watchlist_is_judged_only_on_sessions_the_app_was_scanning():
    def review(movers, in_watchlist, offline=0):
        return {"movers": {"ok": True, "summary": {"movers": movers, "in_watchlist": in_watchlist, "traded": 1,
                                                   "before_open": 5, "traded_r": 0.5, "offline": offline}}}
    got = rolling_capture([review(20, 5), review(20, 20, offline=20), {"movers": {"ok": False}}, {}, review(20, 7)])
    assert got == {"sessions": 2, "movers": 40, "in_watchlist": 0.3, "traded": 0.05, "before_open": 0.25,
                   "traded_r": 1.0}
    assert rolling_capture([{}]) is None
