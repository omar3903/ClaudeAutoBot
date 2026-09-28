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


def test_short_trade_pnl(repo):
    p = _play(symbol="SHRT", side=Side.SHORT, entry=50.0, stop=52.0, targets=[46.0])
    repo.record_play(p)
    tid = repo.open_trade(p, 50.0, 10, "paper")
    out = repo.close_trade(tid, 46.0, "target")
    assert out["realized_pl"] == 40.0              # short: profit when price falls


def test_day_trade_counter(repo):
    for i in range(2):
        p = _play(symbol=f"DT{i}")
        repo.record_play(p)
        tid = repo.open_trade(p, 100.0, 1, "paper")
        repo.close_trade(tid, 101.0, "target")
    assert repo.count_day_trades(5) >= 2


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
