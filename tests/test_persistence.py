from __future__ import annotations

import pytest

from autotradebot.core.enums import Side, StrategyKind, Timeframe
from autotradebot.core.models import Play


def _play(**kw):
    d = dict(symbol="ZZZ", side=Side.LONG, strategy="vwap_reclaim",
             kind=StrategyKind.TECHNICAL, timeframe=Timeframe.INTRADAY,
             entry=100.0, stop=98.0, targets=[104.0])
    d.update(kw)
    p = Play(**d)
    p.suggested_qty = 10
    p.expected_hold_typical = kw.get("expected_hold_typical", 60.0)
    p.expected_hold_max = kw.get("expected_hold_max", 120.0)
    return p


def test_expected_exit_times_recorded(repo):
    p = _play(symbol="TIMED", timeframe=Timeframe.SWING,
              expected_hold_typical=5.0, expected_hold_max=12.0)
    repo.record_play(p)
    tid = repo.open_trade(p, 100.0, 3, "paper")
    t = repo.get_trade(tid)
    assert t["expected_exit_at"] and t["overwatch_at"]
    assert t["overwatch_at"] > t["expected_exit_at"]
    assert t["time_status"] == "on_track"          # just opened
    assert t["held_label"]


def test_trade_lifecycle_and_pnl(repo):
    before = repo.pnl_summary()                      # other tests share this database: count what this one adds
    p = _play()
    repo.record_play(p)
    tid = repo.open_trade(p, fill_price=100.0, fill_qty=10, broker="paper")
    out = repo.close_trade(tid, exit_price=104.0, exit_reason="target")
    assert out["realized_pl"] == 40.0
    assert out["r_multiple"] == 2.0                 # (4 reward) / (2 risk)
    assert out["is_day_trade"] is True
    s = repo.pnl_summary()
    assert s["n_closed"] == before["n_closed"] + 1
    assert abs(s["realized_total"] - before["realized_total"] - 40.0) < 1e-6


def test_a_refusal_noted_on_a_play_row_outlasts_the_scans_and_the_approval_that_write_the_row_again(repo):
    import dataclasses

    from autotradebot.scanner.scanner import ScanResult

    p = _play(symbol="RFA")
    repo.record_play(p)
    note = {"stage": "last look", "reason": "the spread is too dear to cross", "confidence": 0.7,
            "reward_risk": 2.0, "confirmations": 2, "noise": []}
    repo.note_refusal(p, note)
    row = repo.get_play(p.id)
    assert row["evidence"]["autopilot_refused"] == note and row["status"] == "PROPOSED"   # still offered

    again = dataclasses.replace(p, confirmations=3, evidence={})            # the next scan's play for the same setup
    repo.record_scan(ScanResult(kind="cycle", plays=[again]), plays=[again])
    row = repo.get_play(p.id)
    assert row["evidence"]["autopilot_refused"] == note and row["confirmations"] == 3
    repo.record_play(again)                                                 # taken by a click later
    assert repo.get_play(p.id)["evidence"]["autopilot_refused"] == note

    unlogged = _play(symbol="RFB")                                          # no scan logged it: the row is written
    repo.note_refusal(unlogged, note)
    assert repo.get_play(unlogged.id)["evidence"]["autopilot_refused"] == note


def test_closed_trades_are_counted_by_type_as_a_profit_a_loss_or_even(repo):
    before = repo.pnl_summary()["by_type"]          # other tests share this database: count what this one adds

    def trade(exit_price, **kw):
        p = _play(**kw)
        repo.record_play(p)
        tid = repo.open_trade(p, fill_price=100.0, fill_qty=10, broker="paper")
        return tid if exit_price is None else repo.close_trade(tid, exit_price=exit_price, exit_reason="target")

    trade(104.0, symbol="DAYW")
    trade(98.0, symbol="DAYL")
    trade(100.0, symbol="DAYE")
    trade(103.0, symbol="SWW", timeframe=Timeframe.SWING)
    trade(None, symbol="SWO", timeframe=Timeframe.SWING)                   # still open: not counted
    trade(106.0, symbol="PRA", timeframe=Timeframe.SWING, pair_id="pair_1")
    trade(97.0, symbol="PRB", timeframe=Timeframe.SWING, pair_id="pair_1")  # the pair: +60 and -30 = a profit
    trade(99.0, symbol="PRC", timeframe=Timeframe.SWING, pair_id="pair_2")
    trade(None, symbol="PRD", timeframe=Timeframe.SWING, pair_id="pair_2")  # a leg still open: the pair isn't in

    after = repo.pnl_summary()["by_type"]
    added = {k: {n: after[k][n] - before[k][n] for n in after[k]} for k in after}
    assert added == {"INTRADAY": {"closed": 3, "profit": 1, "loss": 1, "even": 1},
                     "SWING": {"closed": 1, "profit": 1, "loss": 0, "even": 0},
                     "PAIRS": {"closed": 1, "profit": 1, "loss": 0, "even": 0}}


def test_the_scale_out_math(repo):
    p = _play(symbol="MATH", entry=100.0, stop=98.0, targets=[104.0, 108.0])
    repo.record_play(p)
    tid = repo.open_trade(p, fill_price=100.0, fill_qty=10, broker="paper")
    t = repo.get_trade(tid)
    assert (t["initial_quantity"], t["banked_pl"], t["target2_price"]) == (10, 0.0, 108.0)

    part = repo.reduce_trade(tid, 5, 104.0, exit_reason="target-1", commission=1.0,
                             stop_price=100.05, target_price=108.0)
    assert part["status"] == "OPEN" and (part["quantity"], part["initial_quantity"]) == (5, 10)
    assert part["banked_pl"] == 19.0 and (part["stop_price"], part["target_price"]) == (100.05, 108.0)
    lowered = repo.reduce_trade(tid, 0, 104.0, stop_price=99.0)     # nothing off, and a stop that isn't better
    assert lowered["quantity"] == 5 and lowered["stop_price"] == 100.05

    out = repo.close_trade(tid, exit_price=108.0, exit_reason="target")
    assert out["status"] == "CLOSED" and out["quantity"] == 5
    assert out["realized_pl"] == 19.0 + 40.0                             # 5 x 4 - 1 banked, then 5 x 8
    assert out["r_multiple"] == pytest.approx(59.0 / (2.0 * 10))        # on the ten shares entered with
    assert out["realized_pl_pct"] == pytest.approx(59.0 / 1000.0 * 100)
    exits = [f for f in repo.trade_record(tid)["fills"] if f.get("leg") == "EXIT"]
    assert sorted(f["quantity"] for f in exits) == [5.0, 5.0]

    p2 = _play(symbol="WHOLE", entry=100.0, stop=98.0, targets=[104.0, 108.0])
    repo.record_play(p2)
    tid2 = repo.open_trade(p2, fill_price=100.0, fill_qty=10, broker="paper")
    whole = repo.reduce_trade(tid2, 10, 104.0, exit_reason="target-1")   # the part is the whole position
    assert whole["status"] == "CLOSED" and whole["realized_pl"] == 40.0 and whole["r_multiple"] == 2.0


def test_every_fee_comes_off_the_pl_the_entrys_too(repo):
    p = _play(symbol="FEES", entry=100.0, stop=98.0, targets=[104.0, 108.0])
    repo.record_play(p)
    tid = repo.open_trade(p, fill_price=100.0, fill_qty=10, broker="ibkr-paper", commission=1.0)
    part = repo.reduce_trade(tid, 5, 104.0, exit_reason="target-1", commission=0.5)
    assert part["banked_pl"] == 19.5                                     # 5 x 4, less the part's own fee
    out = repo.close_trade(tid, exit_price=108.0, exit_reason="target", commission=0.5)
    assert out["fees"] == 2.0
    assert out["realized_pl"] == pytest.approx(20.0 + 40.0 - 2.0)       # 5 x 4 and 5 x 8, less every fee once
    assert out["r_multiple"] == pytest.approx(58.0 / (2.0 * 10))


def test_a_fee_the_broker_reports_after_the_fill_was_booked_is_added_to_the_record(repo):
    from autotradebot.util import clock

    p = _play(symbol="LATE", entry=100.0, stop=98.0, targets=[104.0, 108.0])
    repo.record_play(p)
    tid = repo.open_trade(p, 100.0, 10, "ibkr-paper", broker_order_id="901")
    repo.reduce_trade(tid, 5, 104.0, exit_reason="target-1", broker_order_id="902", commission=0.2)
    today = clock.now_ny().date()

    def booked():
        return {r["order_id"]: r for r in repo.fills_on("ibkr-paper", today) if r["trade_id"] == tid}

    first = booked()
    assert sorted(first) == ["901", "902"] and (first["901"]["leg"], first["902"]["leg"]) == ("ENTRY", "EXIT")
    assert (first["901"]["commission"], first["902"]["commission"]) == (0.0, pytest.approx(0.2))
    # the rest of each fee, reported after the booking
    assert repo.add_fill_fees({first["901"]["fill_id"]: 1.0, first["902"]["fill_id"]: 0.3}) == [tid, tid]
    t = repo.get_trade(tid)
    assert (t["fees"], t["banked_pl"]) == (pytest.approx(1.5), pytest.approx(19.5))   # 5 x 4, less the part's fee
    out = repo.close_trade(tid, 108.0, exit_reason="target", broker_order_id="903")
    assert out["realized_pl"] == pytest.approx(60.0 - 1.5)
    assert booked()["903"]["commission"] == 0.0
    repo.add_fill_fees({booked()["903"]["fill_id"]: 0.5})                    # the closing fill's, later still
    out = repo.get_trade(tid)
    assert (out["fees"], out["banked_pl"], out["realized_pl"]) == (pytest.approx(2.0), pytest.approx(19.5),
                                                                   pytest.approx(58.0))
    assert out["r_multiple"] == pytest.approx(58.0 / 20.0) and out["realized_pl_pct"] == pytest.approx(5.8)
    assert [r["commission"] for r in booked().values()] == [1.0, pytest.approx(0.5), 0.5]
    assert repo.first_fee_day() is not None and repo.first_fee_day() <= today


def test_short_trade_pnl(repo):
    p = _play(symbol="SHRT", side=Side.SHORT, entry=50.0, stop=52.0, targets=[46.0])
    repo.record_play(p)
    tid = repo.open_trade(p, 50.0, 10, "paper")
    out = repo.close_trade(tid, 46.0, "target")
    assert out["realized_pl"] == 40.0              # short: profit when price falls


def test_the_plays_sent_whose_ending_was_never_heard_are_listed_for_the_start(repo):
    import datetime as dt

    start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    sent, filled, cancelled = _play(symbol="T90"), _play(symbol="T91"), _play(symbol="T92")
    for p in (sent, filled, cancelled):
        repo.record_play(p)
        repo.set_play_status(p.id, "ACCEPTED", "autopilot")
        repo.settle_play(p.id, "SUBMITTED")
    repo.open_trade(filled, 100.0, 10, "ibkr-paper")                   # its fill was heard: it has its trade...
    repo.set_play_status(filled.id, "SUBMITTED", "autopilot")          # ...whatever its row says
    repo.settle_play(cancelled.id, "CANCELED")
    until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)
    found = {r["id"]: r for r in repo.submitted_plays(start, until)}
    assert sent.id in found and filled.id not in found and cancelled.id not in found
    assert (found[sent.id]["symbol"], found[sent.id]["status"]) == ("T90", "SUBMITTED")
    assert sent.id not in {r["id"] for r in repo.submitted_plays(start - dt.timedelta(days=1), start)}


def test_a_brokers_error_written_after_the_rows_around_it_keeps_its_place_in_the_trade_record(repo):
    import datetime as dt

    p = _play(symbol="T93")
    repo.record_play(p)
    tid = repo.open_trade(p, 100.0, 10, "ibkr-paper")
    t0 = dt.datetime.now(dt.timezone.utc)
    at = {"PLACE": t0, "CANCEL": t0 + dt.timedelta(seconds=1), "MODIFY": t0 + dt.timedelta(seconds=3)}
    stop = {"symbol": "T93", "side": "SHORT", "qty": 10, "type": "STOP", "stop": 98.0, "tag": f"stop:{tid}"}
    repo.record_order_audit("PLACE", stop, {"order_id": "7", "status": "SUBMITTED", "message": ""}, True, "ibkr",
                            trade_id=tid, message="protective stop", ts=at["PLACE"])
    repo.record_order_audit("CANCEL", {"order_id": "7", "symbol": "T93", "why": "stood down"}, {"status": "sent"},
                            True, "ibkr", trade_id=tid, message="stood down", ts=at["CANCEL"])
    repo.record_order_audit("MODIFY", {"order_id": "7", "symbol": "T93", "stop": 99.0}, {"error": "refused"}, False,
                            "ibkr", trade_id=tid, message="refused", ts=at["MODIFY"])
    # the broker's refusal of the cancel, heard between the two and written last, on the next order sync
    repo.record_order_audit("ERROR", {"order_id": "7", "symbol": "T93", "tag": f"stop:{tid}"},
                            {"code": 10148, "state": "Filled"}, False, "ibkr", trade_id=tid,
                            message="cancel refused (10148)", ts=t0 + dt.timedelta(seconds=2))
    orders = repo.trade_record(tid)["orders"]
    assert [(o["action"], o["ok"]) for o in orders] == [("PLACE", True), ("CANCEL", True), ("ERROR", False),
                                                        ("MODIFY", False)]
    assert orders[2]["message"] == "cancel refused (10148)"


def test_day_trade_counter(repo):
    for i in range(2):
        p = _play(symbol=f"DT{i}")
        repo.record_play(p)
        tid = repo.open_trade(p, 100.0, 1, "paper")
        repo.close_trade(tid, 101.0, "target")
    assert repo.count_day_trades(5) >= 2


def _entered_last_session(trade_id):
    """Moves a trade's entry back to the session before today's."""
    import datetime as dt

    from autotradebot.persistence.db import session_scope
    from autotradebot.persistence.models_orm import Trade
    from autotradebot.util import clock

    prev = clock.prev_trading_day(clock.session_date())
    with session_scope() as s:
        t = s.get(Trade, trade_id)
        t.entry_time = (dt.datetime.combine(prev, dt.time(15, 0), tzinfo=clock.NY)
                        .astimezone(dt.timezone.utc).replace(tzinfo=None))
        t.session_date = prev


def test_a_part_sold_on_the_session_the_trade_was_entered_makes_it_a_day_trade(repo):
    import datetime as dt

    from autotradebot.util import clock

    before = repo.count_day_trades(5)                # other tests share this database: count what this one adds
    p = _play(symbol="T95", timeframe=Timeframe.SWING, targets=[104.0, 108.0])
    repo.record_play(p)
    tid = repo.open_trade(p, 100.0, 10, "ibkr-paper")
    assert repo.get_trade(tid)["is_day_trade"] is False
    assert repo.reduce_trade(tid, 5, 104.0)["is_day_trade"] is True
    assert repo.count_day_trades(5) == before + 1    # a day trade already, while the rest is still held
    repo.reduce_trade(tid, 2, 105.0, exit_reason="exit")
    assert repo.count_day_trades(5) == before + 1    # one trade, however many parts it leaves in
    later = dt.datetime.combine(clock.next_trading_day(clock.session_date()), dt.time(10, 0), tzinfo=clock.NY)
    out = repo.close_trade(tid, 106.0, exit_reason="target", exit_time=later)
    assert out["is_day_trade"] is True               # the rest went on a later session: it was one all the same
    assert repo.count_day_trades(5) == before + 1

    # entered on the session before: a part sold today is no day trade, whatever kind of trade it is
    for timeframe, symbol in ((Timeframe.SWING, "T96"), (Timeframe.INTRADAY, "T97")):
        q = _play(symbol=symbol, timeframe=timeframe, targets=[104.0, 108.0])
        repo.record_play(q)
        qid = repo.open_trade(q, 100.0, 10, "ibkr-paper")
        _entered_last_session(qid)
        repo.reduce_trade(qid, 5, 104.0)
        assert repo.count_day_trades(5) == before + 1, symbol
        assert repo.close_trade(qid, 105.0, exit_reason="target")["is_day_trade"] is False, symbol


def _unstamped(trade_id):
    """Clears a trade's broker: a record with none on it is the simulator's."""
    from autotradebot.persistence.db import session_scope
    from autotradebot.persistence.models_orm import Trade

    with session_scope() as s:
        s.get(Trade, trade_id).broker = ""


def test_day_trades_are_counted_for_the_venue_asked_for(repo):
    from autotradebot.core.models import Account
    from autotradebot.persistence.db import session_scope
    from autotradebot.persistence.models_orm import AccountSnapshot
    from sqlalchemy import select

    venues = ("paper", "ibkr-paper", "ibkr-live", None)
    before = {v: repo.count_day_trades(5, venue=v) for v in venues}   # other tests share this database

    def day_trade(symbol, broker):
        p = _play(symbol=symbol)
        repo.record_play(p)
        tid = repo.open_trade(p, 100.0, 1, broker)
        repo.close_trade(tid, 101.0, "target")
        return tid

    day_trade("T60", "ibkr-live")
    day_trade("T61", "ibkr-live")
    _unstamped(day_trade("T62", "paper"))                       # no broker on it: the simulator's
    p = _play(symbol="T63")                                     # an open day trade entered today
    repo.record_play(p)
    repo.open_trade(p, 100.0, 1, "ibkr-paper")

    added = {v: repo.count_day_trades(5, venue=v) - before[v] for v in venues}
    assert added == {"paper": 1, "ibkr-paper": 1, "ibkr-live": 2, None: 4}

    # the account snapshot keeps its own venue's count
    repo.snapshot_account(Account(account_id="t", equity=5000.0), "ibkr-live")
    with session_scope() as s:
        row = s.execute(select(AccountSnapshot).where(AccountSnapshot.broker == "ibkr-live")
                        .order_by(AccountSnapshot.id.desc())).scalars().first()
        assert row.day_trades_5d == repo.count_day_trades(5, venue="ibkr-live")
        assert row.day_trades_5d != repo.count_day_trades(5)


def test_todays_and_the_weeks_pl_are_the_trades_closed_then_on_the_venue_asked_for(repo):
    import datetime as dt

    from autotradebot.persistence.db import session_scope
    from autotradebot.persistence.models_orm import Trade
    from autotradebot.util import clock

    venues = ("paper", "ibkr-live", None)
    before = {v: repo.pnl_summary(venue=v) for v in venues}     # other tests share this database
    sessions = clock.last_n_sessions(clock.session_date(), 8)

    def at(day):
        return dt.datetime.combine(day, dt.time(15, 0), tzinfo=clock.NY)

    def closed(symbol, broker, exit_price, entered=None, exited=None):
        p = _play(symbol=symbol, timeframe=Timeframe.SWING)
        repo.record_play(p)
        tid = repo.open_trade(p, 100.0, 10, broker)
        if entered is not None:
            with session_scope() as s:
                t = s.get(Trade, tid)
                t.entry_time, t.session_date = at(entered).astimezone(dt.timezone.utc).replace(tzinfo=None), entered
        repo.close_trade(tid, exit_price, exit_reason="target", exit_time=None if exited is None else at(exited))
        return tid

    closed("T70", "ibkr-live", 103.0, entered=sessions[-7])                       # entered last week, closed today
    closed("T71", "ibkr-live", 101.0, entered=sessions[-2], exited=sessions[-2])  # closed the session before
    closed("T72", "ibkr-live", 105.0, entered=sessions[0], exited=sessions[-7])   # closed before the week
    closed("T73", "paper", 98.0)                                                  # the simulator's, today
    _unstamped(closed("T74", "paper", 99.5))                                      # no broker: the simulator's

    def added(venue, key):
        return round(repo.pnl_summary(venue=venue)[key] - before[venue][key], 2)

    assert (added("ibkr-live", "realized_today"), added("ibkr-live", "realized_week")) == (30.0, 40.0)
    assert (added("paper", "realized_today"), added("paper", "realized_week")) == (-25.0, -25.0)
    assert (added(None, "realized_today"), added(None, "realized_week")) == (5.0, 15.0)
    assert added("ibkr-live", "realized_total") == added("paper", "realized_total") == 65.0    # every venue's


def test_a_part_taken_off_marks_the_trades_best_and_worst_prices(repo):
    p = _play(symbol="T98", targets=[104.0, 108.0])
    repo.record_play(p)
    tid = repo.open_trade(p, 100.0, 10, "paper")
    part = repo.reduce_trade(tid, 4, 104.5)                          # past the best point marked so far
    assert (part["mfe"], part["hwm_price"]) == (pytest.approx(4.5), 104.5) and part["mfe_at"]
    part = repo.reduce_trade(tid, 3, 103.0, exit_reason="exit")      # short of it: the marks stay
    assert (part["mfe"], part["hwm_price"]) == (pytest.approx(4.5), 104.5) and not part["mae"]

    q = _play(symbol="T99", side=Side.SHORT, entry=50.0, stop=52.0, targets=[46.0, 44.0])
    repo.record_play(q)
    qid = repo.open_trade(q, 50.0, 10, "paper")
    repo.update_trade_risk(qid, mae=0.5)
    part = repo.reduce_trade(qid, 5, 51.25, exit_reason="exit")      # through the worst point marked so far
    assert part["mae"] == pytest.approx(1.25) and part["hwm_price"] == 50.0 and not part["mfe"]


def _race(monkeypatch, other):
    """Runs ``other`` on a second thread the first time a trade is read, and gives it up to a second to finish before
    the read returns. A booking that reads the record and writes it in two steps lets it in between them; one that
    takes the record in a conditional write first holds the database's write lock, so ``other`` waits for its commit.
    Returns (the thread, a list that gets what ``other`` returned)."""
    import threading

    from sqlalchemy.orm import Session

    real, runs, results = Session.get, [], []

    def get(self, *args, **kwargs):
        found = real(self, *args, **kwargs)
        if not runs:
            runs.append(threading.Thread(target=lambda: results.append(other()), daemon=True))
            runs[0].start()
            runs[0].join(timeout=1.0)
        return found

    monkeypatch.setattr(Session, "get", get)
    return runs, results


def test_two_closes_of_one_record_at_once_book_it_once(repo, monkeypatch):
    p = _play(symbol="T90")
    repo.record_play(p)
    tid = repo.open_trade(p, 100.0, 10, "paper")
    runs, results = _race(monkeypatch, lambda: repo.close_trade(tid, 103.0, exit_reason="target"))
    first = repo.close_trade(tid, 101.0, exit_reason="stop")
    runs[0].join(timeout=30)
    exits = [f for f in repo.trade_record(tid)["fills"] if f["leg"] == "EXIT"]
    assert [(f["quantity"], f["price"]) for f in exits] == [(10.0, 101.0)]
    t = repo.get_trade(tid)
    assert (t["status"], t["exit_reason"], t["realized_pl"]) == ("CLOSED", "stop", 10.0)
    assert first["realized_pl"] == 10.0
    assert results == [t]                            # the second close found it closed, and booked nothing


def test_two_parts_taken_off_one_record_at_once_never_take_off_more_than_it_holds(repo, monkeypatch):
    p = _play(symbol="T91", targets=[104.0, 108.0])
    repo.record_play(p)
    tid = repo.open_trade(p, 100.0, 10, "paper")
    runs, results = _race(monkeypatch, lambda: repo.reduce_trade(tid, 6, 104.0, exit_reason="exit"))
    mine = repo.reduce_trade(tid, 6, 103.0, exit_reason="exit")
    runs[0].join(timeout=30)
    assert (mine["status"], mine["quantity"], mine["banked_pl"]) == ("OPEN", 4.0, 18.0)
    # the other found four shares left - the whole position - so it closed the trade with them
    exits = [f for f in repo.trade_record(tid)["fills"] if f["leg"] == "EXIT"]
    assert [(f["quantity"], f["price"]) for f in exits] == [(6.0, 103.0), (4.0, 104.0)]
    t = repo.get_trade(tid)
    assert (t["status"], t["quantity"], t["realized_pl"]) == ("CLOSED", 4.0, 18.0 + 16.0)
    assert results == [t]


def test_the_recent_trades_carry_each_trades_exit_average_however_many_are_read(repo, monkeypatch):
    from autotradebot.persistence import repository

    monkeypatch.setattr(repository, "_IN_CHUNK", 2)                 # the ids are looked up two at a time
    want = {}
    for i, price in enumerate((101.0, 102.0, 103.0, 104.0, 105.0)):
        p = _play(symbol=f"T7{i}")
        repo.record_play(p)
        tid = repo.open_trade(p, 100.0, 10, "paper")
        repo.reduce_trade(tid, 5, price)                            # half off, the other half a dollar higher
        repo.close_trade(tid, price + 1.0, exit_reason="target")
        want[tid] = (price + 0.5, 2)
    rows = {t["id"]: t for t in repo.recent_trades(limit=50)}
    assert {tid: (rows[tid]["exit_avg_price"], rows[tid]["exit_parts"]) for tid in want} == want


def test_the_startup_migration_quotes_names_and_writes_defaults_the_databases_way(tmp_path, monkeypatch):
    """An older database missing a column gets it at start-up with its default in place, even when the
    name is an SQL keyword and the default holds both kinds of quote."""
    import types

    import sqlalchemy as sa

    from autotradebot.persistence import db as dbmod

    now = sa.MetaData()                              # the table as the models describe it today
    sa.Table("group", now, sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("order", sa.String(16), default='it\'s "ok"'),
             sa.Column("done", sa.Boolean, default=True))
    older = sa.MetaData()                            # the same table in a database made before those columns
    sa.Table("group", older, sa.Column("id", sa.Integer, primary_key=True))
    eng = sa.create_engine("sqlite:///" + (tmp_path / "older.sqlite").as_posix())
    older.create_all(eng)

    monkeypatch.setattr(dbmod, "Base", types.SimpleNamespace(metadata=now))
    db = dbmod._DB()
    db.engine = eng
    db._add_missing_columns()

    live = sa.Table("group", sa.MetaData(), autoload_with=eng)
    assert set(live.columns.keys()) == {"id", "order", "done"}
    with eng.begin() as conn:                        # read back from the file, the table has no Python-side
        conn.execute(sa.insert(live).values(id=1))   # defaults: the database fills them in
        row = conn.execute(sa.select(live)).one()
    assert row._asdict() == {"id": 1, "order": 'it\'s "ok"', "done": True}
    eng.dispose()
