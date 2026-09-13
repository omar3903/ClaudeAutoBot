"""The parallel paths - batched bars, paced requests, the scan's stages and the
exit checks - are faster and still give the same answers."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from tos_bot.core.models import Quote
from tos_bot.data.market_data import MarketDataService, SyntheticProvider
from tos_bot.execution.exit_manager import ExitManager
from tos_bot.scanner.filters import TradeFilters
from tos_bot.util.ratelimit import RateLimiter

SYMS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "XOM", "JPM"]
SILENT = SimpleNamespace(publish=lambda *a, **k: None)


class _BatchFeed:
    """A feed that can batch, recording how it was asked."""

    name = "batch"
    min_request_gap = 0.0
    quotes_are_synthetic = True

    def __init__(self, have):
        self._synth = SyntheticProvider(seed=4)
        self.have = set(have)
        self.batches, self.singles = [], []

    def history(self, symbol, interval, lookback_days, extended_hours):
        self.singles.append(symbol)
        if symbol not in self.have:
            raise RuntimeError(f"no data for {symbol}")
        return self._synth.history(symbol, interval, lookback_days, extended_hours)

    def history_many(self, symbols, interval, lookback_days, extended_hours):
        self.batches.append(list(symbols))
        return {s: self._synth.history(s, interval, lookback_days, extended_hours)
                for s in symbols if s in self.have}

    def quote(self, symbol):
        raise AssertionError("not expected")


class _PlainFeed:
    name = "plain"
    min_request_gap = 0.0

    def __init__(self):
        self._synth = SyntheticProvider(seed=6)
        self.singles = []

    def history(self, symbol, interval, lookback_days, extended_hours):
        self.singles.append(symbol)
        return self._synth.history(symbol, interval, lookback_days, extended_hours)

    def quote(self, symbol):
        return self._synth.quote(symbol)


# ---------------------------------------------------------------- market data
def test_bars_for_many_symbols_come_in_one_batch_and_misses_are_remembered():
    feed = _BatchFeed(have=SYMS[:3])
    md = MarketDataService(providers=[feed], cache=False)
    got = md.get_price_histories(SYMS[:3] + ["ZZZZ"], "1d", 120)
    assert sorted(got) == sorted(SYMS[:3]) and all(len(df) for df in got.values())
    assert feed.batches == [SYMS[:3] + ["ZZZZ"]] and feed.singles == []
    md.get_price_histories(["ZZZZ"], "1d", 120)
    assert len(feed.batches) == 1                   # a dead ticker isn't asked for again straight away


def test_what_the_batch_misses_comes_from_the_next_feed():
    top, backup = _BatchFeed(have=["AAPL"]), _PlainFeed()
    md = MarketDataService(providers=[top, backup], cache=False)
    got = md.get_price_histories(["AAPL", "MSFT"], "1d", 120)
    assert sorted(got) == ["AAPL", "MSFT"] and backup.singles == ["MSFT"]


def test_rate_limiter_paces_starts_without_holding_its_lock_while_sleeping():
    lim = RateLimiter(0.05)
    t0 = time.monotonic()
    for _ in range(4):
        lim.wait()
    assert time.monotonic() - t0 >= 0.14            # starts 50 ms apart

    slow = RateLimiter(0.3)
    slow.wait()
    sleeper = threading.Thread(target=slow.wait)
    sleeper.start()
    time.sleep(0.05)                                # it's now waiting its turn...
    assert slow._lock.acquire(timeout=0.05)         # ...without blocking anyone else
    slow._lock.release()
    sleeper.join()
    assert RateLimiter(0).wait() == 0.0


# ---------------------------------------------------------------- scanner
def test_scan_reads_quotes_off_the_bars_and_applies_the_filters(monkeypatch):
    from tos_bot.config import get_settings
    from tos_bot.scanner.scanner import Scanner
    from tos_bot.strategies.registry import build_strategies
    from tos_bot.util import clock

    monkeypatch.setattr(clock, "is_market_open", lambda ts=None: False)
    settings = get_settings()
    md = MarketDataService(providers=[SyntheticProvider(seed=21)], cache=False)
    asked = []
    monkeypatch.setattr(md, "get_quote", lambda s: asked.append(s))
    scanner = Scanner(settings, md, None, build_strategies(settings))
    scanner._universe = list(SYMS)
    scanner.filters = TradeFilters.build(sides=["LONG"], timeframes=["SWING"])

    r = scanner.run_cycle()
    assert asked == []                              # no extra request per symbol
    assert r.scanned == len(SYMS) and {"prices", "strategies", "rank"} <= set(r.timings)
    assert "intraday_bars" not in r.timings         # no intraday setup can run
    assert all(p.side.value == "LONG" and p.timeframe.value == "SWING" for p in r.plays)


def test_scanner_strategy_swap_is_one_step():
    from tos_bot.scanner.scanner import Scanner

    tech = SimpleNamespace(kind=SimpleNamespace(value="TECHNICAL"))
    fund = SimpleNamespace(kind=SimpleNamespace(value="FUNDAMENTAL"))
    s = SimpleNamespace()
    Scanner.set_strategies(s, [tech, fund])
    assert s._sets == ([tech], [fund]) and s.strategies == [tech, fund]


# ---------------------------------------------------------------- exits
CFG = SimpleNamespace(enabled=True, breakeven_at_r=0, breakeven_buffer_bps=0, trail_start_r=0,
                      trail_lock_ratio=0, flatten_intraday_before_close_min=0, max_swing_hold_days=0)


class _Repo:
    def __init__(self, trades):
        self.t = {x["id"]: x for x in trades}

    def open_trades(self):
        return [dict(x) for x in self.t.values() if x["status"] == "OPEN"]

    def get_trade(self, tid):
        return dict(self.t[tid]) if tid in self.t else None

    def update_trade_risk(self, tid, **kw):
        self.t[tid].update({k: v for k, v in kw.items() if v is not None})

    def note_overdue(self, tid):
        pass


def test_exit_checks_fetch_every_positions_quote_at_once():
    trades = [dict(id=f"t{i}", symbol=s, side="LONG", timeframe="SWING", status="OPEN", broker="paper",
                   entry_price=100.0, quantity=10, stop_price=90.0, target_price=110.0,
                   initial_stop_price=90.0, initial_target_price=110.0, hwm_price=100.0,
                   managed_exit=True, mae=0.0, mfe=0.0, entry_time="2026-09-03T09:40:00")
              for i, s in enumerate(SYMS[:4])]
    asked = []

    def quote(sym):
        asked.append(sym)
        time.sleep(0.25)
        return Quote(symbol=sym, bid=100.0, ask=100.0, last=100.0)

    closed = []
    ex = SimpleNamespace(close_trade=lambda tid, reason="manual": closed.append(tid) or {"ok": True, "trade": {}})
    em = ExitManager(_Repo(trades), ex, quote_fn=quote, cfg=CFG, bus=SILENT, venue="paper")
    t0 = time.monotonic()
    em.run_once()
    assert sorted(asked) == sorted(SYMS[:4]) and closed == []
    assert time.monotonic() - t0 < 0.75             # one after another would take a second
