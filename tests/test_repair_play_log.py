"""scripts/repair_play_log.py: the sent plays that never filled, from before the app saved what became of them."""

from __future__ import annotations

import datetime as dt
import importlib.util
import os
import pathlib
import sys

import pytest

from tos_bot.core.enums import Side, StrategyKind, Timeframe
from tos_bot.core.models import Play
from tos_bot.util import clock

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "repair_play_log.py"


@pytest.fixture
def db(tmp_path):
    """A database of its own - the script looks at every row."""
    from tos_bot.persistence.db import DB
    from tos_bot.persistence.repository import Repository

    DB.init(url=f"sqlite:///{(tmp_path / 'repair.sqlite').as_posix()}")
    DB.create_all()
    yield Repository()
    DB.engine.dispose()
    DB.init(url=os.environ["DATABASE_URL"])


def _script(monkeypatch):
    spec = importlib.util.spec_from_file_location("repair_play_log", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "init_db", lambda: None)            # stay on the test's database
    return mod


def _sent(repo, symbol, sent_at=None, tif="DAY"):
    """A play Autopilot sent an entry for, saved the way the app saved it before - SUBMITTED."""
    from sqlalchemy import update

    from tos_bot.persistence.db import session_scope
    from tos_bot.persistence.models_orm import OrderAudit

    p = Play(symbol=symbol, side=Side.LONG, strategy="vwap_reclaim", kind=StrategyKind.TECHNICAL,
             timeframe=Timeframe.INTRADAY, entry=100.0, stop=98.0, targets=[104.0])
    repo.record_play(p)
    repo.set_play_status(p.id, "SUBMITTED", "autopilot")
    if sent_at is not None:
        repo.record_order_audit("PLACE", {"symbol": symbol, "tif": tif, "is_entry": True, "tag": p.id}, {}, True,
                                "ibkr", play_id=p.id)
        with session_scope() as s:
            s.execute(update(OrderAudit).where(OrderAudit.play_id == p.id).values(ts=sent_at))
    return p


def test_only_the_entries_whose_day_is_over_and_that_bought_nothing_are_marked(db, monkeypatch):
    monkeypatch.setattr(clock, "now_ny", lambda: dt.datetime(2026, 9, 22, 11, 0, tzinfo=clock.NY))
    monday, tuesday = dt.datetime(2026, 9, 21, 14, 10), dt.datetime(2026, 9, 22, 14, 5)      # UTC, as stored
    first, second = _sent(db, "AAA", monday), _sent(db, "BBB", monday)
    working = _sent(db, "CCC", tuesday)                           # today's DAY order may still be working
    resting = _sent(db, "DDD", monday, tif="GTC")                 # a good-till-cancelled one may too
    never = _sent(db, "EEE")                                      # no order ever went out
    traded = _sent(db, "FFF", monday)
    db.open_trade(traded, 100.0, 5, "ibkr")
    mod = _script(monkeypatch)

    monkeypatch.setattr(sys, "argv", ["repair_play_log.py"])
    assert mod.main() == 0
    assert {db.get_play(p.id)["status"] for p in (first, second, working, resting, never)} == {"SUBMITTED"}  # a dry run

    monkeypatch.setattr(sys, "argv", ["repair_play_log.py", "--apply"])
    mod.main()
    got = {p.symbol: (db.get_play(p.id)["status"], db.get_play(p.id)["decided_by"])
           for p in (first, second, working, resting, never, traded)}
    assert got == {"AAA": ("CANCELED", "autopilot"), "BBB": ("CANCELED", "autopilot"),
                   "CCC": ("SUBMITTED", "autopilot"), "DDD": ("SUBMITTED", "autopilot"),
                   "EEE": ("SUBMITTED", "autopilot"), "FFF": ("FILLED", "autopilot")}
    assert "never filled" in db.get_play(first.id)["evidence"]["entry_outcome"]["reason"]
