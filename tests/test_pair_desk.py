"""The pair desk on the simulator: both legs go on together, come off together, and a pair is
never left half on."""

from __future__ import annotations

import os
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from autotradebot.brokers.base import OrderRejected
from autotradebot.brokers.paper_adapter import PaperBroker
from autotradebot.config import PairsCfg
from autotradebot.core.enums import Side
from autotradebot.data.market_data import quote_from_price
from autotradebot.execution.executor import Executor
from autotradebot.execution.exit_manager import ExitManager
from autotradebot.pairs import desk as desk_module
from autotradebot.pairs.desk import PairDesk
from autotradebot.pairs.model import LONG_SPREAD, PairModel
from autotradebot.util import clock

SILENT = SimpleNamespace(publish=lambda *a, **k: None)
VENUE = "pairdesk-test"
EXECUTION = SimpleNamespace(limit_offset_bps=5.0, default_order_type="LIMIT", time_in_force="DAY", bracket_orders=True)


class _Broker(PaperBroker):
    """The simulator, except that some stocks can't be shorted."""

    def __init__(self, prices, no_short=()):
        super().__init__(quote=lambda s: quote_from_price(s, prices[s]), starting_cash=1_000_000.0, slippage_bps=0.0)
        self.no_short = set(no_short)

    def place_order(self, req):
        if req.is_entry and req.side is Side.SHORT and req.symbol in self.no_short:
            raise OrderRejected(f"{req.symbol} can't be borrowed")
        return super().place_order(req)


def _closes(first, second, n=40):
    """Daily closes whose spread wobbles around zero: the first stock 0.5% either side of 100,
    the second steady at 50."""
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz="America/New_York")
                            for d in sorted(clock.last_n_sessions(clock.prev_trading_day(clock.session_date()), n))])
    a = 100 * np.exp(0.005 * (-1) ** np.arange(n))
    return {first: pd.DataFrame({"close": a, "volume": 1e6}, index=idx),
            second: pd.DataFrame({"close": np.full(n, 50.0), "volume": 1e6}, index=idx)}


def _model(first, second):
    return PairModel(first=first, second=second, hedge=1.0, half_life=5.0, lookback=10, entry_z=1.5, exit_z=0.0,
                     stop_z=3.5, time_stop_days=10, adf_stat=-4.0, correlation=0.9, crossings=12, group="Test")


@pytest.fixture
def setup(repo, tmp_path, monkeypatch):
    from autotradebot.persistence.db import DB

    # a database of its own - these trades would otherwise count in other tests' P/L
    DB.init(url=f"sqlite:///{(tmp_path / 'pairs.sqlite').as_posix()}")
    DB.create_all()
    DB.engine.dispose()                   # fresh connections, so SQLite enforces foreign keys as the app's do
    monkeypatch.setattr(desk_module.clock, "current_session", lambda *a: clock.Session.REGULAR)

    def build(first, second, no_short=()):
        prices = {first: 100.0 * np.exp(-0.03), second: 50.0}          # the spread has dropped: long it
        broker = _Broker(prices, no_short)
        broker.connect()
        executor = Executor(broker, repo, EXECUTION, bus=SILENT, venue=VENUE)
        desk = PairDesk(repo, PairsCfg(), tmp_path / f"{first}.json", bus=SILENT)
        desk.models = [_model(first, second)]
        frames = _closes(first, second)

        def enter():
            return desk.enter(f"{first}/{second}", executor=executor, account=broker.get_account(), prices=prices,
                              closes=frames.get, risk_dollars=1_000.0, max_leg_value=200_000.0,
                              buying_power=2_000_000.0, holding=[], venue=VENUE)
        return SimpleNamespace(desk=desk, executor=executor, broker=broker, prices=prices, frames=frames, enter=enter)
    yield build
    DB.engine.dispose()
    DB.init(url=os.environ["DATABASE_URL"])


def _record(repo, pid):
    return repo.get_pair_trade(pid)


def test_both_legs_go_on_together_and_the_exit_manager_leaves_them_to_the_desk(setup, repo):
    s = setup("PDA", "PDB")
    out = s.enter()
    rec = _record(repo, out["pair_trade_id"])
    assert out["ok"] and rec["status"] == "OPEN" and rec["side"] == LONG_SPREAD
    legs = {t["symbol"]: t for t in repo.trades_for_pair(rec["id"])}
    assert legs["PDA"]["side"] == "LONG" and legs["PDB"]["side"] == "SHORT"
    assert all(t["stop_price"] is None and t["target_price"] is None and not t["managed_exit"] for t in legs.values())
    assert all(repo.get_play(t["play_id"])["status"] == "FILLED" for t in legs.values())      # each leg's play is on record
    exits = ExitManager(repo, s.executor, quote_fn=s.broker.get_quote,
                        cfg=SimpleNamespace(enabled=True), bus=SILENT, venue=VENUE)
    assert exits.run_once() == [] and all(t["status"] == "OPEN" for t in repo.trades_for_pair(rec["id"]))
    assert not s.enter()["ok"]                                                   # already on
    s.desk.close(rec["id"], s.executor)
    s.desk.manage(s.executor, s.prices, s.frames.get, in_window=False)


def test_back_at_the_mean_both_legs_come_off_and_the_pair_books_its_result_in_r(setup, repo):
    s = setup("PDC", "PDD")
    pid = s.enter()["pair_trade_id"]
    s.prices["PDC"] = 100.0                                                      # the spread is back
    assert s.desk.manage(s.executor, s.prices, s.frames.get, in_window=False) == []   # decided near the close only
    acted = s.desk.manage(s.executor, s.prices, s.frames.get, in_window=True)
    assert [a["reason"] for a in acted] == ["mean"]
    s.desk.manage(s.executor, s.prices, s.frames.get, in_window=True)
    rec = _record(repo, pid)
    assert rec["status"] == "CLOSED" and rec["exit_reason"] == "mean" and rec["realized_pl"] > 0 and rec["r_multiple"] > 0
    assert all(t["status"] == "CLOSED" for t in repo.trades_for_pair(pid))


def test_a_pair_losing_twice_its_planned_risk_is_closed_at_once(setup, repo):
    s = setup("PDE", "PDF")
    pid = s.enter()["pair_trade_id"]
    s.prices["PDE"] = s.prices["PDE"] * 0.85                                     # the spread keeps falling, hard
    acted = s.desk.manage(s.executor, s.prices, s.frames.get, in_window=False)
    assert [a["reason"] for a in acted] == ["emergency-stop"]
    s.desk.manage(s.executor, s.prices, s.frames.get, in_window=False)
    assert _record(repo, pid)["status"] == "CLOSED"


def test_a_leg_that_cant_be_sent_unwinds_the_other(setup, repo):
    s = setup("PDG", "PDH", no_short=["PDH"])
    out = s.enter()
    assert not out["ok"] and "PDH" in out["reason"]
    [rec] = [r for r in repo.pair_trades(limit=20) if r["pair"] == "PDG/PDH"]
    assert rec["status"] == "FAILED" and all(t["status"] == "CLOSED" for t in repo.trades_for_pair(rec["id"]))
    position = s.broker.get_account().position("PDG")
    assert position is None or abs(position.quantity) < 1e-9


def test_an_error_after_a_leg_filled_leaves_no_shares_behind(setup, repo, monkeypatch):
    s = setup("PDN", "PDO")
    book = s.executor._open_trade

    def broken(play, *args, **kwargs):
        if play.symbol == "PDN":
            raise RuntimeError("the database went away")
        return book(play, *args, **kwargs)

    monkeypatch.setattr(s.executor, "_open_trade", broken)
    out = s.enter()
    assert not out["ok"] and "the database went away" in out["reason"]
    position = s.broker.get_account().position("PDN")
    assert position is None or abs(position.quantity) < 1e-9                    # the filled shares were sold again
    assert s.broker.get_account().position("PDO") is None                        # the second leg was never sent
    assert [r["status"] for r in repo.pair_trades(limit=20) if r["pair"] == "PDN/PDO"] == ["FAILED"]


def _refuse_bookings(repo, monkeypatch, symbol):
    """The database refuses to book ``symbol``'s fills (another program holds it, say) while the returned set holds it."""
    real, refusing = repo.open_trade, {symbol}

    def open_trade(play, *args, **kwargs):
        if play.symbol in refusing:
            raise RuntimeError("database is locked")
        return real(play, *args, **kwargs)

    monkeypatch.setattr(repo, "open_trade", open_trade)
    return refusing


def _held(s, symbol):
    position = s.broker.get_account().position(symbol)
    return 0.0 if position is None else position.quantity


def test_a_filled_leg_the_database_wont_book_is_closed_once_it_does_when_the_other_leg_cant_be_sent(
        setup, repo, monkeypatch):
    s = setup("PDP", "PDQ", no_short=["PDQ"])
    refusing = _refuse_bookings(repo, monkeypatch, "PDP")
    out = s.enter()
    assert not out["ok"] and "PDQ" in out["reason"]
    assert _held(s, "PDP") > 0 and [w["unbooked"] for w in s.executor.working_entries()] == [True]   # still followed
    refusing.clear()                                                             # the database takes bookings again
    s.executor.sync_open_orders()                                                # the leg is booked...
    s.desk.manage(s.executor, s.prices, s.frames.get, in_window=False)           # ...and closed: its pair is over
    s.executor.sync_open_orders()
    assert abs(_held(s, "PDP")) < 1e-9
    [rec] = [r for r in repo.pair_trades(limit=20) if r["pair"] == "PDP/PDQ"]
    [leg] = repo.trades_for_pair(rec["id"])
    assert rec["status"] == "FAILED" and (leg["status"], leg["exit_reason"]) == ("CLOSED", "pair-unwind")


def test_legs_that_dont_both_book_in_time_are_closed_one_whose_booking_waited_on_the_database_too(
        setup, repo, monkeypatch):
    s = setup("PDR", "PDS")
    refusing = _refuse_bookings(repo, monkeypatch, "PDR")
    out = s.enter()
    assert out["ok"] and out["status"] == "ENTERING" and _held(s, "PDR") > 0 and _held(s, "PDS") < 0
    monkeypatch.setattr(desk_module, "ENTRY_TIMEOUT_S", -1.0)                    # their time is up
    s.desk.sync(s.executor)
    assert _record(repo, out["pair_trade_id"])["status"] == "FAILED" and abs(_held(s, "PDS")) < 1e-9
    refusing.clear()
    s.executor.sync_open_orders()
    s.desk.manage(s.executor, s.prices, s.frames.get, in_window=False)
    s.executor.sync_open_orders()
    assert abs(_held(s, "PDR")) < 1e-9
    assert all(t["status"] == "CLOSED" for t in repo.trades_for_pair(out["pair_trade_id"]))


def test_a_leg_closed_outside_the_desk_takes_the_other_with_it(setup, repo):
    s = setup("PDJ", "PDK")
    pid = s.enter()["pair_trade_id"]
    first = next(t for t in repo.trades_for_pair(pid) if t["symbol"] == "PDJ")
    assert s.executor.close_trade(first["id"], reason="manual")["ok"]
    s.desk.manage(s.executor, s.prices, s.frames.get, in_window=False)
    assert _record(repo, pid)["exit_reason"] == "leg-closed"
    s.desk.manage(s.executor, s.prices, s.frames.get, in_window=False)
    assert _record(repo, pid)["status"] == "CLOSED" and all(t["status"] == "CLOSED" for t in repo.trades_for_pair(pid))


def test_nothing_is_entered_inside_the_band_or_outside_the_regular_session(setup, repo, monkeypatch):
    s = setup("PDL", "PDM")
    s.prices["PDL"] = 100.0
    out = s.enter()
    assert not out["ok"] and "inside" in out["reason"]
    monkeypatch.setattr(desk_module.clock, "current_session", lambda *a: clock.Session.PRE)
    s.prices["PDL"] = 100.0 * np.exp(-0.03)
    assert "regular session" in s.enter()["reason"]
