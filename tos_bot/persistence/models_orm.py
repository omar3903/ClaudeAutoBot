"""SQLAlchemy 2.0 ORM.

Tables
------
scan_runs          one row per scan (full scan, cycle or fast cycle)
play_logs          EVERY proposed play (accepted or not) - the audit of ideas
trades             an executed position's full lifecycle + realised P/L
fills              individual executions attached to a trade
account_snapshots  periodic equity / cash / buying-power / day-trade count
order_audit        raw order request + broker response
insider_trades     open-market insider purchases and sales, from SEC Form 4 filings
filings_read       the SEC filings already read, so none is fetched twice
news_items         company headlines and 8-K filings, with their sentiment
daily_reviews      the 16:15 review of each session
pair_trades        a pair trade's two legs and its z-score model
sim_trades         the replay's simulated trades, with the features at the signal (research/dataset.py)
shadow_trades      the plays shown and not taken, followed to their outcome by the review
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
    kind: Mapped[str] = mapped_column(sa.String(8), default="cycle")        # full / cycle / fast
    started_at: Mapped[dt.datetime] = mapped_column(sa.DateTime, default=_utcnow, index=True)
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(sa.DateTime, nullable=True)
    universe_size: Mapped[int] = mapped_column(sa.Integer, default=0)
    scanned: Mapped[int] = mapped_column(sa.Integer, default=0)
    # the column names predate the hot list; existing databases keep them
    liquid: Mapped[int] = mapped_column("prefiltered", sa.Integer, default=0)
    n_plays: Mapped[int] = mapped_column(sa.Integer, default=0)
    hot: Mapped[Optional[dict]] = mapped_column("shortlist", sa.JSON, nullable=True)
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
    sector: Mapped[str] = mapped_column(sa.String(40), default="")
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
    #: the noise flags and the scans in a row that found it, when it was recorded (see scanner/noise.py)
    noise: Mapped[Optional[list]] = mapped_column(sa.JSON, nullable=True)
    confirmations: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True, default=1)
    probability: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)   # the odds the play stated
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
    sector: Mapped[str] = mapped_column(sa.String(40), default="")
    side: Mapped[str] = mapped_column(sa.String(8))
    strategy: Mapped[str] = mapped_column(sa.String(48), index=True)
    kind: Mapped[str] = mapped_column(sa.String(16))
    timeframe: Mapped[str] = mapped_column(sa.String(16))
    broker: Mapped[str] = mapped_column(sa.String(16), default="paper")

    status: Mapped[str] = mapped_column(sa.String(12), default="OPEN", index=True)  # OPEN / CLOSED
    quantity: Mapped[float] = mapped_column(MONEY, default=0)
    #: the shares entered with - quantity shrinks as part of the position is taken off (the scale-out)
    initial_quantity: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)
    #: realized so far on the part taken off; joins the final P/L when the rest closes
    banked_pl: Mapped[float] = mapped_column(MONEY, default=0)
    entry_price: Mapped[float] = mapped_column(MONEY, default=0)
    entry_time: Mapped[Optional[dt.datetime]] = mapped_column(sa.DateTime, index=True)
    order_type: Mapped[str] = mapped_column(sa.String(16), default="LIMIT")
    order_session: Mapped[str] = mapped_column(sa.String(12), default="REGULAR")  # REGULAR / EXTENDED
    #: when the entry order was sent (the fill is entry_time); what the play looked like at the fill
    #: (research/features.py) - the row a model learns from, next to the outcome below
    submitted_at: Mapped[Optional[dt.datetime]] = mapped_column(sa.DateTime, nullable=True)
    entry_context: Mapped[Optional[dict]] = mapped_column(sa.JSON, nullable=True)
    #: seconds from the order going out to the fill coming back - the entry's, and the app's own exit's.
    #: A marketable order's is the broker's speed; a limit's includes the time it rested for its price
    #: (the daily review tells the two apart). A stop or target resting at the broker has none, nor an
    #: entry taken back after a restart, whose send time isn't known
    entry_latency_s: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    exit_submitted_at: Mapped[Optional[dt.datetime]] = mapped_column(sa.DateTime, nullable=True)
    exit_latency_s: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    #: working protective levels - the exit manager moves these
    stop_price: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)
    target_price: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)
    #: the levels the play was entered with - never mutated (basis for R math)
    initial_stop_price: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)
    initial_target_price: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)
    #: the play's second target: where the rest of the position goes after the scale-out at the first
    target2_price: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)
    hwm_price: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)  # favourable extreme
    managed_exit: Mapped[bool] = mapped_column(sa.Boolean, default=True)     # auto exit manager on?
    #: expected-exit overwatch (informational only - never drives the stop)
    expected_exit_at: Mapped[Optional[dt.datetime]] = mapped_column(sa.DateTime, nullable=True)
    overwatch_at: Mapped[Optional[dt.datetime]] = mapped_column(sa.DateTime, nullable=True, index=True)
    overdue_notified: Mapped[bool] = mapped_column(sa.Boolean, default=False)

    exit_price: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)
    exit_time: Mapped[Optional[dt.datetime]] = mapped_column(sa.DateTime, nullable=True, index=True)
    exit_reason: Mapped[str] = mapped_column(sa.String(24), default="")

    fees: Mapped[float] = mapped_column(MONEY, default=0)
    realized_pl: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)
    realized_pl_pct: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    r_multiple: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    mae: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)   # max adverse excursion
    mfe: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)   # max favourable excursion
    mfe_at: Mapped[Optional[dt.datetime]] = mapped_column(sa.DateTime, nullable=True)   # when the MFE was set
    # execution quality (Harris: implementation shortfall) - the fill against the quote at the decision
    decision_price: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)       # the quote's mid at the entry decision
    spread_bps: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)           # the quoted spread then
    entry_slippage_bps: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)   # paid at the entry (+ = worse than the mid)
    exit_decision_price: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)  # the price that triggered the exit
    exit_slippage_bps: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)    # paid at the exit (+ = worse)
    is_day_trade: Mapped[bool] = mapped_column(sa.Boolean, default=False, index=True)
    session_date: Mapped[Optional[dt.date]] = mapped_column(sa.Date, nullable=True, index=True)
    #: the pair trade this is one leg of (see pairs/desk.py); its exits belong to the pair desk
    pair_id: Mapped[Optional[str]] = mapped_column(sa.String(32), nullable=True, index=True)

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


class InsiderTradeLog(Base):
    __tablename__ = "insider_trades"

    accession: Mapped[str] = mapped_column(sa.String(24), primary_key=True)
    line: Mapped[int] = mapped_column(sa.Integer, primary_key=True)           # the transaction's place in the filing
    symbol: Mapped[str] = mapped_column(sa.String(16), index=True)
    issuer_cik: Mapped[int] = mapped_column(sa.BigInteger, default=0)
    issuer_name: Mapped[str] = mapped_column(sa.String(160), default="")
    owner_cik: Mapped[int] = mapped_column(sa.BigInteger, default=0)
    owner_name: Mapped[str] = mapped_column(sa.String(160), default="")
    role: Mapped[str] = mapped_column(sa.String(20), default="other")         # ceo_cfo / officer / director ...
    title: Mapped[str] = mapped_column(sa.String(160), default="")
    code: Mapped[str] = mapped_column(sa.String(2))                           # P purchase / S sale
    trade_date: Mapped[dt.date] = mapped_column(sa.Date, index=True)
    shares: Mapped[float] = mapped_column(MONEY, default=0)
    price: Mapped[float] = mapped_column(MONEY, default=0)
    shares_after: Mapped[float] = mapped_column(MONEY, default=0)
    planned: Mapped[bool] = mapped_column(sa.Boolean, default=False)          # under a 10b5-1 plan
    direct: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    offering: Mapped[bool] = mapped_column(sa.Boolean, default=False)         # bought in a placement or offering
    filed: Mapped[Optional[dt.date]] = mapped_column(sa.Date, nullable=True)


class FilingRead(Base):
    __tablename__ = "filings_read"

    accession: Mapped[str] = mapped_column(sa.String(24), primary_key=True)
    form: Mapped[str] = mapped_column(sa.String(12), default="4")
    filed: Mapped[Optional[dt.date]] = mapped_column(sa.Date, nullable=True, index=True)
    trades: Mapped[int] = mapped_column(sa.Integer, default=0)                # open-market trades found in it
    read_at: Mapped[dt.datetime] = mapped_column(sa.DateTime, default=_utcnow)


class NewsLog(Base):
    __tablename__ = "news_items"

    key: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    symbol: Mapped[str] = mapped_column(sa.String(16), index=True)
    source: Mapped[str] = mapped_column(sa.String(12))                        # ibkr / sec / finnhub
    provider: Mapped[str] = mapped_column(sa.String(40), default="")
    kind: Mapped[str] = mapped_column(sa.String(12), default="news")          # news / analyst / filing
    headline: Mapped[str] = mapped_column(sa.String(500))
    url: Mapped[str] = mapped_column(sa.String(500), default="")
    ref: Mapped[str] = mapped_column(sa.String(80), default="")
    items: Mapped[str] = mapped_column(sa.String(60), default="")              # an 8-K's item numbers
    published_at: Mapped[dt.datetime] = mapped_column(sa.DateTime, index=True)  # UTC
    sentiment: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)  # -1 .. 1
    sentiment_conf: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    fetched_at: Mapped[dt.datetime] = mapped_column(sa.DateTime, default=_utcnow)


class DailyReviewLog(Base):
    """One session's review - its trades, mistakes, the plays not taken and the lessons (research/journal.py)."""

    __tablename__ = "daily_reviews"

    session_date: Mapped[dt.date] = mapped_column(sa.Date, primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(sa.DateTime, default=_utcnow)
    trades: Mapped[int] = mapped_column(sa.Integer, default=0)
    total_r: Mapped[float] = mapped_column(sa.Float, default=0.0)
    realized_pl: Mapped[float] = mapped_column(MONEY, default=0)
    mistakes: Mapped[int] = mapped_column(sa.Integer, default=0)
    #: positions opened that session, and where the ones still open stood at the review, in R
    opened: Mapped[int] = mapped_column(sa.Integer, default=0)
    open_r: Mapped[float] = mapped_column(sa.Float, default=0.0)
    review: Mapped[Optional[dict]] = mapped_column(sa.JSON, nullable=True)


class PairTradeLog(Base):
    """A pair trade: both legs of a pair, entered and closed together (pairs/desk.py)."""

    __tablename__ = "pair_trades"

    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    pair: Mapped[str] = mapped_column(sa.String(40), index=True)                 # FIRST/SECOND
    first_symbol: Mapped[str] = mapped_column(sa.String(16))                     # bought on a long spread
    second_symbol: Mapped[str] = mapped_column(sa.String(16))
    side: Mapped[str] = mapped_column(sa.String(12))                             # LONG_SPREAD / SHORT_SPREAD
    status: Mapped[str] = mapped_column(sa.String(12), default="ENTERING", index=True)
    venue: Mapped[str] = mapped_column(sa.String(16), default="paper")
    by: Mapped[str] = mapped_column(sa.String(32), default="")
    hedge: Mapped[float] = mapped_column(sa.Float, default=0.0)
    lookback: Mapped[int] = mapped_column(sa.Integer, default=0)
    half_life: Mapped[float] = mapped_column(sa.Float, default=0.0)
    entry_z: Mapped[float] = mapped_column(sa.Float, default=0.0)               # the z-score it was entered at
    band_z: Mapped[float] = mapped_column(sa.Float, default=0.0)
    stop_z: Mapped[float] = mapped_column(sa.Float, default=0.0)
    exit_z: Mapped[float] = mapped_column(sa.Float, default=0.0)
    time_stop_days: Mapped[int] = mapped_column(sa.Integer, default=0)
    spread_sd: Mapped[float] = mapped_column(sa.Float, default=0.0)
    qty_first: Mapped[float] = mapped_column(MONEY, default=0)
    qty_second: Mapped[float] = mapped_column(MONEY, default=0)
    price_first: Mapped[float] = mapped_column(MONEY, default=0)                # at the decision
    price_second: Mapped[float] = mapped_column(MONEY, default=0)
    entry_first: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)  # fills
    entry_second: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)
    dollar_risk: Mapped[float] = mapped_column(MONEY, default=0)
    trade_first_id: Mapped[Optional[str]] = mapped_column(sa.String(32), nullable=True)
    trade_second_id: Mapped[Optional[str]] = mapped_column(sa.String(32), nullable=True)
    opened_at: Mapped[Optional[dt.datetime]] = mapped_column(sa.DateTime, nullable=True, index=True)
    closed_at: Mapped[Optional[dt.datetime]] = mapped_column(sa.DateTime, nullable=True, index=True)
    exit_reason: Mapped[str] = mapped_column(sa.String(24), default="")
    exit_z_at: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    realized_pl: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)
    r_multiple: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    model: Mapped[Optional[dict]] = mapped_column(sa.JSON, nullable=True)
    notes: Mapped[str] = mapped_column(sa.Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(sa.DateTime, default=_utcnow, index=True)


class SimTradeLog(Base):
    """A simulated trade of one replay run (research/runner.py), kept so a model can learn from
    it: the outcome next to the features the play had at the signal."""

    __tablename__ = "sim_trades"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(sa.String(32), index=True)
    ran_at: Mapped[dt.datetime] = mapped_column(sa.DateTime, index=True)
    strategy: Mapped[str] = mapped_column(sa.String(48), index=True)
    symbol: Mapped[str] = mapped_column(sa.String(16), index=True)
    side: Mapped[str] = mapped_column(sa.String(8))
    timeframe: Mapped[str] = mapped_column(sa.String(16))
    entered_at: Mapped[Optional[dt.datetime]] = mapped_column(sa.DateTime, nullable=True)
    exited_at: Mapped[Optional[dt.datetime]] = mapped_column(sa.DateTime, nullable=True)
    entry_price: Mapped[float] = mapped_column(MONEY, default=0)
    exit_price: Mapped[float] = mapped_column(MONEY, default=0)
    r: Mapped[float] = mapped_column(sa.Float, default=0.0)
    exit_reason: Mapped[str] = mapped_column(sa.String(24), default="")
    noise: Mapped[Optional[list]] = mapped_column(sa.JSON, nullable=True)
    confirmed: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    mfe_r: Mapped[float] = mapped_column(sa.Float, default=0.0)
    scaled: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    held_out: Mapped[bool] = mapped_column(sa.Boolean, default=False)      # in the out-of-sample sessions
    features: Mapped[Optional[dict]] = mapped_column(sa.JSON, nullable=True)
    feature_schema: Mapped[int] = mapped_column(sa.Integer, default=0)   # research/features.py FEATURE_SCHEMA
    drift_r: Mapped[float] = mapped_column(sa.Float, default=0.0)        # the stock's own drift while held, in R
    cost_r: Mapped[float] = mapped_column(sa.Float, default=0.0)         # slippage and commission paid, in R


class ShadowTradeLog(Base):
    """A play the app showed and didn't take, followed on the session's candles as if it had been
    (research/journal.py shadow_outcomes) - the rows that keep a model honest about what the
    gates turned away."""

    __tablename__ = "shadow_trades"

    play_id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    session_date: Mapped[dt.date] = mapped_column(sa.Date, index=True)
    symbol: Mapped[str] = mapped_column(sa.String(16), index=True)
    strategy: Mapped[str] = mapped_column(sa.String(48), index=True)
    side: Mapped[str] = mapped_column(sa.String(8))
    timeframe: Mapped[str] = mapped_column(sa.String(16), default="INTRADAY")
    seen_at: Mapped[Optional[dt.datetime]] = mapped_column(sa.DateTime, nullable=True)
    passed_checks: Mapped[bool] = mapped_column(sa.Boolean, default=False)   # Autopilot's checks, that is
    filled: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    entered_at: Mapped[Optional[dt.datetime]] = mapped_column(sa.DateTime, nullable=True)
    exited_at: Mapped[Optional[dt.datetime]] = mapped_column(sa.DateTime, nullable=True)
    entry_price: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)
    exit_price: Mapped[Optional[float]] = mapped_column(MONEY, nullable=True)
    r: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    mfe_r: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    exit_reason: Mapped[str] = mapped_column(sa.String(64), default="")
    noise: Mapped[Optional[list]] = mapped_column(sa.JSON, nullable=True)
    confirmations: Mapped[int] = mapped_column(sa.Integer, default=1)
    features: Mapped[Optional[dict]] = mapped_column(sa.JSON, nullable=True)
    feature_schema: Mapped[int] = mapped_column(sa.Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(sa.DateTime, default=_utcnow)
