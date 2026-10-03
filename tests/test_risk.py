from __future__ import annotations

from types import SimpleNamespace

from autotradebot.core.enums import Side, StrategyKind, Timeframe
from autotradebot.core.models import Account, Play
from autotradebot.risk.pdt_guard import PdtGuard
from autotradebot.risk.position_sizing import size_play

ACC_CFG = SimpleNamespace(
    min_start_equity=2000.0, pdt_equity_threshold=25000.0,
    max_day_trades_under_threshold=3, day_trade_warn_at=2, cash_account=False,
)
RISK_CFG = SimpleNamespace(
    max_risk_per_trade_pct=1.0, max_open_risk_pct=4.0, max_positions=5,
    max_position_pct_of_equity=35.0, default_stop_atr_mult=1.5,
    min_reward_risk=1.5, round_lot=1,
)


def _play(entry=100.0, stop=98.0, tf=Timeframe.INTRADAY):
    return Play(symbol="AAA", side=Side.LONG, strategy="x", kind=StrategyKind.TECHNICAL,
                timeframe=tf, entry=entry, stop=stop, targets=[entry + 4])


def _acct(equity, round_trips=0):
    return Account(account_id="t", equity=equity, cash=equity, buying_power=equity * 2,
                   round_trips=round_trips)


def test_sizing_fixed_fractional():
    # wide stop so the risk budget (not the notional cap) is the binding limit
    p = _play(100, 90)                       # $10 risk/share
    r = size_play(p, _acct(10000), RISK_CFG)  # 1% of 10k = $100 budget -> 10 sh
    assert r.qty == 10
    assert p.dollar_risk == 100.0
    assert r.notional == 1000.0              # 10% of equity, under the 35% cap


def test_the_size_factor_scales_the_risk_and_the_caps_still_hold():
    assert size_play(_play(100, 90), _acct(10000), RISK_CFG, size_factor=2.5).qty == 25   # $250 of risk
    none = size_play(_play(100, 90), _acct(10000), RISK_CFG, size_factor=0.0)
    assert none.qty == 0 and "position size factor 0x" in none.caps_hit
    usual = size_play(_play(100, 90), _acct(10000), RISK_CFG)
    assert usual.qty == 10 and not any("size factor" in c for c in usual.caps_hit)          # 1 is the usual size
    # 5x a $100 budget is $500, past the 4% open-risk ceiling with $300 of it used: the ceiling still holds
    big = size_play(_play(100, 90), _acct(10000), RISK_CFG, open_risk_used=300.0, size_factor=5.0)
    assert big.qty == 10 and "portfolio open-risk ceiling" in big.caps_hit
    # and the notional cap: 5x on a tight stop stops at 35% of equity
    tight = size_play(_play(100, 99.9), _acct(10000), RISK_CFG, size_factor=5.0)
    assert tight.qty * 100 <= 10000 * 0.35 + 1 and "max position % of equity" in tight.caps_hit


def test_sizing_capped_by_notional():
    p = _play(100, 99.9)                     # tiny risk -> huge qty by risk alone
    r = size_play(p, _acct(10000), RISK_CFG)
    assert r.qty * 100 <= 10000 * 0.35 + 1
    assert "max position % of equity" in r.caps_hit


def test_the_sizing_names_the_limit_that_decided_the_share_count_and_what_each_limit_allowed():
    cfg = SimpleNamespace(**vars(RISK_CFG), max_symbol_pct_of_equity=20.0, max_adv_pct=1.0)
    wide = size_play(_play(100, 90), _acct(10000), cfg)                     # $100 of risk at $10 a share: 10
    assert (wide.qty, wide.decided_by) == (10, "risk_budget")
    assert wide.limits["risk_budget"] == {"pct": 1.0, "size_factor": 1.0, "usd": 100.0, "shares": 10}
    assert wide.limits["open_risk"] == {"pct": 4.0, "used": 0.0, "usd": 400.0, "shares": 40}
    assert wide.limits["per_position"] == {"pct": 35.0, "usd": 3500.0, "shares": 35}
    assert wide.limits["per_symbol"] == {"pct": 20.0, "held": 0.0, "usd": 2000.0, "shares": 20}
    assert wide.limits["buying_power"] == {"usd": 20000.0, "shares": 200}
    assert "volume" not in wide.limits and "capital_room" not in wide.limits   # no volume known, no capital limit

    assert size_play(_play(100, 90), _acct(10000), cfg, open_risk_used=350.0).decided_by == "open_risk"   # $50 left
    no_symbol_cap = SimpleNamespace(**vars(RISK_CFG))
    assert size_play(_play(100, 99.5), _acct(10000), no_symbol_cap).decided_by == "per_position"   # 200 by risk, 35
    tight = size_play(_play(100, 99.5), _acct(10000), cfg, symbol_notional=500.0)
    assert (tight.qty, tight.decided_by, tight.limits["per_symbol"]["usd"]) == (15, "per_symbol", 1500.0)
    thin = _play(100, 99.5)
    thin.evidence["adv_shares"] = 1200                                      # 1% of it: 12 shares
    assert (size_play(thin, _acct(10000), cfg).decided_by, thin.suggested_qty) == ("volume", 12)
    short_of_cash = Account(account_id="t", equity=10000, buying_power=900)
    assert size_play(_play(100, 99.5), short_of_cash, cfg).decided_by == "buying_power"
    capped = Account(account_id="t", equity=10000, buying_power=20000, raw={"capital_room": 800.0})
    room = size_play(_play(100, 99.5), capped, cfg)
    assert (room.qty, room.decided_by, room.limits["capital_room"]) == (8, "capital_room", {"usd": 800.0, "shares": 8})


def test_pdt_blocks_below_floor():
    g = PdtGuard(ACC_CFG)
    d = g.assess(_acct(1500), _play())
    assert not d.allowed and "floor" in d.reason


def test_pdt_caps_day_trades_under_25k():
    g = PdtGuard(ACC_CFG)
    ok = g.assess(_acct(5000, round_trips=2), _play(tf=Timeframe.INTRADAY))
    assert ok.allowed and ok.warnings                    # 3rd allowed, but warned
    blocked = g.assess(_acct(5000, round_trips=3), _play(tf=Timeframe.INTRADAY))
    assert not blocked.allowed and "PDT" in blocked.reason


def test_pdt_swing_not_blocked_by_day_trade_count():
    g = PdtGuard(ACC_CFG)
    d = g.assess(_acct(5000, round_trips=3), _play(tf=Timeframe.SWING))
    assert d.allowed


def test_pdt_ignored_above_25k():
    g = PdtGuard(ACC_CFG)
    d = g.assess(_acct(30000, round_trips=9), _play(tf=Timeframe.INTRADAY))
    assert d.allowed


def test_paper_mode_never_blocks():
    g = PdtGuard(ACC_CFG, paper=True)
    # below the $2000 floor AND over the day-trade cap -> still allowed in paper
    d = g.assess(_acct(500, round_trips=9), _play(tf=Timeframe.INTRADAY))
    assert d.allowed
    assert "paper" in d.reason.lower()
    # counter is still surfaced so the operator can see it
    assert d.day_trades_used == 9


def test_the_pdt_guard_counts_only_the_day_trades_of_the_venue_orders_go_to():
    class Repo:
        asked = []

        def count_day_trades(self, lookback_sessions=5, venue=None):
            self.asked.append(venue)
            return {"ibkr-live": 3, "ibkr-paper": 1}.get(venue, 4)

    live = PdtGuard(ACC_CFG, trade_repo=Repo(), venue="ibkr-live")
    blocked = live.assess(_acct(5000), _play(tf=Timeframe.INTRADAY))
    assert not blocked.allowed and blocked.day_trades_used == 3
    # the paper account's trades don't use up the live account's day trades
    ok = PdtGuard(ACC_CFG, trade_repo=Repo(), venue="ibkr-paper").assess(_acct(5000), _play(tf=Timeframe.INTRADAY))
    assert ok.allowed and ok.day_trades_used == 1
    assert Repo.asked == ["ibkr-live", "ibkr-paper"]


def test_one_stock_never_takes_more_than_its_share_of_equity():
    cfg = SimpleNamespace(**{**vars(RISK_CFG), "max_symbol_pct_of_equity": 15.0})
    p = _play(100, 90)                                   # $10 risk/share: 10 shares by risk alone
    r = size_play(p, _acct(10000), cfg, symbol_notional=1_000.0)
    assert r.qty == 5 and "max exposure per stock" in r.caps_hit      # $1,500 cap - $1,000 already held
    assert size_play(_play(100, 90), _acct(10000), cfg, symbol_notional=1_600.0).qty == 0


# --------------------------------------------------------------------------- #
#  A slice of the stock's usual daily volume
# --------------------------------------------------------------------------- #
def _thin(adv, entry=100.0, stop=90.0):
    p = _play(entry, stop)
    p.evidence["adv_shares"] = adv
    return p


def test_a_position_never_exceeds_one_percent_of_the_stocks_usual_daily_volume():
    cfg = SimpleNamespace(**vars(RISK_CFG), max_adv_pct=1.0)
    r = size_play(_thin(500), _acct(10000), cfg)            # 10 shares by risk alone; 1% of 500 is 5
    assert r.qty == 5 and "liquidity: 1% of its usual daily volume" in r.caps_hit
    assert size_play(_thin(5_000), _acct(10000), cfg).qty == 10      # plenty of volume: risk decides, no cap named
    assert "liquidity: 1% of its usual daily volume" not in size_play(_thin(5_000), _acct(10000), cfg).caps_hit
    assert size_play(_thin(90), _acct(10000), cfg).qty == 0          # too thin for one share
    # ...and the cap is named for it, not the risk budget, which would have bought 10
    assert size_play(_thin(90), _acct(10000), cfg).caps_hit == ["liquidity: 1% of its usual daily volume"]


def test_the_risk_budget_is_named_only_when_it_is_what_buys_nothing():
    assert "risk budget too small for one share" in size_play(_play(100, 10), _acct(500), RISK_CFG).caps_hit
    held = size_play(_play(100, 90), _acct(10000), SimpleNamespace(**vars(RISK_CFG), max_symbol_pct_of_equity=15.0),
                     symbol_notional=1_600.0)
    assert held.qty == 0 and held.caps_hit == ["max exposure per stock"]


def test_practice_size_is_named_for_what_it_is_not_the_records_half_kelly():
    practice = "practice size: a quarter of the usual risk until the replay proves the strategy"
    r = size_play(_play(100, 90), _acct(100_000.0), RISK_CFG, risk_pct=0.25, risk_why=practice)
    assert r.qty == 25 and r.caps_hit == [practice]
    assert size_play(_play(100, 90), _acct(100_000.0), RISK_CFG, risk_pct=0.5).caps_hit == [
        "half-Kelly from the strategy's record"]


def test_no_known_volume_means_no_liquidity_cap():
    cfg = SimpleNamespace(**vars(RISK_CFG), max_adv_pct=1.0)
    assert size_play(_thin(500), _acct(10000), cfg).qty == 5
    assert size_play(_play(100, 90), _acct(10000), cfg).qty == 10    # a play saved before the scan wrote it
    assert size_play(_thin(0), _acct(10000), cfg).qty == 10


def test_the_liquidity_cap_can_be_switched_off():
    assert size_play(_thin(500), _acct(10000), SimpleNamespace(**vars(RISK_CFG), max_adv_pct=0.5)).qty == 2
    assert size_play(_thin(500), _acct(10000), SimpleNamespace(**vars(RISK_CFG), max_adv_pct=0.0)).qty == 10
    assert size_play(_thin(500), _acct(10000), RISK_CFG).qty == 10   # no setting = off


# --------------------------------------------------------------------------- #
#  Aziz: a smaller size at Mid-day
# --------------------------------------------------------------------------- #
def test_a_day_trade_sized_at_midday_risks_a_fraction_of_the_usual(monkeypatch):
    import datetime as dt
    from zoneinfo import ZoneInfo

    from autotradebot.risk import position_sizing
    from autotradebot.risk.position_sizing import time_of_day_factor

    ny = ZoneInfo("America/New_York")
    cfg = SimpleNamespace(**RISK_CFG.__dict__, midday_size_pct=60.0)
    midday, open_ = dt.datetime(2026, 9, 15, 13, 0, tzinfo=ny), dt.datetime(2026, 9, 15, 9, 45, tzinfo=ny)
    assert time_of_day_factor(_play(), cfg, now=midday) == 0.6
    assert time_of_day_factor(_play(), cfg, now=open_) == 1.0
    assert time_of_day_factor(_play(tf=Timeframe.SWING), cfg, now=midday) == 1.0      # swing trades are untouched
    assert time_of_day_factor(_play(), RISK_CFG, now=midday) == 1.0                    # no setting = off
    assert time_of_day_factor(_play(), SimpleNamespace(midday_size_pct=100.0), now=midday) == 1.0

    monkeypatch.setattr(position_sizing.clock, "time_of_day", lambda now=None: "MIDDAY")
    usual = size_play(_play(stop=90.0), _acct(100_000.0), RISK_CFG)         # $1,000 of risk at $10 a share
    smaller = size_play(_play(stop=90.0), _acct(100_000.0), cfg)
    assert usual.qty == 100 and smaller.qty == 60 and "Mid-day: a smaller size (Aziz)" in smaller.caps_hit
    monkeypatch.setattr(position_sizing.clock, "time_of_day", lambda now=None: "OPEN")
    assert size_play(_play(stop=90.0), _acct(100_000.0), cfg).qty == 100
