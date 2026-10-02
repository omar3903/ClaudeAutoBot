"""The books' models at work: the noise checks and readings on plays, the stop floor, half-Kelly
sizing, the evidence weights, the replay's costs and held-out sessions, the plays not taken,
the statistical setups, earnings dates and the market's regime."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from test_replay import EXACT, FLAT, QUIET, _daily, _LongAtBar, _session
from autotradebot.core.enums import Side, StrategyKind, Timeframe
from autotradebot.core.models import Account, Play
from autotradebot.data.market_data import quote_from_price
from autotradebot.engine.market_regime import MarketRegime
from autotradebot.quant import readings
from autotradebot.research.replay import (ReplaySettings, SimTrade, earnings_signals, held_out_from, learned_skips,
                                     noise_report, replay_intraday, shadow_trade, strategy_records)
from autotradebot.research.weights import evidence_multiplier
from autotradebot.risk.position_sizing import size_play
from autotradebot.scanner.evaluator import evaluate, with_today
from autotradebot.scanner.noise import NoiseSettings, context_flags
from autotradebot.signals.earnings import earnings_times
from autotradebot.strategies import REGISTRY
from autotradebot.strategies.base import Strategy, StrategyContext
from autotradebot.util import clock

NY = "America/New_York"
DAY = dt.date(2026, 9, 10)


def _ar1(phi, n, seed, sd=0.002):
    rng = np.random.default_rng(seed)
    y = np.zeros(n)
    for i in range(1, n):
        y[i] = phi * y[i - 1] + rng.normal(0, sd)
    return y


def _bars(closes, last_day=DAY, start="09:30"):
    """5-minute candles for ``closes``, filling whole sessions back from ``last_day``."""
    closes = np.asarray(closes, float)
    sessions = sorted(clock.last_n_sessions(last_day, -(-len(closes) // 78)))
    idx = [pd.Timestamp(f"{d} {start}", tz=NY) + pd.Timedelta(minutes=5 * i) for d in sessions for i in range(78)]
    idx = idx[-len(closes):] if len(closes) % 78 == 0 else idx[:len(closes)]
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame({"open": opens, "high": np.maximum(opens, closes) * 1.0005, "low": np.minimum(opens, closes) * 0.9995,
                         "close": closes, "volume": np.full(len(closes), 1e5)}, index=pd.DatetimeIndex(idx))


def _daily_frame(closes, opens=None, through=None):
    closes = np.asarray(closes, float)
    through = through or clock.prev_trading_day(DAY)
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz=NY) for d in sorted(clock.last_n_sessions(through, len(closes)))])
    opens = closes if opens is None else np.asarray(opens, float)
    return pd.DataFrame({"open": opens, "high": np.maximum(opens, closes) * 1.005, "low": np.minimum(opens, closes) * 0.995,
                         "close": closes, "volume": np.full(len(closes), 3e6)}, index=idx)


def _ctx(intraday, daily, now="11:00", market=None, signals=None):
    source = intraday if intraday is not None else daily
    price = float(source["close"].iloc[-1])
    return StrategyContext(symbol="QNT", intraday=intraday, daily=with_today(daily, intraday),
                           quote=quote_from_price("QNT", price), now=pd.Timestamp(f"{DAY} {now}", tz=NY).to_pydatetime(),
                           market=market or {}, signals=signals)


def _play(tf=Timeframe.INTRADAY, price=100.0):
    return Play(symbol="QNT", side=Side.LONG, strategy="x", kind=StrategyKind.TECHNICAL, timeframe=tf,
                entry=price, stop=price - 1, targets=[price + 2])


REVERTING = 100 * np.exp(_ar1(0.2, 234, seed=1))
TRENDING = 100 * np.exp(np.cumsum(_ar1(0.7, 234, seed=2)))


# ---------------------------------------------------------------- noise checks and readings
def test_a_setup_that_fights_the_price_s_character_is_flagged():
    settings, daily = NoiseSettings(), _daily_frame(np.linspace(90, 100, 80))
    snapping = _ctx(_bars(REVERTING), daily)
    assert snapping.price_character(True)["character"] == "mean reverting"
    assert "not_trending" in context_flags(_play(), snapping, "momentum", settings)
    assert "not_mean_reverting" not in context_flags(_play(), snapping, "reversal", settings)
    trending = _ctx(_bars(TRENDING), daily)
    assert trending.price_character(True)["character"] == "trending"
    assert "not_mean_reverting" in context_flags(_play(), trending, "reversal", settings)
    assert "not_trending" not in context_flags(_play(), trending, "momentum", settings)


def test_momentum_setups_are_flagged_while_the_market_is_turbulent():
    daily, swing = _daily_frame(np.linspace(90, 100, 80)), _play(Timeframe.SWING)
    stormy = _ctx(None, daily, market={"p_turbulent": 0.82, "regime": "turbulent"})
    assert "turbulent_market" in context_flags(swing, stormy, "momentum", NoiseSettings())
    assert "turbulent_market" not in context_flags(swing, stormy, "reversal", NoiseSettings())
    calm = _ctx(None, daily, market={"p_turbulent": 0.2, "regime": "calm"})
    assert "turbulent_market" not in context_flags(swing, calm, "momentum", NoiseSettings())


class _Always:
    key, style, weight, timeframe = "always", "reversal", 1.0, Timeframe.INTRADAY

    def generate(self, ctx):
        return [Play(symbol=ctx.symbol, side=Side.LONG, strategy=self.key, kind=StrategyKind.TECHNICAL,
                     timeframe=Timeframe.INTRADAY, entry=ctx.price, stop=ctx.price * 0.99, targets=[ctx.price * 1.02],
                     confidence=0.6, probability=0.55)]


def test_plays_carry_the_readings_and_the_evidence_weight_scales_their_rank():
    bars, daily = _bars(REVERTING), _daily_frame(100 * np.exp(np.cumsum(_ar1(0.0, 120, seed=3, sd=0.01))))
    kw = dict(run_id="r", equity=0.0, params={}, market={"p_turbulent": 0.3, "regime": "calm"})
    [weighted] = evaluate("QNT", [_Always()], daily, bars, evidence_weights={"always": 1.4}, **kw)
    [plain] = evaluate("QNT", [_Always()], daily, bars, **kw)
    ev = weighted.evidence
    assert ev["price_character"]["bars"] == 234 and ev["market_regime"]["regime"] == "calm"
    assert ev["vol_forecast"]["model"] in ("garch", "riskmetrics") and ev["evidence_weight"] == 1.4
    assert weighted.score > plain.score and "evidence_weight" not in plain.evidence
    # a reversal setup is expected to take about the price's half-life - here well under a bar
    assert weighted.expected_hold_typical == 10.0 and ev["hold_from_half_life"]


class _TightSwing(Strategy):
    key, kind, timeframe, title, thesis = "tight_swing", StrategyKind.TECHNICAL, Timeframe.SWING, "t", "t"

    def generate(self, ctx):
        p = self._mk_play(ctx, Side.LONG, ctx.price, ctx.price * 0.999, [ctx.price * 1.08], 0.6, "r", "d", {})
        return [p] if p else []


def test_the_stop_widens_when_tomorrow_s_volatility_is_forecast_to_rise():
    daily = _daily_frame(100 * np.exp(np.cumsum(np.full(120, 0.001))))
    stops = {}
    for label, ratio in (("rising", 1.5), ("falling", 0.8)):
        ctx = _ctx(None, daily)
        ctx._memo["vol_forecast"] = {"vol": 0.05, "ratio": ratio, "model": "garch"}
        [p] = _TightSwing().generate(ctx)
        stops[label] = (p.entry - p.stop) / p.entry
    assert stops["rising"] == pytest.approx(0.07, abs=0.002)                   # 1.4 forecast standard deviations
    assert stops["falling"] < 0.03


def test_the_volatility_forecast_leaves_out_today_s_unfinished_candle():
    daily = _daily_frame(100 * np.exp(np.cumsum(_ar1(0.0, 120, seed=4, sd=0.01))))
    today = _bars(np.full(12, float(daily["close"].iloc[-1]) * 1.3))                 # a wild partial candle
    assert len(readings.completed_daily(with_today(daily, today), DAY)) == len(daily)
    assert readings.vol_forecast("VF", with_today(daily, today), DAY) == readings.vol_forecast("VF", daily, DAY)


def test_half_kelly_can_only_lower_the_risk_per_trade():
    cfg = SimpleNamespace(max_risk_per_trade_pct=1.0, max_open_risk_pct=4.0, max_position_pct_of_equity=100.0, round_lot=1)
    account = Account(account_id="t", equity=100_000.0, cash=100_000.0, buying_power=200_000.0)
    play = Play(symbol="K", side=Side.LONG, strategy="x", kind=StrategyKind.TECHNICAL, timeframe=Timeframe.SWING,
                entry=100.0, stop=90.0, targets=[120.0])
    assert size_play(play, account, cfg).qty == 100
    half = size_play(play, account, cfg, risk_pct=0.5)
    assert half.qty == 50 and "half-Kelly from the strategy's record" in half.caps_hit
    assert size_play(play, account, cfg, risk_pct=3.0).qty == 100


# ---------------------------------------------------------------- the evidence weights
def test_the_evidence_weight_is_shrunk_bounded_and_never_raises_a_record_that_fails_out_of_sample():
    assert evidence_multiplier(None, None).multiplier == 1.0
    few = evidence_multiplier({"trades": 10, "expectancy_r": 0.5}, None)
    many = evidence_multiplier({"trades": 400, "expectancy_r": 0.5}, None)
    assert 1.0 < few.multiplier < many.multiplier == 1.5
    assert evidence_multiplier({"trades": 400, "expectancy_r": -0.5}, None).multiplier == 0.5
    held = evidence_multiplier({"trades": 100, "expectancy_r": 0.3,
                                "out_of_sample": {"trades": 30, "expectancy_r": -0.1}}, None)
    assert held.multiplier == 1.0 and "held-out" in held.note
    assert evidence_multiplier({"trades": 100, "expectancy_r": 0.3}, {"trades": 20, "expectancy_r": -0.2}).multiplier == 1.0
    # each real trade counts twice: (0 + 2 x 0.4 x 25) / (50 + 2 x 25 + 50)
    assert evidence_multiplier({"trades": 50, "expectancy_r": 0.0},
                               {"trades": 25, "expectancy_r": 0.4}).multiplier == pytest.approx(1.267, abs=0.001)


# ---------------------------------------------------------------- the replay
def test_every_fill_pays_commission_and_market_fills_pay_slippage_too():
    costly = ReplaySettings(slippage_bps=0.0, commission_bps=10.0, breakeven_at_r=0.0, trail_start_r=0.0)
    session = _session(FLAT + [(100.0, 100.1, 99.9, 100.0), (100.2, 102.1, 100.1, 101.9)])
    [t] = replay_intraday([_LongAtBar()], "RPL", session, _daily(), costly, QUIET)
    assert t.exit_reason == "target" and t.entry == pytest.approx(100.1) and t.exit == pytest.approx(101.898)
    assert t.r == pytest.approx(1.798 / 1.1, abs=0.001) and t.mfe_r == pytest.approx(1.9 / 1.1, abs=0.001)


def _sim(r, day, noise=(), strategy="s"):
    return SimTrade(strategy=strategy, symbol="X", side="LONG", timeframe="INTRADAY", entered_at=f"{day}T10:00:00-04:00",
                    exited_at=f"{day}T11:00:00-04:00", entry=1.0, exit=1.0, r=r, exit_reason="x", noise=list(noise))


def test_records_and_noise_verdicts_are_also_given_for_the_held_out_sessions():
    split = {"INTRADAY": "2026-09-01", "SWING": None}
    record = strategy_records([_sim(1.0, "2026-08-20")] * 20 + [_sim(-0.5, "2026-09-03")] * 12, split=split)["s"]
    assert record["trades"] == 32 and record["expectancy_r"] > 0
    assert record["out_of_sample"] == {**record["out_of_sample"], "trades": 12, "expectancy_r": -0.5}

    clean = [_sim(0.5, "2026-08-20")] * 20 + [_sim(0.5, "2026-09-03")] * 20
    helps = [_sim(-1.0, "2026-08-20", ["not_trending"])] * 12 + [_sim(-1.0, "2026-09-03", ["not_trending"])] * 12
    fades = [_sim(-1.0, "2026-08-20", ["turbulent_market"])] * 12 + [_sim(2.0, "2026-09-03", ["turbulent_market"])] * 12
    report = noise_report(clean + helps + fades, split)
    assert report["not_trending"]["held_out"]["verdict"].startswith("helps")
    assert report["turbulent_market"]["held_out"]["verdict"].startswith("hurts")
    assert learned_skips(report) == ["not_trending"]                            # it has to hold up on both


def _spread(avg, n, day, noise=(), spread=0.5):
    """``n`` trades on ``day`` averaging ``avg``R, ``spread``R either side of it."""
    return [_sim(avg + (spread if i % 2 else -spread), day, noise) for i in range(n)]


def test_the_noise_report_says_how_many_standard_errors_apart_the_removed_and_kept_trades_are():
    row = noise_report(_spread(-0.5, 10, "2026-08-20", ["not_trending"]) + _spread(0.5, 10, "2026-08-20"))["not_trending"]
    assert row["t"] == -4.24                    # 1R apart over sqrt(2 x 0.5^2 x 10/9 / 10): Welch's t, removed minus kept
    few = noise_report(_spread(-0.5, 4, "2026-08-20", ["not_trending"]) + _spread(0.5, 10, "2026-08-20"))
    assert few["not_trending"]["verdict"] == "too few trades to tell" and few["not_trending"]["t"] is None


def test_a_check_is_learned_only_on_a_gap_that_matters_stands_out_of_the_noise_and_holds_up_held_out():
    split, early, late = {"INTRADAY": "2026-09-01", "SWING": None}, "2026-08-20", "2026-09-03"
    kept = _spread(0.3, 40, early, spread=0.05) + _spread(0.3, 20, late, spread=0.05)

    def removing(avg, spread, held_avg=None):
        flagged = (_spread(avg, 30, early, ["not_trending"], spread)
                   + _spread(avg if held_avg is None else held_avg, 16, late, ["not_trending"], spread))
        report = noise_report(kept + flagged, split)
        return learned_skips(report), report["not_trending"]

    skips, row = removing(0.1, 0.5)                    # 0.2R worse, about 2.7 standard errors, and worse held out
    assert skips == ["not_trending"] and row["t"] <= -2
    skips, row = removing(0.27, 0.05)                  # far out of the noise, but 0.03R is too small a gap to matter
    assert skips == [] and row["verdict"].startswith("helps") and row["t"] <= -2
    skips, row = removing(0.0, 2.0)                    # 0.3R worse, but about one standard error: inside the noise
    assert skips == [] and row["verdict"].startswith("helps") and -2 < row["t"] < 0
    skips, row = removing(-0.3, 0.3, held_avg=0.5)     # clearly worse over every session, better on the held-out ones
    assert skips == [] and row["t"] <= -2 and row["held_out"]["verdict"].startswith("hurts")


def test_the_held_out_sessions_are_the_latest_third():
    assert held_out_from(DAY, 3) == "2026-09-10"
    assert held_out_from(DAY, 60) == sorted(clock.last_n_sessions(DAY, 60))[40].isoformat()
    assert held_out_from(DAY, 1) is None


def test_a_play_not_taken_is_followed_as_if_it_had_been():
    session = _session(FLAT + [(100.0, 100.1, 99.9, 100.0), (100.2, 102.1, 100.1, 101.9)])
    play = Play(symbol="RPL", side=Side.LONG, strategy="s", kind=StrategyKind.TECHNICAL, timeframe=Timeframe.INTRADAY,
                entry=100.0, stop=99.0, targets=[102.0])
    seen = session.index[5] + pd.Timedelta(minutes=2)                           # filled at the next bar's open
    t = shadow_trade(play, session, seen, EXACT)
    assert (t.exit_reason, t.entered_at[11:16], t.r) == ("target", "10:00", 2.0)
    assert shadow_trade(play, session, session.index[-1] + pd.Timedelta(minutes=10), EXACT) is None
    far = Play(symbol="RPL", side=Side.LONG, strategy="s", kind=StrategyKind.TECHNICAL, timeframe=Timeframe.INTRADAY,
               entry=95.0, stop=94.0, targets=[97.0])
    assert shadow_trade(far, session, seen, EXACT) is None                      # the price had already moved away


# ---------------------------------------------------------------- the statistical setups
def _gap_day(open_ratio, price_ratio, sd_overnight=0.0):
    """80 daily candles rising 0.5% a day with 2% swings, then today's first three 5-minute
    candles opening at ``open_ratio`` of yesterday's close."""
    n = 80
    closes = 100 * np.exp(0.005 * np.arange(n) + 0.01 * (-1) ** np.arange(n))
    opens = None
    if sd_overnight:
        opens = np.concatenate([[closes[0]], closes[:-1] * np.exp(sd_overnight * (-1) ** np.arange(1, n))])
    daily = _daily_frame(closes, opens)
    last = float(closes[-1])
    prior = _bars(np.full(156, last), last_day=clock.prev_trading_day(DAY))
    today = _bars([last * open_ratio, last * price_ratio, last * price_ratio])
    return daily, pd.concat([prior, today]), last


def test_gap_reversion_buys_a_gap_below_yesterday_s_range_while_the_trend_is_up():
    daily, bars, close = _gap_day(0.965, 0.966)
    prior_low = float(daily["low"].iloc[-1])
    [play] = REGISTRY["gap_reversion"]().generate(_ctx(bars, daily, now="09:45"))
    assert play.side is Side.LONG and play.targets[0] == pytest.approx(prior_low, rel=1e-4)
    assert play.evidence["gap_sigmas"] > 1.0 and play.stop < play.entry
    assert REGISTRY["gap_reversion"]().generate(_ctx(bars, daily, now="10:20")) == []        # past the first half hour


def test_post_earnings_drift_needs_an_earnings_filing_from_before_the_open():
    daily, bars, close = _gap_day(1.02, 1.025, sd_overnight=0.005)
    before = SimpleNamespace(filings=[{"items": "2.02,9.01", "published_at": "2026-09-10T11:05:00+00:00"}])
    [play] = REGISTRY["earnings_drift"]().generate(_ctx(bars, daily, now="09:45", signals=before))
    assert play.side is Side.LONG and play.evidence["gap_sigmas"] > 3
    after = SimpleNamespace(filings=[{"items": "2.02", "published_at": "2026-09-10T14:00:00+00:00"}])
    other = SimpleNamespace(filings=[{"items": "5.02", "published_at": "2026-09-10T11:05:00+00:00"}])
    for signals in (None, after, other):
        assert REGISTRY["earnings_drift"]().generate(_ctx(bars, daily, now="09:45", signals=signals)) == []


def test_the_new_setups_are_on_even_when_config_yaml_predates_them():
    from autotradebot.strategies.registry import _effective

    assert _effective("gap_reversion", {}, {})["enabled"] and _effective("earnings_drift", {}, {})["enabled"]
    assert not _effective("vwap_reclaim", {}, {})["enabled"]


# ---------------------------------------------------------------- earnings dates and the market's regime
def test_earnings_releases_are_the_8k_filings_with_item_2_02():
    doc = {"filings": {"recent": {"form": ["8-K", "10-Q", "8-K", "4"], "items": ["2.02,9.01", "", "5.02", ""],
                                  "acceptanceDateTime": ["2026-07-30T16:05:12.000Z", "2026-07-30T16:10:00.000Z",
                                                         "2026-08-02T12:00:00.000Z", "2026-08-03T12:00:00.000Z"]}}}
    assert earnings_times(doc) == ["2026-07-30T16:05:12+00:00"]
    assert earnings_signals(["2026-09-10T11:05:00+00:00"], DAY).filings[0]["items"] == "2.02"
    assert earnings_signals(["2026-09-10T11:05:00+00:00"], dt.date(2026, 9, 11)) is None


def test_the_market_regime_is_fitted_on_spy_once_a_session_and_replayed_from_earlier_days(tmp_path):
    rng = np.random.default_rng(5)
    days = pd.bdate_range(end=pd.Timestamp("2026-09-09"), periods=700, tz=NY)
    vol = np.where((np.arange(700) // 120) % 2 == 0, 0.006, 0.02)                # calm and turbulent stretches
    closes = 400 * np.exp(np.cumsum(rng.normal(0.0003, vol)))
    frame = pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1e7}, index=days)
    asked = []

    class Source:
        def history_many(self, requests, con_ids=None, end=None):
            asked.append(dict(requests))
            return {"SPY": frame}

    regime = MarketRegime(tmp_path / "spy.pkl")
    reading = regime.refresh(Source(), through=dt.date(2026, 9, 9))
    assert reading["regime"] == "turbulent" and reading["p_turbulent"] > 0.5 and reading["for_session"] == "2026-09-10"
    assert regime.refresh(Source(), through=dt.date(2026, 9, 9)) == reading and len(asked) == 1
    assert regime.context() == {"p_turbulent": reading["p_turbulent"], "regime": "turbulent"}

    history = MarketRegime(tmp_path / "spy.pkl").history(days[500].date())
    assert not regime.look_ahead
    assert history[days[650].date()] > 0.5 > history[days[530].date()]
