"""A fully working simulated broker.

Enough fidelity to exercise the whole pipeline offline: cash + margin book,
market / limit / stop orders, attached OCO brackets, slippage, commissions,
realised-P&L accounting, and day-trade (round-trip) detection so the PDT guard
has something real to count.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from ..config import PROJECT_ROOT
from ..core.enums import AssetClass, OrderType, Side, TimeInForce
from ..core.models import Account, Fill, OrderRequest, OrderResult, Position, Quote
from ..data.market_data import MarketDataService, SyntheticProvider
from ..util import clock
from .base import BrokerAdapter, OrderRejected

log = logging.getLogger(__name__)

_STATE_PATH = PROJECT_ROOT / "data" / "paper_state.json"


class _Order:
    def __init__(self, req: OrderRequest) -> None:
        self.id = f"paper_{uuid.uuid4().hex[:10]}"
        self.req = req
        self.status = "WORKING"
        self.filled_qty = 0.0
        self.avg_price = 0.0
        self.fills: List[Fill] = []
        self.created = clock.now_ny()
        self.children: List["_Order"] = []       # bracket TP / SL
        self.parent_id: Optional[str] = None
        self.oco_group: Optional[str] = None

    def result(self) -> OrderResult:
        return OrderResult(
            order_id=self.id, status=self.status, symbol=self.req.symbol,
            submitted_qty=self.req.quantity, filled_qty=self.filled_qty,
            avg_fill_price=self.avg_price, fills=list(self.fills),
            raw={"parent_id": self.parent_id, "oco_group": self.oco_group},
        )


class PaperBroker(BrokerAdapter):
    name = "paper"
    paper = True
    supports_shorting = True
    supports_fractional = True
    supports_bracket_native = True

    def __init__(
        self,
        starting_cash: float = 100000.0,
        data_service: Optional[MarketDataService] = None,
        slippage_bps: float = 2.0,
        commission_per_share: float = 0.0,
        commission_min: float = 0.0,
        margin_multiplier: float = 2.0,
        always_fill_marketable: bool = True,
        persist: bool = True,
        state_path: Optional[Path] = None,
    ) -> None:
        self._cash = float(starting_cash)
        self._start_equity = float(starting_cash)
        self.data = data_service or MarketDataService(providers=[SyntheticProvider()])
        self.slippage_bps = slippage_bps
        self.commission_per_share = commission_per_share
        self.commission_min = commission_min
        self.margin_multiplier = margin_multiplier
        self.always_fill_marketable = always_fill_marketable
        self.persist = persist
        self.state_path = state_path or _STATE_PATH

        self._positions: Dict[str, Position] = {}
        self._orders: Dict[str, _Order] = {}
        self._realized_pl = 0.0
        self._round_trips: List[dt.date] = []        # one entry per day trade
        self._opened_today: Dict[str, dt.date] = {}  # symbol -> session it was opened
        self._connected = False
        self._lock = threading.RLock()

    # -- connection -------------------------------------------------- #
    def connect(self) -> None:
        if self.persist:
            self._load_state()
        self._connected = True
        log.info("paper broker ready - cash $%.2f, equity $%.2f, %d position(s)",
                 self._cash, self.get_account().equity, len(self._positions))

    # -- persistence ------------------------------------------------- #
    def _load_state(self) -> None:
        try:
            if not self.state_path.exists():
                return
            s = json.loads(self.state_path.read_text())
        except Exception as e:  # noqa: BLE001
            log.warning("could not read paper state (%s) - starting fresh", e)
            return
        self._cash = float(s.get("cash", self._cash))
        self._start_equity = float(s.get("start_equity", self._start_equity))
        self._realized_pl = float(s.get("realized_pl", 0.0))
        self._round_trips = [dt.date.fromisoformat(d) for d in s.get("round_trips", [])]
        self._opened_today = {k: dt.date.fromisoformat(v)
                              for k, v in (s.get("opened_today", {}) or {}).items()}
        self._positions = {}
        for p in s.get("positions", []):
            self._positions[p["symbol"]] = Position(
                symbol=p["symbol"], quantity=float(p["quantity"]),
                avg_price=float(p["avg_price"]),
                asset_class=AssetClass(p.get("asset_class", "EQUITY")),
            )

    def _save_state(self) -> None:
        if not self.persist:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps({
                "cash": round(self._cash, 6),
                "start_equity": round(self._start_equity, 2),
                "realized_pl": round(self._realized_pl, 6),
                "round_trips": [d.isoformat() for d in self._round_trips],
                "opened_today": {k: v.isoformat() for k, v in self._opened_today.items()},
                "positions": [
                    {"symbol": p.symbol, "quantity": p.quantity,
                     "avg_price": p.avg_price, "asset_class": p.asset_class.value}
                    for p in self._positions.values() if abs(p.quantity) > 1e-9
                ],
                "updated": clock.now_ny().isoformat(),
            }, indent=2))
        except Exception:  # noqa: BLE001
            log.debug("paper state save failed", exc_info=True)

    @property
    def is_connected(self) -> bool:
        return self._connected

    # -- market data (delegated) --------------------------------- #
    def get_quote(self, symbol: str) -> Quote:
        return self.data.get_quote(symbol)

    def get_price_history(
        self, symbol: str, interval: str = "5m", lookback_days: int = 10,
        start=None, end=None, extended_hours: bool = False,
    ) -> pd.DataFrame:
        return self.data.get_price_history(symbol, interval, lookback_days, extended_hours)

    # -- account ------------------------------------------------- #
    def get_account(self) -> Account:
        with self._lock:
            positions: List[Position] = []
            long_mv = short_mv = 0.0
            for pos in self._positions.values():
                if abs(pos.quantity) < 1e-9:
                    continue
                try:
                    px = self.data.get_quote(pos.symbol).last
                except Exception:  # noqa: BLE001
                    px = pos.avg_price
                pos.market_price = px
                positions.append(pos)
                if pos.quantity > 0:
                    long_mv += pos.quantity * px
                else:
                    short_mv += abs(pos.quantity) * px

            equity = self._cash + long_mv - short_mv
            gross = long_mv + short_mv
            buying_power = max(0.0, equity * self.margin_multiplier - gross)
            return Account(
                account_id="PAPER-0001",
                equity=round(equity, 2),
                cash=round(self._cash, 2),
                buying_power=round(buying_power, 2),
                day_trade_buying_power=round(max(0.0, equity - 2000.0) * 4.0, 2),
                is_cash_account=False,
                round_trips=self.day_trades_in_last_5_sessions(),
                positions=positions,
                raw={"realized_pl": round(self._realized_pl, 2),
                     "start_equity": self._start_equity},
            )

    def day_trades_in_last_5_sessions(self) -> int:
        window = set(clock.last_n_sessions(clock.session_date(), 5))
        return sum(1 for d in self._round_trips if d in window)

    # -- orders ------------------------------------------------ #
    def place_order(self, req: OrderRequest) -> OrderResult:
        if not self._connected:
            raise OrderRejected("broker not connected")
        if req.quantity <= 0:
            raise OrderRejected("quantity must be > 0")
        with self._lock:
            order = _Order(req)
            self._orders[order.id] = order
            self._maybe_fill(order)
            if order.status == "FILLED" and req.is_entry and (
                req.take_profit or req.stop_loss
            ):
                self._attach_bracket(order)
            return order.result()

    def place_bracket(
        self, entry: OrderRequest, take_profit: Optional[float],
        stop_loss: Optional[float],
    ) -> OrderResult:
        entry.take_profit = take_profit
        entry.stop_loss = stop_loss
        return self.place_order(entry)

    def _attach_bracket(self, parent: _Order) -> None:
        req = parent.req
        exit_side = Side.SHORT if req.side is Side.LONG else Side.LONG
        grp = f"oco_{uuid.uuid4().hex[:8]}"
        for price, otype, tag in (
            (req.take_profit, OrderType.LIMIT, "TP"),
            (req.stop_loss, OrderType.STOP, "SL"),
        ):
            if not price:
                continue
            child_req = OrderRequest(
                symbol=req.symbol, side=exit_side, quantity=parent.filled_qty,
                order_type=otype,
                limit_price=price if otype is OrderType.LIMIT else None,
                stop_price=price if otype is OrderType.STOP else None,
                tif=TimeInForce.GTC, asset_class=req.asset_class, is_entry=False,
                client_tag=f"{req.client_tag}:{tag}",
            )
            child = _Order(child_req)
            child.parent_id = parent.id
            child.oco_group = grp
            self._orders[child.id] = child
            parent.children.append(child)
        log.info("bracket attached to %s: TP=%s SL=%s", parent.id,
                 req.take_profit, req.stop_loss)

    # -- fill engine ---------------------------------------- #
    def _ref_price(self, symbol: str) -> Quote:
        return self.data.get_quote(symbol)

    def _slip(self, price: float, side: Side) -> float:
        d = price * self.slippage_bps / 1e4
        return price + d if side is Side.LONG else price - d

    def _commission(self, qty: float) -> float:
        return max(self.commission_min, abs(qty) * self.commission_per_share)

    def _maybe_fill(self, order: _Order) -> None:
        req = order.req
        q = self._ref_price(req.symbol)
        px: Optional[float] = None
        if req.order_type is OrderType.MARKET:
            px = q.ask if req.side is Side.LONG else q.bid
        elif req.order_type is OrderType.LIMIT:
            if req.side is Side.LONG and q.ask <= (req.limit_price or 0):
                px = min(req.limit_price, q.ask)
            elif req.side is Side.SHORT and q.bid >= (req.limit_price or 1e18):
                px = max(req.limit_price, q.bid)
            elif self.always_fill_marketable and req.limit_price:
                # treat a resting limit near the market as marketable for the sim
                if (req.side is Side.LONG and req.limit_price >= q.bid) or (
                    req.side is Side.SHORT and req.limit_price <= q.ask
                ):
                    px = req.limit_price
        elif req.order_type is OrderType.STOP:
            trig = req.stop_price or 0.0
            if (req.side is Side.LONG and q.last >= trig) or (
                req.side is Side.SHORT and q.last <= trig
            ):
                px = self._slip(q.last, req.side)
        elif req.order_type is OrderType.STOP_LIMIT:
            trig = req.stop_price or 0.0
            if (req.side is Side.LONG and q.last >= trig) or (
                req.side is Side.SHORT and q.last <= trig
            ):
                px = req.limit_price or q.last

        if px is None:
            order.status = "WORKING"
            return
        self._execute(order, self._slip(px, req.side), req.quantity)

    def _execute(self, order: _Order, price: float, qty: float) -> None:
        req = order.req
        signed = qty if req.side is Side.LONG else -qty
        comm = self._commission(qty)
        pos = self._positions.get(req.symbol) or Position(
            symbol=req.symbol, quantity=0.0, avg_price=0.0,
            asset_class=req.asset_class,
        )

        prev_qty = pos.quantity
        # realised P&L when reducing / flipping
        if prev_qty != 0 and (prev_qty > 0) != (signed > 0):
            closed = min(abs(signed), abs(prev_qty))
            direction = 1 if prev_qty > 0 else -1
            self._realized_pl += direction * (price - pos.avg_price) * closed - comm
            sess = clock.session_date()
            if self._opened_today.get(req.symbol) == sess:
                self._round_trips.append(sess)     # entered & exited same session
        new_qty = prev_qty + signed
        if (prev_qty >= 0 and signed > 0) or (prev_qty <= 0 and signed < 0):
            # adding to / opening a position -> weighted average
            denom = abs(prev_qty) + abs(signed)
            pos.avg_price = (abs(prev_qty) * pos.avg_price + abs(signed) * price) / denom
            if prev_qty == 0:
                self._opened_today[req.symbol] = clock.session_date()
        elif abs(new_qty) > 1e-9 and (new_qty > 0) == (prev_qty > 0):
            pass  # partial reduction, avg unchanged
        else:
            pos.avg_price = price if abs(new_qty) > 1e-9 else 0.0
            if abs(new_qty) > 1e-9:
                self._opened_today[req.symbol] = clock.session_date()

        pos.quantity = new_qty
        pos.market_price = price
        self._positions[req.symbol] = pos
        self._cash -= signed * price + comm

        order.filled_qty = qty
        order.avg_price = price
        order.status = "FILLED"
        order.fills.append(
            Fill(order_id=order.id, symbol=req.symbol, side=req.side,
                 quantity=qty, price=price, commission=comm)
        )
        log.info("FILL %s %s %s @ %.4f (cash $%.2f, realised $%.2f)",
                 req.side.value, qty, req.symbol, price, self._cash, self._realized_pl)
        self._save_state()

    # -- polling (call every few seconds from the engine) ------ #
    def poll(self) -> List[OrderResult]:
        changed: List[OrderResult] = []
        with self._lock:
            for order in list(self._orders.values()):
                if order.status != "WORKING":
                    continue
                # OCO: if a sibling filled, cancel this one
                if order.oco_group and any(
                    o.oco_group == order.oco_group and o.status == "FILLED"
                    for o in self._orders.values()
                ):
                    order.status = "CANCELED"
                    changed.append(order.result())
                    continue
                before = order.status
                self._maybe_fill(order)
                if order.status != before:
                    changed.append(order.result())
        return changed

    def cancel_order(self, order_id: str) -> None:
        with self._lock:
            o = self._orders.get(order_id)
            if o and o.status in ("WORKING", "PARTIAL"):
                o.status = "CANCELED"
                for c in o.children:
                    if c.status == "WORKING":
                        c.status = "CANCELED"

    def get_order(self, order_id: str) -> OrderResult:
        o = self._orders.get(order_id)
        if not o:
            raise OrderRejected(f"unknown order {order_id}")
        return o.result()

    def list_orders(self, status: Optional[str] = None) -> List[OrderResult]:
        with self._lock:
            return [o.result() for o in self._orders.values()
                    if status is None or o.status == status]

    # -- test / demo helpers ---------------------------------- #
    def reset(self, cash: Optional[float] = None) -> None:
        with self._lock:
            self._cash = float(cash if cash is not None else self._start_equity)
            self._start_equity = self._cash
            self._positions.clear()
            self._orders.clear()
            self._realized_pl = 0.0
            self._round_trips.clear()
            self._opened_today.clear()
            self._save_state()
