"""The trading capital's three meanings: the whole account with margin, cash only, or a set amount - and what
Autopilot's max_gross_exposure_pct is a share of."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from autotradebot.core.models import Account
from autotradebot.engine import capital
from autotradebot.engine.runtime import load_capital_mode

# an account worth 100k with 300k of buying power left (the broker's figure, margin in it); 1 USD per unit
ACC = Account(account_id="T", equity=100_000.0, cash=90_000.0, buying_power=300_000.0, usd_per_base=1.0)


def test_the_whole_account_with_margin_may_use_the_buying_power_but_risk_stays_on_the_accounts_value():
    assert capital.capacity_usd(ACC, None, 40_000.0) == 340_000.0          # what's held plus what's left
    view = capital.sizing_account(ACC, None, 40_000.0, mode=capital.MODE_MARGIN)
    assert view.raw["capital_room"] == 300_000.0 and view.buying_power == 300_000.0
    assert view.equity == 100_000.0                                          # risk per trade: never on margin
    half = capital.sizing_account(ACC, None, 40_000.0, share=0.5, invested_in_kind=40_000.0,
                                  mode=capital.MODE_MARGIN)
    assert half.raw["capital_room"] == pytest.approx(340_000.0 * 0.5 - 40_000.0)
    broke = Account(account_id="T", equity=100_000.0, cash=100_000.0, buying_power=0.0, usd_per_base=1.0)
    assert capital.capacity_usd(broke, None, 0.0) == 100_000.0              # no buying power reported: its value


def test_cash_only_never_holds_more_than_the_account_is_worth():
    assert capital.capacity_usd(ACC, None, 40_000.0, capital.MODE_CASH) == 100_000.0
    view = capital.sizing_account(ACC, None, 40_000.0, mode=capital.MODE_CASH)
    assert view.raw["capital_room"] == 60_000.0 and view.buying_power == 100_000.0 and view.equity == 100_000.0


def test_a_set_amount_is_that_much_of_the_accounts_money_whatever_the_mode():
    for mode in capital.MODES:
        view = capital.sizing_account(ACC, 50_000.0, 10_000.0, mode=mode)
        assert (view.equity, view.raw["capital_room"]) == (50_000.0, 40_000.0)
        assert capital.capacity_usd(ACC, 50_000.0, 10_000.0, mode) == 50_000.0


def test_the_state_says_which_it_is_and_what_the_bot_may_hold():
    margin = capital.state(ACC, "paper", "the paper account", None, 40_000.0)
    assert (margin["mode"], margin["effective"], margin["buying_power"]) == ("margin", 340_000.0, 300_000.0)
    cash = capital.state(ACC, "paper", "the paper account", None, 40_000.0, mode=capital.MODE_CASH)
    assert (cash["mode"], cash["effective"], cash["available"]) == ("cash", 100_000.0, 60_000.0)
    amount = capital.state(ACC, "paper", "the paper account", 50_000.0, 10_000.0, mode=capital.MODE_CASH)
    assert (amount["mode"], amount["effective"]) == ("amount", 50_000.0)


def test_the_mode_is_read_back_from_disk_and_a_bad_one_is_left_out():
    assert load_capital_mode({"paper": "cash", "ibkr-paper": "margin", "live": "yolo", "x": 3}) == {
        "paper": "cash", "ibkr-paper": "margin"}
    assert load_capital_mode(None) == {}
    assert capital.parse_mode(" Cash ") == "cash"
    with pytest.raises(ValueError):
        capital.parse_mode("everything")


def test_the_engine_keeps_the_mode_per_venue_and_sizes_by_it(tmp_path):
    from autotradebot.engine import capital_ops

    class Engine(capital_ops.CapitalOps):
        def __init__(self):
            self._account, self._venue, self.capital, self.capital_mode = ACC, "paper", {}, {}
            self.day_trade_pct, self.size_factor, self.saved = 100.0, 1.0, 0
            risk = SimpleNamespace(max_position_pct_of_equity=12.0)
            self.settings = SimpleNamespace(config=SimpleNamespace(risk=risk))

        def _locked(self): return None
        def _save_runtime(self): self.saved += 1
        def _settings_changed(self): pass
        def _publish(self, *a, **k): pass
        def _held_by_kind(self): return {capital.DAY: 40_000.0, capital.SWING: 0.0}
        def effective_day_pct(self): return 100.0
        def _both_kinds(self): return False
        def working_entries(self): return []

    e = Engine()
    assert e.sizing_account() is ACC and e.exposure_ceiling() == 340_000.0  # the default: the whole account, margin
    out = e.set_capital(None, mode="cash")
    assert out["ok"] and "nothing is borrowed" in out["note"] and e.capital_mode == {"paper": "cash"} and e.saved == 1
    assert e.sizing_account().raw["capital_room"] == 60_000.0 and e.exposure_ceiling() == 100_000.0
    assert e.capital_state()["mode"] == "cash"
    assert not e.set_capital(None, mode="everything")["ok"] and e.capital_mode == {"paper": "cash"}
    back = e.set_capital(None, mode="margin")
    assert back["ok"] and "buying power" in back["note"] and e.sizing_account() is ACC
    assert e.set_capital(None)["ok"] and e.capital_mode == {"paper": "margin"}   # no mode: the one set stays
