"""The data layer: the daily bar store, prices through MarketData, the symbol master."""

from __future__ import annotations

import pytest

import fakes
from tos_bot.data import symbols as symbols_module
from tos_bot.data.bars import FULL_HISTORY, KEEP_SESSIONS, DailyBarStore
from tos_bot.data.market_data import MarketData, NoDataSource
from tos_bot.data.symbols import SymbolMaster
from tos_bot.scanner import schedule
from tos_bot.util import clock


# ---------------------------------------------------------------- daily candles on disk
def test_the_bar_store_downloads_once_then_tops_up(tmp_path):
    store = DailyBarStore(tmp_path)
    full = fakes.daily_bars("AAA")
    through = full.index[-1].date()
    assert store.duration_needed("AAA", through) == FULL_HISTORY

    store.merge("AAA", full.iloc[:-3], through)
    assert store.duration_needed("AAA", through) == f"{(through - full.index[-4].date()).days + 3} D"
    store.merge("AAA", full.tail(5), through)
    assert store.duration_needed("AAA", through) is None
    frame = store.frame("AAA")
    assert len(frame) == min(len(full), KEEP_SESSIONS) and not frame.index.duplicated().any()

    restarted = DailyBarStore(tmp_path)
    assert restarted.last_session("AAA") == through and restarted.frame("AAA").equals(frame)


def test_candles_saved_a_day_early_are_read_back_on_their_session(tmp_path):
    full = fakes.daily_bars("CCC")
    early = full.copy()
    early.index = full.index.tz_localize(None).tz_localize("UTC").tz_convert("America/New_York")  # the old bug
    early.to_pickle(tmp_path / "CCC.pkl")
    store = DailyBarStore(tmp_path)
    assert store.frame("CCC").index.equals(full.index)
    assert store.last_session("CCC") == full.index[-1].date()
    assert store.duration_needed("CCC", full.index[-1].date()) is None      # nothing to re-download


def test_the_bar_store_never_keeps_an_unfinished_session(tmp_path):
    store = DailyBarStore(tmp_path)
    full = fakes.daily_bars("BBB")
    through = full.index[-2].date()
    store.merge("BBB", full, through)
    assert store.last_session("BBB") == through


# ---------------------------------------------------------------- prices
def test_without_the_gateway_there_are_no_prices(tmp_path):
    md = MarketData(DailyBarStore(tmp_path))
    assert not md.attached and md.source_name == "none"
    with pytest.raises(NoDataSource):
        md.intraday(["AAA"])


def test_the_daily_update_only_asks_for_what_is_missing(tmp_path):
    gateway, md = fakes.FakeGateway(), MarketData(DailyBarStore(tmp_path))
    md.attach(gateway)
    through = schedule.last_completed_session(clock.now_ny())
    assert md.update_daily(["AAA", "BBB"], through) == 2
    assert md.update_daily(["AAA", "BBB"], through) == 0
    assert [(s, d) for s, _, d in gateway.requests] == [("AAA", FULL_HISTORY), ("BBB", FULL_HISTORY)]
    assert md.daily_frame("AAA").index[-1].date() == through


def test_intraday_candles_are_cached_briefly(tmp_path):
    gateway, md = fakes.FakeGateway(), MarketData(DailyBarStore(tmp_path))
    md.attach(gateway)
    first = md.intraday(["AAA", "BBB"])
    second = md.intraday(["AAA"])
    assert second["AAA"] is first["AAA"] and len(gateway.requests) == 2


def test_without_a_data_subscription_quotes_come_from_candles(tmp_path):
    gateway, md = fakes.FakeGateway(delayed=True), MarketData(DailyBarStore(tmp_path))
    md.attach(gateway)
    q = md.quote("AAA")
    assert q.bid < q.last < q.ask and md.delayed
    assert [bar for _, bar, _ in gateway.requests] == ["1 min"]
    md.quote("AAA")
    assert len(gateway.requests) == 1                                     # cached


# ---------------------------------------------------------------- what IBKR knows about each symbol
_DETAILS = {
    "AAA": {"con_id": 1, "exchange": "NASDAQ", "stock_type": "COMMON", "industry": "Technology", "category": "Semiconductors"},
    "BBB": {"con_id": 2, "exchange": "NYSE", "stock_type": "COMMON", "industry": "Technology", "category": "Software"},
    "CCC": {"con_id": 3, "exchange": "NYSE", "stock_type": "COMMON", "industry": "Technology", "category": "Semiconductors"},
    "FND": {"con_id": 4, "exchange": "ARCA", "stock_type": "ETF", "industry": "Funds", "category": "Equity Fund"},
    "ZZZ": None,
}


def test_the_symbol_master_remembers_contracts_and_missing_symbols(tmp_path):
    master = SymbolMaster(tmp_path / "symbols.json")
    assert master.unknown(["AAA", "ZZZ"]) == ["AAA", "ZZZ"]
    master.record(_DETAILS)
    assert master.unknown(["AAA", "ZZZ", "NEW"]) == ["NEW"]
    assert master.tradable(["AAA", "FND", "ZZZ", "NEW"]) == ["AAA"]
    assert master.sector("AAA") == "Technology" and master.sector("NEW") == ""
    assert master.peers("AAA", ["AAA", "BBB", "CCC", "FND"], limit=5) == ["CCC", "BBB"]   # same category first

    restarted = SymbolMaster(tmp_path / "symbols.json")
    assert restarted.get("AAA").con_id == 1 and not restarted.get("ZZZ").found


def test_symbols_ibkr_doesnt_know_are_asked_about_again_after_a_month(tmp_path, monkeypatch):
    master = SymbolMaster(tmp_path / "symbols.json")
    master.record({"ZZZ": None})
    later = symbols_module.time.time() + 31 * 86400
    monkeypatch.setattr(symbols_module.time, "time", lambda: later)
    assert master.unknown(["ZZZ"]) == ["ZZZ"]


def test_the_quick_refresh_merges_the_latest_candles_into_the_cache(tmp_path):
    from tos_bot.data.market_data import INTRADAY_DURATION, REFRESH_DURATION

    first_symbol, second_symbol = fakes.SYMBOLS[:2]
    gateway, md = fakes.FakeGateway(), MarketData(DailyBarStore(tmp_path))
    md.attach(gateway)
    first = md.intraday([first_symbol])[first_symbol]
    got = md.refresh_intraday([first_symbol, second_symbol])
    assert [r for r in gateway.requests if r[0] == second_symbol] == [(second_symbol, "5 mins", INTRADAY_DURATION)]
    assert (first_symbol, "5 mins", REFRESH_DURATION) in gateway.requests       # cached: only the last half hour
    assert got[first_symbol].index.is_unique and len(got[first_symbol]) == len(first)
    assert set(got) == {first_symbol, second_symbol}


def test_the_research_store_keeps_years_of_candles_for_the_stocks_the_replay_runs_on(tmp_path):
    deep = DailyBarStore(tmp_path / "deep", keep_sessions=3 * 253 + 10, full_history="3 Y")
    gateway, md = fakes.FakeGateway(), MarketData(DailyBarStore(tmp_path / "bars"), deep=deep)
    md.attach(gateway)
    through = schedule.last_completed_session(clock.now_ny())
    md.update_daily(["AAA", "CCC"], through)                                   # the live store: a year of each
    seen = []
    assert md.deepen_daily(["AAA", "BBB"], through, progress=lambda done, total: seen.append((done, total))) == 2
    assert seen == [(0, 2), (2, 2)]                                             # the stage shows from the first second
    assert [d for s, _, d in gateway.requests if s in ("AAA", "BBB")][-2:] == ["3 Y", "3 Y"]
    assert len(md.deep_frame("AAA")) == 756 > len(md.daily_frame("AAA"))
    assert md.deepen_daily(["AAA", "BBB"], through) == 0                        # current: nothing to ask for

    week_ago = through
    for _ in range(5):
        week_ago = clock.prev_trading_day(week_ago)
    deep.merge("CCC", fakes.daily_bars("CCC", 756, through=week_ago), week_ago)     # a long history gone a week stale
    joined = md.deep_frame("CCC")                                               # ...is carried forward in memory
    assert joined.index[-1].date() == through and joined.index.is_unique and len(joined) == 761
    asked = len(gateway.requests)
    assert md.deepen_daily(["CCC"], through) == 0 and len(gateway.requests) == asked   # ...and topped up for nothing
    assert deep.last_session("CCC") == through

    plain = MarketData(DailyBarStore(tmp_path / "bars"))                        # no research store: the live year
    assert plain.deepen_daily(["AAA"], through) == 0 and len(plain.deep_frame("AAA")) == len(md.daily_frame("AAA"))


# ---------------------------------------------------------------- the latest price, for the dashboard
def test_the_latest_price_is_the_newer_of_the_quote_and_the_candles_and_says_when_its_from(tmp_path):
    import datetime as dt

    from tos_bot.data.market_data import quote_from_price

    gateway, md = fakes.FakeGateway(delayed=True), MarketData(DailyBarStore(tmp_path))
    md.attach(gateway)
    assert md.last_seen("AAA") is None                                          # nothing fetched: nothing to show
    q = md.quote("AAA")
    frame = gateway.history_many({"AAA": ("1 min", "1800 S")})["AAA"]
    price, at, age = md.last_seen("AAA")
    assert price == q.last and at == frame.index[-1].to_pydatetime() and 0 <= age < 5   # the candle's time, not now
    assert len(gateway.requests) == 2                                           # reading it asks the broker nothing

    old = dt.datetime(2020, 1, 2, 15, 0, tzinfo=dt.timezone.utc)
    md._quotes["AAA"] = (md._quotes["AAA"][0], quote_from_price("AAA", 1.0, ts=old))
    md.intraday(["AAA"])
    assert md.last_seen("AAA")[0] == pytest.approx(float(md._intraday["AAA"][1]["close"].iloc[-1]))  # the newer one


def test_refresh_fetches_every_price_in_one_batch_and_keeps_to_the_limit(tmp_path):
    gateway, md = fakes.FakeGateway(["AAA", "BBB", "CCC"], delayed=True), MarketData(DailyBarStore(tmp_path))
    md.attach(gateway)
    assert md.refresh_prices([]) == 0 and gateway.requests == []
    assert md.refresh_prices(["AAA", "BBB", "AAA", "NOPE"]) == 2               # a symbol with no candles is left out
    assert sorted(s for s, _, _ in gateway.requests) == ["AAA", "BBB", "NOPE"]
    assert {bar for _, bar, _ in gateway.requests} == {"1 min"}
    assert md.last_seen("BBB") is not None and md.last_seen("NOPE") is None
    gateway.requests.clear()
    assert md.refresh_prices(["AAA", "BBB", "CCC"], limit=2) == 2 and len(gateway.requests) == 2
