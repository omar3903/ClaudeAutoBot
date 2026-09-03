"""SQLAlchemy 2.0 ORM.

Tables
------
scan_runs          one row per scanner cycle
play_logs          EVERY proposed play (accepted or not) - the audit of ideas
trades             an executed position's full lifecycle + realised P/L
fills              individual executions attached to a trade
account_snapshots  periodic equity / cash / buying-power / day-trade count
order_audit        raw order request + broker response
token_audit        every token refresh / rotation / re-auth event
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

MONEY = sa.Numeric(20, 6)


class Base(DeclarativeBase):
    pass


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    started_at: Mapped[dt.datetime] = mapped_column(sa.DateTime, default=_utcnow, index=True)
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(sa.DateTime, nullable=True)
    universe_size: Mapped[int] = mapped_column(sa.Integer, default=0)
    scanned: Mapped[int] = mapped_column(sa.Integer, default=0)
    prefiltered: Mapped[int] = mapped_column(sa.Integer, default=0)
    n_plays: Mapped[int] = mapped_column(sa.Integer, default=0)
    shortlist: Mapped[Optional[dict]] = mapped_column(sa.JSON, nullable=True)
    n_errors: Mapped[int] = mapped_column(sa.Integer, default=0)
    elapsed_s: Mapped[float] = mapped_column(sa.Float, default=0.0)


class PlayLog(Base):
    __tablename__ = "play_logs"

    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    scan_run_id: Mapped[Optional[str]] = mapped_column(
        sa.String(32), sa.ForeignKey("scan_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(sa.DateTime, default=_utcnow, index=True)
    symbol: Mapped[str] = mapped_column(sa.String(16), index=True)
    side: Mapped[str] = mapped_column(sa.String(8))
    strategy: Mapped[str] = mapped_column(sa.String(48), index=True)
    kind: Mapped[str] = mapped_column(sa.String(16))
    timeframe: Mapped[str] = mapped_column(sa.String(16))
    entry: Mapped[float] = mapped_column(MONEY)
    stop: Mapped[float] = mapped_column(MONEY)
    targets: Mapped[Optional[dict]] = mapped_column(sa.JSON, nullable=True)
    reward_risk: Mapped[float] = mapped_column(sa.Float, default=0.0)
    confidence: Mapped[float] = mapped_column(sa.Float, default=0.0)
    score: Mapped[float] = mapped_column(sa.Float, default=0.0, index=True)
    suggested_qty: Mapped[int] = mapped_column(sa.Integer, default=0)
    dollar_risk: Mapped[float] = mapped_column(MONEY, default=0)
    notional: Mapped[float] = mapped_column(MONEY, default=0)
    rationale: Mapped[str] = mapped_column(sa.String(400), default="")
    explanation: Mapped[str] = mapped_column(sa.Text, default="")
    evidence: Mapped[Optional[dict]] = mapped_column(sa.JSON, nullable=True)
    tags: Mapped[Optional[dict]] = mapped_column(sa.JSON, nullable=True)
    status: Mapped[str] = mapped_column(sa.String(16), default="PROPOSED", index=True)
    decided_at: Mapped[Optional[dt.datetime]] = mapped_column(sa.DateTime, nullable=True)
    decided_by: Mapped[str] = mapped_column(sa.String(32), default="")

    trade: Mapped[Optional["Trade"]] = relationship(back_populates="play", uselist=False)


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    play_id: Mapped[Optional[str]] = mapped_column(
        sa.String(32), sa.ForeignKey("play_logs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    symbol: Mapped[str] = mapped_column(sa.String(16), index=True)
    side: Mapped[str] = mapped_column(sa.String(8))
    strategy: Mapped[str] = mapped_column(sa.String(48), index=True)
    kind: Mapped[str] = mapped_column(sa.String(16))
    timeframe: Mapped[str] = mapped_column(sa.String(16))
    broker: Mapped[str] = mapped_column(sa.String(16), default="paper")

    status: Mapped[str] = mapped_column(sa.String(12), default="OPEN", index=True)  # OPEN / CLOSED
    quantity: Mapped[float] = mapped_column(MONEY, default=0)
    entry_price: Mapped[float] = mapped_column(MONEY, default=0)
    entry_time: Mapped[Optional[dt.datetime]] = mapped_column(sa.DateTime, index=True)
    order_type: Mapped[str] = mapped_column(sa.String(16), default="LIMIT")
    order_session: Mapped[str] = mapped_column(sa.String(12), default="REGULAR")  # REGULAR / EXTENDED
    #: working protective levels - the exit manager moves these
    stop_price: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)
    target_price: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)
    #: the levels the play was entered with - never mutated (basis for R math)
    initial_stop_price: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)
    initial_target_price: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)
    hwm_price: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)  # favourable extreme
    managed_exit: Mapped[bool] = mapped_column(sa.Boolean, default=True)     # auto exit manager on?

    exit_price: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)
    exit_time: Mapped[Optional[dt.datetime]] = mapped_column(sa.DateTime, nullable=True, index=True)
    exit_reason: Mapped[str] = mapped_column(sa.String(24), default="")

    fees: Mapped[float] = mapped_column(MONEY, default=0)
    realized_pl: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)
    realized_pl_pct: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    r_multiple: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    mae: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)   # max adverse excursion
    mfe: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)   # max favourable excursion
    is_day_trade: Mapped[bool] = mapped_column(sa.Boolean, default=False, index=True)
    session_date: Mapped[Optional[dt.date]] = mapped_column(sa.Date, nullable=True, index=True)

    notes: Mapped[str] = mapped_column(sa.Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(sa.DateTime, default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(sa.DateTime, default=_utcnow, onupdate=_utcnow)

    play: Mapped[Optional[PlayLog]] = relationship(back_populates="trade")
    fills: Mapped[list["Fill"]] = relationship(back_populates="trade", cascade="all, delete-orphan")


class Fill(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[str] = mapped_column(
        sa.String(32), sa.ForeignKey("trades.id", ondelete="CASCADE"), index=True
    )
    broker_order_id: Mapped[str] = mapped_column(sa.String(48), default="")
    ts: Mapped[dt.datetime] = mapped_column(sa.DateTime, default=_utcnow, index=True)
    side: Mapped[str] = mapped_column(sa.String(8))
    leg: Mapped[str] = mapped_column(sa.String(8), default="ENTRY")   # ENTRY / EXIT
    quantity: Mapped[float] = mapped_column(MONEY)
    price: Mapped[float] = mapped_column(MONEY)
    commission: Mapped[float] = mapped_column(MONEY, default=0)

    trade: Mapped[Trade] = relationship(back_populates="fills")


class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(sa.DateTime, default=_utcnow, index=True)
    broker: Mapped[str] = mapped_column(sa.String(16), default="paper")
    equity: Mapped[float] = mapped_column(MONEY, default=0)
    cash: Mapped[float] = mapped_column(MONEY, default=0)
    buying_power: Mapped[float] = mapped_column(MONEY, default=0)
    day_trades_5d: Mapped[int] = mapped_column(sa.Integer, default=0)
    open_positions: Mapped[int] = mapped_column(sa.Integer, default=0)
    unrealized_pl: Mapped[float] = mapped_column(MONEY, default=0)
    realized_pl_day: Mapped[float] = mapped_column(MONEY, default=0)


class OrderAudit(Base):
    __tablename__ = "order_audit"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(sa.DateTime, default=_utcnow, index=True)
    play_id: Mapped[Optional[str]] = mapped_column(sa.String(32), nullable=True, index=True)
    trade_id: Mapped[Optional[str]] = mapped_column(sa.String(32), nullable=True, index=True)
    broker: Mapped[str] = mapped_column(sa.String(16), default="paper")
    action: Mapped[str] = mapped_column(sa.String(24), default="")     # PLACE / CANCEL / REPLACE
    request: Mapped[Optional[dict]] = mapped_column(sa.JSON, nullable=True)
    response: Mapped[Optional[dict]] = mapped_column(sa.JSON, nullable=True)
    ok: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    message: Mapped[str] = mapped_column(sa.String(400), default="")


class TokenAudit(Base):
    __tablename__ = "token_audit"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(sa.DateTime, default=_utcnow, index=True)
    broker: Mapped[str] = mapped_column(sa.String(16), default="schwab")
    event: Mapped[str] = mapped_column(sa.String(32))   # REFRESH / ROTATE / REAUTH_REQUIRED / REAUTH_OK / ERROR
    refresh_token_age_days: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    refresh_token_expires_at: Mapped[Optional[dt.datetime]] = mapped_column(sa.DateTime, nullable=True)
    detail: Mapped[str] = mapped_column(sa.String(400), default="")
