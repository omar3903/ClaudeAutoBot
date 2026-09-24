"""The data layer: the daily bar store, prices through MarketData, the symbol master."""

from __future__ import annotations

import pytest

import fakes
from tos_bot.data import symbols as symbols_module
from tos_bot.data.bars import FULL_HISTORY, KEEP_SESSIONS, DailyBarStore
from tos_bot.data.market_data import MarketData, NoDataSource
from tos_bot.data.symbols import SymbolMaster
from tos_bot.research.history import IntradayHistory
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


# ---------------------------------------------------------------- only a stock symbol names a candle file
def _plant_outside(tmp_path):
    """Candles where a name that isn't a symbol would reach from a store in tmp_path / "store",
    and those names: a full path, a step up, a sub-folder, a dot, a drive and a network share."""
    frame = fakes.daily_bars("AAA")
    for where in (tmp_path / "ZZ.pkl", tmp_path / "store" / "ZZ" / "ZZ.pkl", tmp_path / "store" / "ZZ.B.pkl"):
        where.parent.mkdir(parents=True, exist_ok=True)
        frame.to_pickle(where)
    return [str(tmp_path / "ZZ"), "..\\ZZ", "../ZZ", "ZZ/ZZ", "ZZ.B", "C:\\ZZ", "\\\\ZZHOST\\S\\X"]


def test_only_a_stock_symbol_names_a_candle_file(tmp_path):
    store = DailyBarStore(tmp_path / "store")
    assert store._path("AAA") == tmp_path / "store" / "AAA.pkl"
    assert store._path("BBB B") == tmp_path / "store" / "BBB_B.pkl"
    for name in _plant_outside(tmp_path):
        assert store.frame(name) is None, name


def test_a_download_for_a_name_that_isnt_a_symbol_saves_nothing_and_goes_on(tmp_path):
    store = DailyBarStore(tmp_path / "store")
    bars = fakes.daily_bars("AAA")
    through = bars.index[-1].date()
    for name in (str(tmp_path / "ZZ"), "../ZZ", "QQ/ZZ"):
        assert len(store.merge(name, bars, through))                 # no error to stop the download loop
        assert store.frame(name) is None
    assert not [p for p in tmp_path.rglob("*") if p.is_file()]
    store.merge("AAA", bars, through)
    assert (tmp_path / "store" / "AAA.pkl").exists() and store.last_session("AAA") == through


def test_the_replay_candle_store_only_names_files_after_stock_symbols(tmp_path):
    history = IntradayHistory(tmp_path / "store")
    for name in _plant_outside(tmp_path):
        assert history.stored(name) is None, name
    frame = fakes.daily_bars("AAA")
    for name in (str(tmp_path / "YY"), "../YY", "QQ/YY"):
        history._write(name, frame)                                  # no error to stop the download loop
    assert not list(tmp_path.rglob("YY.*"))
    history._write("BBB B", frame)
    assert (tmp_path / "store" / "BBB_B.pkl").exists() and history.stored("BBB B").equals(frame)


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


def test_refresh_shows_extended_hours_prices_but_the_quote_the_exits_read_stays_regular_hours(tmp_path):
    gateway, md = fakes.FakeGateway(["AAA"], delayed=True), MarketData(DailyBarStore(tmp_path))
    md.attach(gateway)
    assert md.refresh_prices(["AAA"]) == 1 and gateway.requests == [("AAA", "1 min", "3600 S")]
    shown = float(fakes.premarket_bars("AAA")["close"].iloc[-1])              # the fake's extended-hours candles
    assert md.last_seen("AAA")[0] == pytest.approx(shown, abs=1e-4)
    regular = float(fakes.intraday_bars("AAA")["close"].iloc[-1])
    assert md.quote("AAA").last == pytest.approx(regular, abs=1e-4)             # the exits and entry checks: regular hours
    assert regular != pytest.approx(shown, abs=1e-4)


def test_on_real_time_data_a_newer_price_to_show_never_reaches_the_quote_either(tmp_path):
    import datetime as dt
    import time

    from tos_bot.data.market_data import quote_from_price

    gateway, md = fakes.StreamingGateway(["AAA"]), MarketData(DailyBarStore(tmp_path))
    gateway.connect()
    md.attach(gateway)
    md.streams.sync(["AAA"], [], 5)
    streamed = gateway.tick("AAA", 10.0)
    late = streamed.ts + dt.timedelta(minutes=5)
    md._shown["AAA"] = (time.monotonic(), quote_from_price("AAA", 11.0, ts=late))
    assert md.last_seen("AAA")[:2] == (11.0, late)                              # shown on the dashboard...
    assert md.quote("AAA").last == 10.0                                         # ...the exits read the stream
    gateway.tick("AAA", 10.0, age_s=3.0)                                        # the stream gone quiet: a snapshot
    regular = float(fakes.intraday_bars("AAA")["close"].iloc[-1])
    assert md.quote("AAA").last == pytest.approx(regular, abs=1e-4) and gateway.snapshots == 1


def test_the_latest_price_is_the_newer_of_a_price_to_show_and_the_quote(tmp_path):
    import datetime as dt
    import time

    from tos_bot.data.market_data import quote_from_price

    gateway, md = fakes.FakeGateway(["AAA"], delayed=True), MarketData(DailyBarStore(tmp_path))
    md.attach(gateway)
    q = md.quote("AAA")                                                         # the fake's session ends at 15:55
    md.refresh_prices(["AAA"])                                                  # ...its extended-hours candles at 09:25
    assert md.last_seen("AAA")[:2] == (q.last, q.ts)                            # the quote's candle is the later one

    md._quotes["AAA"] = (time.monotonic() - 600, q)                             # fetched ten minutes ago...
    md._shown["AAA"] = (time.monotonic(), quote_from_price("AAA", q.last, ts=q.ts))   # ...and the same candle just now
    assert md.last_seen("AAA")[2] < 5                                           # counted as fresh

    late = q.ts + dt.timedelta(minutes=35)
    md._shown["AAA"] = (time.monotonic(), quote_from_price("AAA", 1.5, ts=late))     # a trade after the close
    assert md.last_seen("AAA")[:2] == (1.5, late)
    assert md.quote("AAA").last == q.last                                       # the exits still read regular hours


def test_a_price_to_show_is_asked_for_at_most_every_15_seconds_and_a_broker_error_isnt_raised(tmp_path, monkeypatch):
    gateway, md = fakes.FakeGateway(["AAA"], delayed=True), MarketData(DailyBarStore(tmp_path))
    md.attach(gateway)
    price, at, age = md.price_now("AAA", con_id=7)
    assert price == pytest.approx(float(fakes.premarket_bars("AAA")["close"].iloc[-1]), abs=1e-4)
    assert at == fakes.premarket_bars("AAA").index[-1].to_pydatetime() and len(gateway.requests) == 1
    assert md.price_now("AAA")[0] == price and len(gateway.requests) == 1      # asked within 15 s: not again
    assert md.price_now("NOPE") is None and md.price_now("NOPE") is None
    assert len(gateway.requests) == 2                                           # nothing came back: still asked once

    md._shown_asked["AAA"] -= 16                                                # 16 s on
    assert md.price_now("AAA")[0] == price and len(gateway.requests) == 3      # asked again

    def broken(*args, **kwargs):
        raise RuntimeError("the Gateway went away")

    monkeypatch.setattr(gateway, "history_many", broken)
    md._shown_asked["AAA"] -= 16
    assert md.price_now("AAA")[0] == price                                      # what the app holds, not an error
    assert md.price_now("ZZZ") is None
