"""Where orders go, and which IB Gateway connection is held for it.

Pure logic, no I/O, so the routing is easy to reason about and test:

    mode   paper platform   connection held               orders go to
    ----   --------------   ---------------------------   --------------------------
    live   (either)         IBKR live account, trading    your live IBKR account
    paper  ibkr             IBKR paper account, trading   your IBKR paper account
    paper  simulator        IBKR paper account, data      the built-in simulator
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

PAPER_PLATFORMS = {
    "ibkr": "IBKR paper account",
    "simulator": "Built-in simulator (on IBKR prices)",
}

# venue ids are stored on every trade, so an exit only ever goes to the account
# that actually holds the position
_VENUE_LABELS = {
    "paper": "the built-in simulator",
    "ibkr-paper": "your IBKR paper account",
    "ibkr-live": "your live IBKR account",
}
ROUTE_LABELS = {
    "paper": "SIMULATED (built-in)",
    "ibkr-paper": "IBKR PAPER ACCOUNT",
    "ibkr-live": "LIVE IBKR",
}


@dataclass(frozen=True)
class VenuePlan:
    account: str = "paper"          # which Gateway login: "paper" | "live"
    trade: bool = False             # orders go to this connection (else the simulator)

    @property
    def key(self) -> Tuple[str, bool]:
        """Identity of the connection - a read-only one is a different connection."""
        return self.account, self.trade


def normalize_platform(paper_platform: Optional[str]) -> str:
    pp = (paper_platform or "").strip().lower()
    return pp if pp in PAPER_PLATFORMS else "ibkr"


def plan_venue(mode: str, paper_platform: Optional[str]) -> VenuePlan:
    if mode == "live":
        return VenuePlan("live", trade=True)
    return VenuePlan("paper", trade=normalize_platform(paper_platform) == "ibkr")


def venue_id(plan: VenuePlan) -> str:
    return f"ibkr-{plan.account}" if plan.trade else "paper"


def venue_label(vid: Optional[str]) -> str:
    return _VENUE_LABELS.get(vid or "paper", vid or "the built-in simulator")


IBKR_STEPS = (
    "In IBKR Client Portal, enable your paper trading account (its username starts with DU).",
    "Install IB Gateway and log in - the paper user for the paper port, your normal user for live.",
    "Gateway → Configure → Settings → API → Settings: set the socket port (4002 paper / 4001 live), "
    "keep 127.0.0.1 in Trusted IPs and untick Read-Only API. (Older versions also have an Enable "
    "ActiveX and Socket Clients box to tick; newer ones have the API on already.)",
    "Configure → Settings → Lock and Exit → Auto restart (not Auto logoff), set to 9:00 PM New York time. "
    "After-hours trading has ended at 8:00 PM, IBKR's nightly maintenance (about 11:45 PM–12:45 AM ET) "
    "hasn't started, and pre-market (4:00 AM) and the pre-market scan are hours away. The app reconnects "
    "by itself; IBKR still asks for a full login about once a week. For hands-off logins, run it through "
    "IBC (github.com/IbcAlpha/IBC).",
    "Click Test paper (or Test live). There's no token to store - IBKR's login is the running Gateway, "
    "and the app connects by itself as soon as the Gateway answers.",
)
