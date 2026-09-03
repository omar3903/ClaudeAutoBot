"""Plain in-memory data structures passed between engine layers.

These are deliberately framework-free (`dataclass`, not ORM, not pydantic) so
brokers, strategies and the scanner never import the database or the web layer.
Persistence maps them to SQLAlchemy rows in :mod:`tos_bot.persistence`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .enums import (
    AssetClass,
    OrderType,
    PlayStatus,
    Side,
    StrategyKind,
    Timeframe,
    TimeInForce,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------- #
#  Market data                                                               #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    last: float
    volume: float = 0.0
    ts: datetime = field(default_factory=_utcnow)

    @property
    def mid(self) -> float:
        if self.bid and self.ask:
            return (self.bid + self.ask) / 2.0
        return self.last

    @property
    def spread_bps(self) -> float:
        if self.bid and self.ask and self.mid:
            return (self.ask - self.bid) / self.mid * 1e4
        return 0.0


@dataclass(frozen=True)
class Instrument:
    symbol: str
    asset_class: AssetClass = AssetClass.EQUITY
    name: str = ""
    exchange: str = ""
    cusip: str = ""
    tick_size: float = 0.01
    lot_size: float = 1.0


# --------------------------------------------------------------------------- #
#  Account / positions                                                       #
# --------------------------------------------------------------------------- #
@dataclass
class Position:
    symbol: str
    quantity: float                     # signed: negative = short
    avg_price: float
    market_price: float = 0.0
    asset_class: AssetClass = AssetClass.EQUITY

    @property
    def side(self) -> Side:
        return Side.LONG if self.quantity >= 0 else Side.SHORT

    @property
    def market_value(self) -> float:
        return self.quantity * self.market_price

    @property
    def unrealized_pl(self) -> float:
        return (self.market_price - self.avg_price) * self.quantity


@dataclass
class Account:
    account_id: str
    equity: float = 0.0                  # net liquidation value
    cash: float = 0.0
    buying_power: float = 0.0
    day_trade_buying_power: float = 0.0
    is_cash_account: bool = False
    round_trips: int = 0                 # broker-reported day trades in last 5 sessions
    positions: List[Position] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=_utcnow)

    def position(self, symbol: str) -> Optional[Position]:
        for p in self.positions:
            if p.symbol == symbol:
                return p
        return None


# --------------------------------------------------------------------------- #
#  Orders                                                                    #
# --------------------------------------------------------------------------- #
@dataclass
class OrderRequest:
    symbol: str
    side: Side
    quantity: float
    order_type: OrderType = OrderType.LIMIT
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    tif: TimeInForce = TimeInForce.DAY
    asset_class: AssetClass = AssetClass.EQUITY
    is_entry: bool = True                 # entry vs. exit leg
    #: "REGULAR" (RTH only) | "EXTENDED" (pre/post, limit-only) | "SEAMLESS" (both)
    session: str = "REGULAR"
    # Optional bracket children (attached after entry fills, or as OCO)
    take_profit: Optional[float] = None
    stop_loss: Optional[float] = None
    client_tag: str = ""


@dataclass
class Fill:
    order_id: str
    symbol: str
    side: Side
    quantity: float
    price: float
    ts: datetime = field(default_factory=_utcnow)
    commission: float = 0.0


@dataclass
class OrderResult:
    order_id: str
    status: str
    symbol: str
    submitted_qty: float
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    fills: List[Fill] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)
    message: str = ""


# --------------------------------------------------------------------------- #
#  Scanner + Play                                                            #
# --------------------------------------------------------------------------- #
@dataclass
class ScanCandidate:
    """A symbol that survived the pre-filter and is worth running strategies on."""

    symbol: str
    price: float
    dollar_volume: float
    atr_pct: float
    rvol: float                          # today's volume vs. 20-day average, so far
    gap_pct: float
    change_pct: float
    spread_bps: float = 0.0
    notes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Play:
    """A single actionable idea shown in the dashboard.

    ``explanation`` is what the hover pop-up renders; ``rationale`` is the
    one-liner in the table; ``evidence`` holds the raw numbers behind it.
    """

    symbol: str
    side: Side
    strategy: str                        # registry key, e.g. "vwap_reclaim"
    kind: StrategyKind
    timeframe: Timeframe
    entry: float
    stop: float
    targets: List[float] = field(default_factory=list)
    confidence: float = 0.5              # 0..1 from the strategy itself
    score: float = 0.0                   # blended rank score assigned by the scanner
    rationale: str = ""
    explanation: str = ""
    #: the price/behaviour that voids the idea - "if it gets here, the edge is gone"
    invalidation: str = ""
    #: rough probability this edge resolves in our favour (0..1), NOT a promise
    probability: float = 0.5
    evidence: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    asset_class: AssetClass = AssetClass.EQUITY
    sector: str = ""                     # GICS sector of the underlying
    #: may this idea be entered in the pre / post-market session too?
    extended_hours_ok: bool = False
    #: expected time to exit (minutes if intraday, trading days if swing)
    expected_hold_typical: float = 0.0
    expected_hold_max: float = 0.0

    # sizing hints (filled by the risk module)
    suggested_qty: int = 0
    risk_per_share: float = 0.0
    dollar_risk: float = 0.0
    notional: float = 0.0

    # lifecycle
    id: str = field(default_factory=lambda: _new_id("play"))
    status: PlayStatus = PlayStatus.PROPOSED
    created_at: datetime = field(default_factory=_utcnow)
    expires_at: Optional[datetime] = None
    scan_run_id: Optional[str] = None
    trade_id: Optional[str] = None          # set once this play becomes a trade

    # ---- convenience ---------------------------------------------------- #
    @property
    def primary_target(self) -> Optional[float]:
        return self.targets[0] if self.targets else None

    @property
    def reward_risk(self) -> float:
        if not self.targets or self.stop == self.entry:
            return 0.0
        reward = abs(self.targets[0] - self.entry)
        risk = abs(self.entry - self.stop)
        return reward / risk if risk else 0.0

    @property
    def is_day_trade(self) -> bool:
        return self.timeframe is Timeframe.INTRADAY

    def to_row(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "sector": self.sector,
            "side": self.side.value,
            "strategy": self.strategy,
            "kind": self.kind.value,
            "timeframe": self.timeframe.value,
            "entry": round(self.entry, 4),
            "stop": round(self.stop, 4),
            "targets": [round(t, 4) for t in self.targets],
            "reward_risk": round(self.reward_risk, 2),
            "confidence": round(self.confidence, 3),
            "score": round(self.score, 3),
            "rationale": self.rationale,
            "explanation": self.explanation,
            "invalidation": self.invalidation,
            "probability": round(self.probability, 3),
            "evidence": self.evidence,
            "tags": self.tags,
            "suggested_qty": self.suggested_qty,
            "dollar_risk": round(self.dollar_risk, 2),
            "notional": round(self.notional, 2),
            "extended_hours_ok": self.extended_hours_ok,
            "expected_hold_typical": self.expected_hold_typical,
            "expected_hold_max": self.expected_hold_max,
            "expected_hold_unit": "min" if self.timeframe is Timeframe.INTRADAY else "d",
            "status": self.status.value,
            "trade_id": self.trade_id,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }
