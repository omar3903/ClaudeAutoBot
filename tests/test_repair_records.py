"""scripts/repair_records.py: never-moved stops booked as trailing stops, slippage measured on delayed quotes."""

from __future__ import annotations

import datetime as dt
import importlib.util
import os
import pathlib
import sys

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "repair_records.py"
ENTRY = ("decision_price", "entry_slippage_bps")
EXIT = ("exit_decision_price", "exit_slippage_bps")


@pytest.fixture
def db(tmp_path):
    """A database of its own - the script looks at every row."""
    from autotradebot.persistence.db import DB

    DB.init(url=f"sqlite:///{(tmp_path / 'repair.sqlite').as_posix()}")
    DB.create_all()
    yield
    DB.engine.dispose()
    DB.init(url=os.environ["DATABASE_URL"])


def _script(monkeypatch):
    spec = importlib.util.spec_from_file_location("repair_records", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "init_db", lambda: None)            # stay on the test's database
    return mod


def _trade(tid, symbol, entered, exited=None, reason="", stop=95.0, initial_stop=95.0, measured=True):
    """A trade as an earlier app stored it - times naive UTC, slippage measured whatever the quote."""
    from autotradebot.persistence.db import session_scope
    from autotradebot.persistence.models_orm import Trade

    slip = dict(decision_price=100.0, entry_slippage_bps=8.0, exit_decision_price=104.0, exit_slippage_bps=6.0)
    with session_scope() as s:
        s.add(Trade(id=tid, symbol=symbol, side="LONG", strategy="vwap_reclaim", kind="TECHNICAL", timeframe="SWING",
                    broker="ibkr-paper", status="CLOSED" if exited else "OPEN", quantity=10, entry_price=100.0,
                    entry_time=entered, exit_time=exited, exit_price=104.0 if exited else None, exit_reason=reason,
                    stop_price=stop, initial_stop_price=initial_stop, spread_bps=4.0,
                    **{k: v for k, v in slip.items() if measured and (exited or k in ENTRY)}))


def _rows():
    from autotradebot.persistence.db import session_scope
    from autotradebot.persistence.models_orm import Trade

    with session_scope() as s:
        return {t.id: {c: getattr(t, c) for c in ("exit_reason", "spread_bps") + ENTRY + EXIT}
                for t in s.query(Trade).all()}


def test_a_dry_run_says_what_it_would_change_by_trade_id_and_changes_nothing_until_apply(db, monkeypatch, capsys):
    before, after = dt.datetime(2026, 9, 21, 14, 0), dt.datetime(2026, 9, 24, 14, 0)       # UTC, as stored
    _trade("T01", "AAA", before, before + dt.timedelta(hours=2), "trailing-stop", stop=95.004)   # its stop never moved
    _trade("T02", "BBB", before, before + dt.timedelta(hours=2), "trailing-stop", stop=97.0)     # it did: kept
    _trade("T03", "CCC", before, after, "stop")                     # in before real-time prices, out after
    _trade("T04", "DDD", after, after + dt.timedelta(hours=1), "target")                  # all on real-time prices
    _trade("T05", "EEE", before, stop=95.0)                         # still open, entered on delayed quotes
    _trade("T06", "FFF", before, before + dt.timedelta(hours=1), "stop", measured=False)  # nothing to clear
    mod = _script(monkeypatch)
    untouched = _rows()

    monkeypatch.setattr(sys, "argv", ["repair_records.py"])
    assert mod.main() == 0
    said = capsys.readouterr().out
    assert _rows() == untouched                                     # a dry run
    lines = [line.split()[0] for line in said.splitlines() if line.startswith("  ")]
    assert lines == ["T01", "T01", "T02", "T03", "T05"]
    assert "T01  exit reason trailing-stop -> stop" in said and "T03  entry slippage cleared" in said
    assert "T01  entry and exit slippage cleared" in said and "would change: 1 never-moved stop(s)" in said
    assert not any(sym in said for sym in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF"))      # trade ids only

    monkeypatch.setattr(sys, "argv", ["repair_records.py", "--apply"])
    mod.main()
    got = _rows()
    assert (got["T01"]["exit_reason"], got["T02"]["exit_reason"]) == ("stop", "trailing-stop")
    for tid in ("T01", "T02", "T05"):
        assert all(got[tid][f] is None for f in ENTRY + EXIT), tid
    assert all(got["T03"][f] is None for f in ENTRY)
    assert (got["T03"]["exit_slippage_bps"], got["T03"]["exit_decision_price"]) == (6.0, 104.0)   # out on live prices
    assert got["T04"] == untouched["T04"] and got["T06"] == untouched["T06"]
    assert {r["spread_bps"] for r in got.values()} == {4.0}         # the quote's spread is kept
    capsys.readouterr()
    mod.main()
    assert "changed: 0 never-moved stop(s) relabelled 'stop', entry slippage cleared on 0" in capsys.readouterr().out
