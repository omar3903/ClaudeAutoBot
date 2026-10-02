"""The IB Gateway connection the app holds, and the built-in simulator.

Exactly one Gateway connection at a time: the account the switches point at,
for trading or read-only (see brokers/venues.py). It is also the price source
for everything - scans, the simulator's fills, the automatic exits - so it is
attached to :class:`MarketData` as soon as it connects.
"""

from __future__ import annotations

import contextlib
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .. import secrets_store
from ..brokers import get_broker
from ..brokers.base import BrokerAdapter, WrongAccount
from ..brokers.venues import VenuePlan
from ..config import Settings
from ..data.market_data import MarketData
from ..util.net import port_is_open
from .capital import in_account_currency, money

log = logging.getLogger(__name__)

BrokerFactory = Callable[..., BrokerAdapter]


class Connections:
    #: a separate client id, so a test never bumps the app's own connection
    PROBE_CLIENT_OFFSET = 50

    def __init__(self, settings: Settings, market_data: MarketData, *,
                 broker_factory: BrokerFactory = get_broker,
                 port_check: Callable[[str, int], bool] = port_is_open,
                 simulator_state: Optional[Path] = None) -> None:
        self.settings = settings
        self.md = market_data
        self._factory = broker_factory
        self._port_check = port_check
        self._simulator_state = simulator_state
        self._simulator: Optional[BrokerAdapter] = None
        self.ibkr: Optional[BrokerAdapter] = None
        self.plan = VenuePlan()
        self.connected_at = 0.0
        #: why the wanted connection isn't up, each with its fix
        self.blockers: List[str] = []

    # ---- IB Gateway ------------------------------------------------------ #
    @property
    def connected(self) -> bool:
        return self.ibkr is not None and self.ibkr.is_connected

    def holds(self, plan: VenuePlan) -> bool:
        """This plan's connection is held (a dropped session reconnects by itself)."""
        return self.ibkr is not None and self.plan.key == plan.key

    def port_open(self, port: int) -> bool:
        return self._port_check(self.settings.secrets.ibkr_host, port)

    def prereqs(self, plan: VenuePlan) -> List[str]:
        sec = self.settings.secrets
        if sec.ibkr_port_problem():
            return [sec.ibkr_port_problem()]
        port = sec.ibkr_port_for(plan.account)
        if self.port_open(port):
            return []
        return [f"IB Gateway / TWS isn't reachable on {sec.ibkr_host}:{port} ({plan.account} port) - "
                "start it (or IBC) with the API enabled. See Connections."]

    def ensure(self, plan: VenuePlan) -> Optional[BrokerAdapter]:
        """Hold exactly the connection ``plan`` needs; None if it can't be had."""
        self.blockers = []
        if self.holds(plan) and self.ibkr.is_connected:
            return self.ibkr
        self.close()
        self.blockers = self.prereqs(plan)
        if self.blockers:
            return None
        sec = self.settings.secrets
        try:
            broker = self._factory("ibkr", port=sec.ibkr_port_for(plan.account), mode=plan.account,
                                   readonly=(not plan.trade) or sec.ibkr_readonly)
            broker.connect()
        except Exception as e:  # noqa: BLE001
            self.blockers = [str(e)]
            log.warning("IBKR %s account unavailable: %s", plan.account, e)
            return None
        self.ibkr, self.plan, self.connected_at = broker, plan, time.monotonic()
        self.md.attach(broker)
        log.info("connected to IBKR %s (%s)", plan.account, "trading" if plan.trade else "data only")
        return broker

    def close(self) -> None:
        if self.ibkr is not None:
            self.md.detach()
            with contextlib.suppress(Exception):
                self.ibkr.close()
        self.ibkr, self.plan = None, VenuePlan()

    def refresh(self) -> None:
        """Nudge the Gateway session back up after IBKR's daily restart."""
        if self.ibkr is not None:
            with contextlib.suppress(Exception):
                self.ibkr.refresh_if_needed()

    def session_status(self) -> Optional[Dict[str, Any]]:
        if self.ibkr is None:
            return None
        try:
            return self.ibkr.session_status()
        except Exception:  # noqa: BLE001
            return None

    def probe(self, account: str) -> Dict[str, Any]:
        """Read-only check for the Connections panel. Never places an order."""
        account = "live" if account == "live" else "paper"
        sec = self.settings.secrets
        port = sec.ibkr_port_for(account)
        out: Dict[str, Any] = {"ok": False, "account_type": account, "host": sec.ibkr_host, "port": port}
        if sec.ibkr_port_problem():
            out["reason"] = sec.ibkr_port_problem()
            return out
        if not self.port_open(port):
            out["reason"] = (f"Nothing is listening on {sec.ibkr_host}:{port}. Start IB Gateway logged "
                             f"in to your {account} account, with the API enabled on that port.")
            return out
        shared = self.connected and self.plan.account == account
        broker = self.ibkr if shared else None
        try:
            if broker is None:
                broker = self._factory("ibkr", port=port, mode=account, readonly=True,
                                       client_id=int(sec.ibkr_client_id) + self.PROBE_CLIENT_OFFSET)
                broker.connect()
            acc = broker.get_account()
            data = broker.session_status()["market_data"]
            worth = in_account_currency(acc)
            masked = secrets_store.mask(acc.account_id)
            out.update(ok=True, account=masked, market_data=data, equity=round(acc.equity, 2),
                       currency=worth["currency"], account_value=worth["equity"], positions=len(acc.positions),
                       note=f"Connected to {account} account {masked}, worth "
                            f"{money(worth['equity'], worth['currency'])} - "
                            + ("real-time market data." if data == "live" else
                               "no real-time data subscription, so prices come from IBKR's delayed candles."))
        except WrongAccount as e:
            why = str(e)                       # the adapter's own words: which account is on which port
            out["reason"] = why[:1].upper() + why[1:]
        except Exception as e:  # noqa: BLE001
            out["reason"] = f"The Gateway is up but the API connection failed: {e}"
        finally:
            if broker is not None and not shared:
                with contextlib.suppress(Exception):
                    broker.close()
        return out

    # ---- the simulator ---------------------------------------------------- #
    def simulator(self) -> BrokerAdapter:
        """Fills against IBKR prices; created on first use."""
        if self._simulator is None:
            self._simulator = self._factory(
                "paper", quote=self.md.quote, state_path=self._simulator_state,
                starting_cash=self.settings.config.account.paper_start_cash)
            self._simulator.connect()
        return self._simulator

    def close_all(self) -> None:
        self.close()
        if self._simulator is not None:
            with contextlib.suppress(Exception):
                self._simulator.close()
