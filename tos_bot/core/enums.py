from __future__ import annotations

from enum import Enum


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def sign(self) -> int:
        return 1 if self is Side.LONG else -1

    @property
    def entry_action(self) -> str:
        return "BUY" if self is Side.LONG else "SELL_SHORT"



class AssetClass(str, Enum):
    EQUITY = "EQUITY"
    ETF = "ETF"
    OPTION = "OPTION"
    FUTURE = "FUTURE"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class TimeInForce(str, Enum):
    DAY = "DAY"
    GTC = "GOOD_TILL_CANCEL"
    IOC = "IMMEDIATE_OR_CANCEL"
    FOK = "FILL_OR_KILL"


class PlayStatus(str, Enum):
    PROPOSED = "PROPOSED"        # shown to the operator, not acted on
    ACCEPTED = "ACCEPTED"        # operator clicked Yes; about to route
    REJECTED = "REJECTED"        # operator dismissed it
    SUBMITTED = "SUBMITTED"      # order sent to broker
    WORKING = "WORKING"          # live at the exchange
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"            # entry filled -> becomes an open trade
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"          # proposal aged out before acceptance
    ERROR = "ERROR"


class StrategyKind(str, Enum):
    TECHNICAL = "TECHNICAL"
    FUNDAMENTAL = "FUNDAMENTAL"


class Timeframe(str, Enum):
    INTRADAY = "INTRADAY"       # day trade -- counts against PDT
    SWING = "SWING"             # multi-day hold
