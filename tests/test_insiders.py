"""How unusual a wave of insider buying or selling is (made-up company and insiders)."""

from __future__ import annotations

import datetime as dt

from tos_bot.signals.form4 import BUY, SELL, InsiderTrade
from tos_bot.signals.insiders import insider_signals

TODAY = dt.date(2026, 9, 15)


def _trade(code=BUY, owner=1, role="ceo_cfo", title="Chief Executive Officer", shares=100_000, price=12.0,
           after=None, days_ago=3, planned=False, offering=False, symbol="EXH"):
    after = 2 * shares if after is None else after
    return InsiderTrade(accession=f"acc-{owner}-{days_ago}-{code}", line=1, symbol=symbol, issuer_cik=101,
                        issuer_name="Example Holdings Inc", owner_cik=owner, owner_name=f"Insider {owner}", role=role,
                        title=title, code=code, trade_date=TODAY - dt.timedelta(days=days_ago), shares=shares,
                        price=price, shares_after=after, planned=planned, direct=True, offering=offering)


def _only(signals, direction):
    [signal] = [s for s in signals if s.direction == direction]
    return signal


def test_a_chief_executive_doubling_a_large_stake_is_unusual():
    buying = _only(insider_signals("EXH", [_trade()], TODAY), "buying")
    assert buying.unusual and buying.score >= 0.75
    assert (buying.insiders, buying.value, buying.top_role, buying.avg_price) == (1, 1_200_000, "ceo_cfo", 12.0)
    assert buying.reasons == ["The Chief Executive Officer bought $1.2M in the open market",
                              "At least doubled a holding",
                              "No other open-market buying by its insiders in the previous year"]


def test_a_new_holding_is_not_taken_for_a_doubled_one():
    buying = _only(insider_signals("EXH", [_trade(after=100_000)], TODAY), "buying")
    assert "Started a new holding, or added to one held another way" in buying.reasons
    assert buying.score < _only(insider_signals("EXH", [_trade()], TODAY), "buying").score


def test_several_insiders_buying_together_count_for_more_than_one():
    one = _only(insider_signals("EXH", [_trade(role="director", title="", shares=16_000, after=96_000)], TODAY),
                "buying")
    two = _only(insider_signals("EXH", [_trade(owner=1, role="director", title="", shares=8_000, after=48_000),
                                        _trade(owner=2, role="director", title="", shares=8_000, after=48_000,
                                               price=12.4, days_ago=9)], TODAY), "buying")
    assert two.score > one.score and two.insiders == 2
    assert two.reasons[0] == "2 insiders bought $195K in the open market within 7 days, led by the director"


def test_insiders_all_paying_one_price_on_one_day_look_like_a_placement():
    deal = [_trade(owner=n, role="director", title="", shares=500_000, price=1.5, after=600_000) for n in (1, 2, 3, 4)]
    buying = _only(insider_signals("EXH", deal, TODAY), "buying")
    assert not buying.unusual
    assert any("more likely a private placement or offering" in r for r in buying.reasons)


def test_plan_trades_offerings_and_token_buys_are_not_unusual():
    assert insider_signals("EXH", [_trade(planned=True)], TODAY) == []
    assert insider_signals("EXH", [_trade(offering=True)], TODAY) == []
    token = _only(insider_signals("EXH", [_trade(shares=1_000, price=10.0)], TODAY), "buying")
    assert not token.unusual


def test_insiders_who_buy_every_few_months_are_nothing_new():
    habit = [_trade(days_ago=3, shares=20_000, after=400_000), _trade(days_ago=120, shares=20_000, after=380_000)]
    usual = _only(insider_signals("EXH", habit, TODAY), "buying")
    fresh = _only(insider_signals("EXH", habit[:1], TODAY), "buying")
    assert usual.score < fresh.score
    assert not any("previous year" in r for r in usual.reasons)


def test_rarity_is_not_claimed_before_the_history_is_read():
    unknown = _only(insider_signals("EXH", [_trade()], TODAY, history_known=False), "buying")
    known = _only(insider_signals("EXH", [_trade()], TODAY), "buying")
    assert not any("previous year" in r for r in unknown.reasons)
    assert unknown.score < known.score


def test_selling_has_to_clear_a_higher_bar():
    small = [_trade(SELL, role="director", title="", shares=25_000, price=12.0, after=225_000)]
    assert not _only(insider_signals("EXH", small, TODAY), "selling").unusual
    big = [_trade(SELL, owner=1, shares=600_000, price=12.0, after=400_000),
           _trade(SELL, owner=2, role="officer", title="VP Operations", shares=300_000, price=12.0, after=0, days_ago=5)]
    selling = _only(insider_signals("EXH", big, TODAY), "selling")
    assert selling.unusual and "Sold an entire holding" in selling.reasons


def test_other_stocks_and_old_trades_are_ignored():
    assert insider_signals("EXH", [_trade(symbol="OTHR"), _trade(days_ago=45)], TODAY) == []
