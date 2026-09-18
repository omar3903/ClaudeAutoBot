"""Every test run gets its own database, .env, data folder and runtime file, so
nothing a test does can touch yours."""

from __future__ import annotations

import os
import tempfile

import pytest

_RUN_DIR = tempfile.mkdtemp(prefix="atb-tests-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_RUN_DIR, "tests.sqlite").replace("\\", "/")
os.environ["ATB_ENV_PATH"] = os.path.join(_RUN_DIR, "tests.env")
os.environ["ATB_DATA_DIR"] = os.path.join(_RUN_DIR, "data")
os.environ["TOS_RUNTIME_PATH"] = os.path.join(_RUN_DIR, "runtime.json")
os.environ["OPEN_BROWSER_ON_START"] = "0"
os.environ["PAPER_PERSIST"] = "0"
os.environ["SIGNALS_ENABLED"] = "0"            # no test reaches SEC, IBKR news or Finnhub


@pytest.fixture(scope="session", autouse=True)
def _db():
    from tos_bot.persistence.db import DB
    DB.init(url=os.environ["DATABASE_URL"])
    DB.create_all()
    yield


@pytest.fixture(autouse=True)
def _regular_session(monkeypatch):
    """The executor refuses exits while the exchange is closed; the tests run at any hour, so for them
    the session is open unless a test says otherwise."""
    from tos_bot.execution.executor import Executor
    from tos_bot.util import clock
    monkeypatch.setattr(Executor, "_session_now", staticmethod(lambda: clock.Session.REGULAR))


@pytest.fixture
def repo():
    from tos_bot.persistence.repository import Repository
    return Repository()


@pytest.fixture
def paper():
    from fakes import fixed_quote
    from tos_bot.brokers.paper_adapter import PaperBroker
    broker = PaperBroker(quote=fixed_quote(), starting_cash=5000.0)
    broker.connect()
    return broker


@pytest.fixture
def synth_frames():
    import fakes
    intraday = fakes.intraday_bars("TESTX")
    return intraday, fakes.daily_bars("TESTX"), fakes.quote_from_price("TESTX", float(intraday["close"].iloc[-1]))
