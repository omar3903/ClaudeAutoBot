from __future__ import annotations

import os
import tempfile

import pytest

# isolate every test run's DB before tos_bot.persistence is imported anywhere
_TMPDB = os.path.join(tempfile.gettempdir(), "tos_trader_pytest.sqlite")
if os.path.exists(_TMPDB):
    os.remove(_TMPDB)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ["BROKER"] = "paper"
os.environ["OPEN_BROWSER_ON_START"] = "0"
os.environ["PAPER_PERSIST"] = "0"          # tests never touch data/paper_state.json
# tests never rewrite the user's data/runtime.json (mode + autopilot knobs)
_TMP_RUNTIME = os.path.join(tempfile.gettempdir(), "tos_trader_pytest_runtime.json")
if os.path.exists(_TMP_RUNTIME):
    os.remove(_TMP_RUNTIME)
os.environ["TOS_RUNTIME_PATH"] = _TMP_RUNTIME


@pytest.fixture(scope="session", autouse=True)
def _db():
    from tos_bot.persistence.db import DB
    DB.init(url=os.environ["DATABASE_URL"])
    DB.create_all()
    yield


@pytest.fixture
def md():
    from tos_bot.data.market_data import MarketDataService, SyntheticProvider
    return MarketDataService(providers=[SyntheticProvider(seed=99)], cache=False,
                             min_interval_between_calls=0.0)


@pytest.fixture
def paper(md):
    from tos_bot.brokers.paper_adapter import PaperBroker
    b = PaperBroker(starting_cash=5000.0, data_service=md, persist=False)
    b.connect()
    return b


@pytest.fixture
def repo():
    from tos_bot.persistence.repository import Repository
    return Repository()


@pytest.fixture
def synth_frames(md):
    intr = md.get_price_history("TESTX", "5m", 10)
    daily = md.get_price_history("TESTX", "1d", 400)
    q = md.get_quote("TESTX")
    return intr, daily, q
