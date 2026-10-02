"""The scans on a synthetic IB Gateway - the full scan's ranking, hot list and
swing setups, the intraday cycle's buffer decisions - and the pieces under
them: heat, today's candle, the listings directory and SEC financials."""

from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pandas as pd
import pytest

import fakes
from autotradebot.config import get_settings
from autotradebot.core.enums import Side, StrategyKind, Timeframe
from autotradebot.core.models import Play
from autotradebot.data.bars import DailyBarStore
from autotradebot.data.listings import UsListings, parse_directory
from autotradebot.data.market_data import MarketData
from autotradebot.data.sec_edgar import SecEdgarFundamentals, annual_series, financials_from_facts, sec_ticker
from autotradebot.data.sectors import SECTORS, sector_from_ibkr
from autotradebot.data.symbols import SymbolMaster
from autotradebot.scanner import schedule
from autotradebot.scanner.evaluator import evaluate, median_volume, with_today
from autotradebot.scanner.heat import daily_metrics, intraday_metrics, liquid, rank_by_daily_heat
from autotradebot.scanner.scanner import BENCHMARK, Scanner
from autotradebot.scanner.schedule import ScanSettings
from autotradebot.strategies.registry import build_strategies
from autotradebot.util import clock


@pytest.fixture
def scanner(tmp_path):
    settings = get_settings()
    md = MarketData(DailyBarStore(tmp_path / "bars"))
    md.attach(fakes.FakeGateway(fakes.SYMBOLS + [BENCHMARK]))
    return Scanner(settings, md, SymbolMaster(tmp_path / "symbols.json"), fakes.FakeListings(fakes.SYMBOLS),
                   fakes.NoFundamentals(), tmp_path / "watchlists", build_strategies(settings))


# ---------------------------------------------------------------- the scans
def test_the_full_scan_ranks_everything_and_builds_the_days_lists(scanner):
    result = scanner.run_full(ScanSettings(hot_list_size=6, sector_queue_size=10))
    wl = scanner.watchlist
    assert result.universe_size == 40 and result.scanned == 40 and 6 <= result.liquid <= 40
    assert wl.session == schedule.watchlist_session(clock.now_ny()) and len(wl.hot) == 6
    per_sector = {}
    for c in wl.hot:
        per_sector[c.sector] = per_sector.get(c.sector, 0) + 1
    assert max(per_sector.values()) <= 2 and set(per_sector) <= set(SECTORS)
    assert [c.daily_heat for c in wl.hot] == sorted((c.daily_heat for c in wl.hot), reverse=True)
    assert all(p.timeframe is Timeframe.SWING and p.sector for p in result.plays)
    assert {"listings", "contracts", "daily_candles", "ranking"} <= set(result.timings)


def test_a_second_full_scan_the_same_day_downloads_nothing(scanner):
    scanner.run_full(ScanSettings())
    asked = len(scanner.md.source.requests)
    scanner.run_full(ScanSettings())
    assert len(scanner.md.source.requests) == asked


def test_a_cycle_scans_the_hot_list_and_the_next_buffer_names(scanner, monkeypatch):
    monkeypatch.setattr(clock, "is_market_open", lambda *a, **k: True)
    scanner.run_full(ScanSettings(hot_list_size=6, sector_queue_size=10))
    wl = scanner.watchlist
    per_sector = get_settings().config.scanner.buffer_picks_per_sector
    picked = {c.symbol for q in wl.queues.values() for c in q[:per_sector]}

    hot_before = wl.hot_symbols()
    result = scanner.run_cycle()
    assert set(result.symbols) == set(hot_before) | picked
    assert {d.symbol for d in result.decisions} == picked
    assert sum(wl.searched.values()) == len(picked) and all(c.heat is not None for c in wl.hot)

    fast = scanner.run_cycle(fast=True)
    assert set(fast.symbols) == set(wl.hot_symbols()) and not fast.decisions


def test_the_wide_scan_looks_at_every_liquid_stock(scanner, monkeypatch):
    monkeypatch.setattr(clock, "is_market_open", lambda *a, **k: True)
    scanner.run_full(ScanSettings(hot_list_size=6, sector_queue_size=10))
    wl = scanner.watchlist
    asked = len(scanner.md.source.requests)
    result = scanner.run_wide()
    assert result.kind == "wide" and set(result.symbols) == set(wl.ranked) and result.scanned == len(wl.ranked)
    assert len(scanner.md.source.requests) - asked >= len(wl.ranked)          # one request per stock
    assert not result.errors and all(c.heat is not None for c in wl.hot)
    assert all(d.action in ("adopted", "kept") for d in result.decisions)
    assert all(p.symbol in wl.ranked for p in result.plays)
    assert scanner.run_wide(stocks=5).scanned == 5                              # capped to the hottest five


def test_yesterdays_movers_get_hot_list_slots_from_the_full_scan(scanner):
    from autotradebot.scanner.heat import daily_metrics

    scanner.settings.config.scanner.movers_min_rvol = 0.0
    result = scanner.run_full(ScanSettings(hot_list_size=6, sector_queue_size=10, yesterday_movers=3))
    wl = scanner.watchlist
    ranked = sorted((daily_metrics(s, scanner.md.daily_frame(s)) for s in wl.ranked), key=lambda m: -m.move_atr)
    movers = [m.symbol for m in ranked[:3] if scanner.symbols.sector(m.symbol)]
    assert movers and all(s in wl.hot_symbols() for s in movers) and len(wl.hot) == 6
    assert all(c.why.startswith("moved") for c in wl.hot if c.symbol in movers)
    assert all(d.action == "adopted" and d.note.startswith("moved") for d in result.decisions)


def test_todays_movers_get_hot_list_slots_from_the_wide_scan(scanner, monkeypatch):
    monkeypatch.setattr(clock, "is_market_open", lambda *a, **k: True)
    scanner.settings.config.scanner.movers_min_rvol = 0.0
    scanner.run_full(ScanSettings(hot_list_size=6, sector_queue_size=10, yesterday_movers=0))
    wl = scanner.watchlist
    result = scanner.run_wide(movers=3)
    today = {s: intraday_metrics(s, scanner.md.intraday([s])[s], scanner.md.daily_frame(s)) for s in wl.ranked}
    movers = [s for s, m in sorted(today.items(), key=lambda kv: -abs(kv[1].change_pct)) if m and scanner.symbols.sector(s)][:3]
    assert movers and all(s in wl.hot_symbols() for s in movers)
    assert all("today" in c.why for c in wl.hot if c.symbol in movers)                  # marked, adopted or already hot
    assert all(d.note for d in result.decisions if d.symbol in movers and d.action == "adopted")


class _EveryStock:
    """One day play on every stock it is shown, at its latest price."""
    key, style, weight = "vwap_reclaim", "reversal", 1.0
    kind, timeframe = StrategyKind.TECHNICAL, Timeframe.INTRADAY

    def generate(self, ctx):
        return [Play(symbol=ctx.symbol, side=Side.LONG, strategy=self.key, kind=StrategyKind.TECHNICAL,
                     timeframe=Timeframe.INTRADAY, entry=ctx.price, stop=ctx.price * 0.99,
                     targets=[ctx.price * 1.03])]


def test_the_wide_scan_hands_the_thread_over_between_its_chunks_and_leaves_newer_reads_to_the_check(scanner,
                                                                                                    monkeypatch):
    monkeypatch.setattr(clock, "is_market_open", lambda *a, **k: True)
    scanner.run_full(ScanSettings(hot_list_size=6, sector_queue_size=10))
    ranked = scanner.watchlist.ranked
    monkeypatch.setattr(scanner, "WIDE_CHUNK", 10)
    scanner.set_strategies([_EveryStock()])
    asked = len(scanner.md.source.requests)
    pauses = []

    def between():
        pauses.append({r[0] for r in scanner.md.source.requests[asked:] if r[0] != BENCHMARK})
        # the first pause's check reads the first stock, which the sweep has read, and the last, which it hasn't yet
        return [ranked[0], ranked[-1]] if len(pauses) == 1 else []

    result = scanner.run_wide(between=between)
    chunks = math.ceil(len(ranked) / 10)
    assert chunks >= 3 and pauses == [set(ranked[:10 * n]) for n in range(1, chunks)]     # between chunks only
    assert result.scanned == len(ranked) and set(result.symbols) == set(ranked) - {ranked[0]}
    assert {p.symbol for p in result.plays} == set(ranked) - {ranked[0]}           # the check's play on it stands
    assert "between_chunks" in result.timings


def test_the_gap_check_adopts_the_gappers_into_the_hot_list(scanner, monkeypatch):
    from autotradebot.analysis.levels import find_levels
    from autotradebot.scanner.heat import GapperMetrics, rank_gappers

    scanner.run_full(ScanSettings(hot_list_size=6, sector_queue_size=10))
    wl = scanner.watchlist
    queued = [c.symbol for q in wl.queues.values() for c in q]
    gapper, small, hot_gapper = queued[0], queued[1], wl.hot_symbols()[0]
    monkeypatch.setitem(fakes.GAPS, gapper, 7.5)                  # +7.5% pre-market on heavy volume
    monkeypatch.setitem(fakes.GAPS, small, 0.4)                   # a wiggle, not a gap
    monkeypatch.setitem(fakes.GAPS, hot_gapper, -3.0)             # a hot-list name gapping down keeps its slot
    hot_before = wl.hot_symbols()

    result = scanner.run_gappers()
    assert result.kind == "gappers" and set(result.symbols) >= set(hot_before) | set(queued)
    assert [g["symbol"] for g in result.gappers] == [gapper, hot_gapper] and result.gappers[0]["heat"] == 1.0
    assert gapper in wl.hot_symbols() and hot_gapper in wl.hot_symbols() and small not in wl.hot_symbols()
    assert gapper not in [c.symbol for q in wl.queues.values() for c in q]
    [d] = result.decisions
    assert (d.symbol, d.action) == (gapper, "adopted") and d.replaced in hot_before and "gapped +7.5%" in d.note
    assert next(c for c in wl.hot if c.symbol == gapper).gap_pct == pytest.approx(7.5, abs=0.1)
    assert next(c for c in wl.hot if c.symbol == hot_gapper).gap_pct == pytest.approx(-3.0, abs=0.1)
    seen = scanner.premarket[gapper]
    assert seen["gap_pct"] == pytest.approx(7.5, abs=0.1) and seen["high"] >= seen["last"] >= seen["low"]
    assert result.summary()["gappers"][0]["symbol"] == gapper

    # the pre-market high and low are levels for the day's setups
    daily = fakes.daily_bars(gapper)
    price = float(daily["close"].iloc[-1])
    extra = [(seen["high"], "pre-market high"), (seen["low"], "pre-market low")]
    with_pre = find_levels(daily, price, None, max_levels=50, extra_levels=extra)
    assert {"pre-market high", "pre-market low"} <= {s for lvl in with_pre.levels for s in lvl.sources}
    assert not {"pre-market high", "pre-market low"} & {s for lvl in find_levels(daily, price, None, max_levels=50).levels
                                                          for s in lvl.sources}

    # thin or small moves never count as gappers
    quiet = GapperMetrics("Q", 10.0, 10.5, 5.0, 1.0, 100.0, 1050.0, 10.6, 10.0)
    assert rank_gappers([quiet], 2.0, 50_000) == [] and rank_gappers([], 2.0, 50_000) == []


def test_the_watch_tier_is_the_hot_list_then_kept_then_the_next_picks(scanner, monkeypatch):
    from autotradebot.scanner.filters import TradeFilters
    from autotradebot.scanner.watchlist import Candidate, DayWatchlist

    day = dt.date(2026, 9, 24)
    assert clock.is_trading_day(day)
    at = lambda hhmm: dt.datetime.combine(day, dt.time.fromisoformat(hhmm), clock.NY)   # noqa: E731
    monkeypatch.setattr(scanner.settings.config.scanner, "buffer_picks_per_sector", 2)
    industry = {"T01": "Technology", "T02": "Energy", "T03": "Technology", "T04": "Technology", "T05": "Technology",
                "T06": "Financial", "T07": "Technology", "T08": "Technology", "T09": "Technology", "T10": "Energy"}
    scanner.symbols.record({s: {"con_id": i + 1, "industry": ind} for i, (s, ind) in enumerate(industry.items())})
    wl = DayWatchlist(
        session=day, bars_through=day - dt.timedelta(days=1), built_at="", universe=40, liquid=40,
        # the latest intraday heat, or the daily heat until a cycle has seen the stock
        hot=[Candidate("T01", "Technology", 1.0, heat=0.5), Candidate("T02", "Energy", 2.0),
             Candidate("T03", "Technology", 0.5, heat=3.0)],
        kept={"Technology": [Candidate("T04", "Technology", 5.0, heat=0.2),
                             Candidate("T05", "Technology", 0.1, heat=0.9)],
              "Financials": [Candidate("T06", "Financials", 0.3)]},
        # two picks a sector - T09 waits for a later cycle; T02 is hot already
        queues={"Technology": [Candidate("T07", "Technology", 0.4), Candidate("T08", "Technology", 0.6),
                               Candidate("T09", "Technology", 9.0)],
                "Energy": [Candidate("T10", "Energy", 0.8), Candidate("T02", "Energy", 0.7)]})
    scanner.watchlist = wl
    before, asked = wl.state(), len(scanner.md.source.requests)

    tier = ["T03", "T02", "T01", "T05", "T06", "T04", "T10", "T08", "T07"]
    assert scanner.watch_symbols(50, at("10:00")) == tier
    assert scanner.watch_symbols(4, at("10:00")) == tier[:4]
    assert scanner.watch_symbols(50, at("08:00")) == tier                      # the pre-market streams it too
    # a read and nothing more: no request, no change, nothing saved
    assert wl.state() == before and len(scanner.md.source.requests) == asked
    assert not (scanner.watchlist_dir / f"watchlist_{day.isoformat()}.json").exists()

    assert scanner.watch_symbols(0, at("10:00")) == []
    assert scanner.watch_symbols(50, at("17:00")) == []                        # after hours
    assert scanner.watch_symbols(50, dt.datetime.combine(day + dt.timedelta(days=1), dt.time(10), clock.NY)) == []
    scanner.filters = TradeFilters(sectors=("Technology", "Energy"))           # Financials filtered out
    assert scanner.watch_symbols(50, at("10:00")) == [s for s in tier if s != "T06"]
    scanner.filters = TradeFilters()
    wl.session = day - dt.timedelta(days=1)                                    # a watchlist left from yesterday
    assert scanner.watch_symbols(50, at("10:00")) == []


def test_todays_candle_is_built_from_the_intraday_bars():
    daily = fakes.daily_bars("AAA").iloc[:-1]
    intraday = fakes.intraday_bars("AAA")
    today = intraday[intraday.index.date == intraday.index[-1].date()]
    full = with_today(daily, intraday)
    assert len(full) == len(daily) + 1 and full.index[-1].date() == intraday.index[-1].date()
    assert full["high"].iloc[-1] == today["high"].max() and full["volume"].iloc[-1] == today["volume"].sum()
    assert with_today(full, intraday) is full and with_today(daily, None) is daily


def test_a_play_carries_the_stocks_median_volume_over_its_last_20_completed_sessions():
    intraday = fakes.intraday_bars("AAA")
    final = schedule.last_completed_session(clock.now_ny())               # the newest session whose candle is final
    days = pd.bdate_range(end=pd.Timestamp(final), periods=30, tz="America/New_York")
    volume = [10_000.0] * 10 + [120_000.0] * 9 + [100_000.0] * 10 + [5_000_000.0]    # older 10, then the last 20
    daily = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": volume}, index=days)
    day, swing = evaluate("AAA", [_DayAndSwing()], daily, intraday, run_id="r", equity=0.0, params={})
    assert day.evidence["adv_shares"] == swing.evidence["adv_shares"] == 110_000   # not the 30-session 100,000

    calm = daily.assign(volume=volume[:-1] + [120_000.0])                   # the spike day made an ordinary one
    assert median_volume(calm) == median_volume(daily) == 110_000
    printing = pd.Timestamp(clock.next_trading_day(final), tz="America/New_York")
    partial = pd.concat([daily, daily.iloc[-1:].set_axis([printing]).assign(volume=1.0)])
    assert median_volume(partial) == 110_000                                # a candle still printing never counts
    assert median_volume(daily.iloc[:0]) is None                            # no history: no cap
    assert median_volume(daily.assign(volume=0.0)) is None                  # no volume is missing data
    no_volume, _ = evaluate("AAA", [_DayAndSwing()], daily.assign(volume=0.0), intraday, run_id="r", equity=0.0, params={})
    assert "adv_shares" not in no_volume.evidence


class _DayAndSwing:
    """One day play and one swing play on every stock it is shown."""
    key, style, weight = "both", "momentum", 1.0

    def generate(self, ctx):
        return [Play(symbol=ctx.symbol, side=Side.LONG, strategy=self.key, kind=StrategyKind.TECHNICAL, timeframe=tf,
                     entry=ctx.price, stop=ctx.price * 0.99, targets=[ctx.price * 1.03])
                for tf in (Timeframe.INTRADAY, Timeframe.SWING)]


def test_a_day_play_carries_the_last_closed_candle_it_was_seen_on():
    daily, intraday = fakes.daily_bars("AAA").iloc[:-1], fakes.intraday_bars("AAA")
    day, swing = evaluate("AAA", [_DayAndSwing()], daily, intraday, run_id="r", equity=0.0, params={})
    assert day.evidence["bar_at"] == intraday.index[-2].isoformat()       # the newest candle is still printing
    assert pd.Timestamp(day.evidence["bar_at"]).tzinfo is not None
    assert "bar_at" not in swing.evidence                                 # swing plays count scans, as before
    no_candles, _ = evaluate("AAA", [_DayAndSwing()], daily, None, run_id="r", equity=0.0, params={})
    assert no_candles.is_day_trade and "bar_at" not in no_candles.evidence   # no candles, nothing to count by


# ---------------------------------------------------------------- heat
def _daily(volume_last=1.0, move=0.0, n=60):
    idx = pd.date_range("2026-01-02", periods=n, freq="B", tz="America/New_York")
    close = np.full(n, 50.0)
    close[-1] += move
    volume = np.full(n, 1e6)
    volume[-1] *= volume_last
    return pd.DataFrame({"open": close, "high": close + 0.5, "low": close - 0.5, "close": close,
                         "volume": volume}, index=idx)


def test_daily_heat_puts_unusual_volume_and_big_moves_first():
    quiet, busy = daily_metrics("QUIET", _daily()), daily_metrics("BUSY", _daily(volume_last=4.0, move=2.0))
    assert busy.rvol == pytest.approx(4.0) and busy.move_atr > quiet.move_atr and busy.extreme > quiet.extreme
    ranked = rank_by_daily_heat([quiet, busy])
    assert [m.symbol for m in ranked] == ["BUSY", "QUIET"] and ranked[0].heat > ranked[1].heat
    assert daily_metrics("NEW", _daily(n=20)) is None                   # too little history
    prefilter = {"min_price": 3.0, "max_price": 600.0, "min_dollar_volume": 5e6, "min_atr_pct": 1.0}
    assert liquid(busy, prefilter) and not liquid(busy, {**prefilter, "min_price": 60.0})


def test_intraday_heat_rises_with_volume_and_range():
    daily = fakes.daily_bars("AAA")
    calm = fakes.intraday_bars("AAA")
    wild = calm.copy()
    today = wild.index.date == wild.index[-1].date()
    wild.loc[today, "volume"] *= 4
    wild.loc[today, "high"] *= 1.03
    a, b = intraday_metrics("AAA", calm, daily), intraday_metrics("AAA", wild, daily)
    assert b.rvol == pytest.approx(4 * a.rvol, rel=0.02) and b.range_atr > a.range_atr and b.heat > a.heat


def test_ibkr_classification_maps_onto_the_sectors():
    assert sector_from_ibkr("Consumer, Non-cyclical", "Pharmaceuticals") == "Healthcare"
    assert sector_from_ibkr("Consumer, Non-cyclical", "Food") == "Consumer Staples"
    assert sector_from_ibkr("Financial", "REITS") == "Real Estate"
    assert sector_from_ibkr("Communications", "Internet") == "Communication Services"
    assert sector_from_ibkr(None, None) == ""


# ---------------------------------------------------------------- the listings directory
NASDAQ = ("Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
          "AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N\n"
          "QQQ|Invesco QQQ Trust, Series 1|G|N|N|100|Y|N\n"
          "ZXZZT|NASDAQ TEST STOCK|G|Y|N|100|N|N\n"
          "ABCDW|ABC Acquisition Corp - Warrants|S|N|N|100|N|N\n"
          "File Creation Time: 0914202604:00|||||||\n")
OTHER = ("ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n"
         "BRK.B|Berkshire Hathaway Inc. Class B|N|BRK.B|N|100|N|BRK=B\n"
         "TSM|Taiwan Semiconductor Manufacturing Company Ltd.|N|TSM|N|100|N|TSM\n"
         "JPM$D|JPMorgan Chase & Co. Depositary Shares|N|JPMpD|N|100|N|JPM-D\n"
         "SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY\n"
         "File Creation Time: 0914202604:00|||||||\n")


def test_the_directory_keeps_stocks_and_adrs_only():
    assert [(l.symbol, l.exchange) for l in parse_directory(NASDAQ, OTHER)] == [
        ("AAPL", "Nasdaq"), ("BRK B", "NYSE"), ("TSM", "NYSE")]


def test_listings_are_cached_daily_and_survive_a_failed_download(tmp_path):
    calls = []

    def fetch():
        calls.append(1)
        return NASDAQ, OTHER

    def offline():
        raise OSError("offline")

    today = dt.date(2026, 9, 14)
    assert len(UsListings(tmp_path, fetch).load(today)) == 3
    assert len(UsListings(tmp_path, fetch).load(today)) == 3 and len(calls) == 1
    assert [l.symbol for l in UsListings(tmp_path, offline).load(today + dt.timedelta(days=1))] == ["AAPL", "BRK B", "TSM"]


# ---------------------------------------------------------------- SEC EDGAR financials
FY = dt.date.today().year - 1


def _row(val, end, start=None, form="10-K", filed=None):
    row = {"val": val, "end": end, "form": form, "filed": filed or f"{FY + 1}-02-01"}
    if start:
        row["start"] = start
    return row


def _flow(values):
    return {"units": {"USD": [_row(v, f"{y}-12-31", f"{y}-01-01") for y, v in values.items()]}}


def _balance(value):
    return {"units": {"USD": [_row(value, f"{FY}-12-31")]}}


def _facts():
    return {
        "us-gaap": {
            "Revenues": _flow({FY - 2: 1000.0, FY - 1: 1200.0, FY: 1400.0}),
            "OperatingIncomeLoss": _flow({FY - 1: 200.0, FY: 250.0}),
            "NetIncomeLoss": _flow({FY - 2: 100.0, FY - 1: 150.0, FY: 180.0}),
            "DepreciationDepletionAndAmortization": _flow({FY - 1: 50.0, FY: 60.0}),
            "InterestExpense": _flow({FY: 20.0}),
            "PaymentsToAcquirePropertyPlantAndEquipment": _flow({FY: 70.0}),
            "IncomeTaxExpenseBenefit": _flow({FY: 45.0}),
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": _flow({FY: 225.0}),
            "LongTermDebtNoncurrent": _balance(500.0),
            "LongTermDebtCurrent": _balance(50.0),
            "CashAndCashEquivalentsAtCarryingValue": _balance(300.0),
            "EarningsPerShareDiluted": {"units": {"USD/shares": [_row(1.8, f"{FY}-12-31", f"{FY}-01-01")]}},
        },
        "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
            {"val": 60.0, "accn": "0002"}, {"val": 40.0, "accn": "0002"}, {"val": 90.0, "accn": "0001"}]}}},
    }


def test_financials_are_read_off_the_latest_annual_filings():
    fin = financials_from_facts("XYZ", _facts(), dt.date(FY + 1, 3, 1))
    assert fin.revenue == [1000.0, 1200.0, 1400.0] and fin.net_income[-1] == 180.0
    assert fin.ebit[-1] == 250.0 and fin.ebitda[-1] == 310.0 and math.isnan(fin.ebitda[0])
    assert fin.total_debt == 550.0 and fin.cash_and_st_investments == 300.0
    assert fin.capex[-1] == -70.0 and fin.interest_expense[-1] == 20.0
    assert fin.shares_out == 100.0 and fin.tax_rate == pytest.approx(0.2) and fin.ttm_eps == 1.8


def test_a_restatement_wins_and_quarters_are_ignored():
    gaap = {"Revenues": {"units": {"USD": [
        _row(1000.0, "2025-12-31", "2025-01-01", filed="2026-02-01"),
        _row(1010.0, "2025-12-31", "2025-01-01", filed="2027-02-01"),        # restated a year later
        _row(300.0, "2025-12-31", "2025-10-01", form="10-Q"),
        _row(260.0, "2025-12-31", "2025-10-01"),                            # a quarter inside a 10-K
    ]}}}
    assert annual_series(gaap, ["Revenues"], balance=False) == {dt.date(2025, 12, 31): 1010.0}


def test_stale_or_foreign_filers_give_nothing():
    assert financials_from_facts("OLD", _facts(), dt.date(FY + 3, 1, 1)) is None
    assert financials_from_facts("IFRS", {"ifrs-full": {}}, dt.date(FY + 1, 3, 1)) is None
    assert sec_ticker("BRK B") == "BRK-B"


def test_sec_answers_are_cached_on_disk(tmp_path):
    def fetch(url):
        if url.endswith("company_tickers.json"):
            return {"0": {"ticker": "XYZ", "cik_str": 1234}}
        return {"facts": _facts()}

    fin = SecEdgarFundamentals(tmp_path, fetch_json=fetch).get("XYZ")
    assert fin is not None and fin.shares_out == 100.0
    assert SecEdgarFundamentals(tmp_path, fetch_json=fetch).get("NOPE") is None
    cached = SecEdgarFundamentals(tmp_path, fetch_json=lambda url: pytest.fail("should come from the cache"))
    assert cached.get("XYZ").revenue == fin.revenue and cached.get("NOPE") is None
