"""Which broker connection backs trading and market data right now.

Pure logic (no I/O) so the engine's routing is easy to reason about and test.
Three switches, all set from the dashboard:

    mode  paper_platform  connection held             orders go to
    ----  --------------  --------------------------  ----------------------
    live  (any)           live_broker, live account   that live account
    paper ibkr            IBKR paper account          the IBKR paper account
    paper schwab          Schwab (data only)          the built-in simulator
    paper simulator       live_broker's feed,         the built-in simulator
                          data only

thinkorswim's paperMoney has no API, so the "schwab" paper platform simulates
fills on Schwab's real-time data instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

PAPER_PLATFORMS = {
    "simulator": "Built-in simulator",
    "ibkr": "IBKR paper account",
    "schwab": "thinkorswim / Schwab data (simulated fills)",
}
LIVE_BROKERS = {
    "ibkr": "Interactive Brokers",
    "schwab": "Charles Schwab / thinkorswim",
}

# venue ids are stored on every trade, so an exit is only ever sent to the
# account that actually holds the position
_VENUE_LABELS = {
    "paper": "the built-in simulator",
    "ibkr-paper": "your IBKR paper account",
    "ibkr-live": "your live IBKR account",
    "schwab": "your Schwab account",
}
ROUTE_LABELS = {
    "paper": "SIMULATED (built-in)",
    "ibkr-paper": "IBKR PAPER ACCOUNT",
    "ibkr-live": "LIVE IBKR",
    "schwab": "LIVE SCHWAB",
}


@dataclass(frozen=True)
class VenuePlan:
    broker: Optional[str] = None     # "ibkr" | "schwab" | None (no connection)
    account: str = "live"            # "paper" | "live" - for IBKR, which Gateway port
    trade: bool = False              # orders go to this connection (else the simulator)

    @property
    def key(self) -> Optional[Tuple[str, str, bool]]:
        """Identity of the connection. A Schwab connection is the same object
        whether it trades or only feeds data; an IBKR one is not (read-only)."""
        if self.broker is None:
            return None
        return (self.broker, self.account, self.trade if self.broker == "ibkr" else True)


def normalize(paper_platform: Optional[str], live_broker: Optional[str]) -> Tuple[str, str]:
    pp = (paper_platform or "").strip().lower()
    lb = (live_broker or "").strip().lower()
    return (pp if pp in PAPER_PLATFORMS else "simulator",
            lb if lb in LIVE_BROKERS else "schwab")


def plan_venue(mode: str, paper_platform: Optional[str], live_broker: Optional[str]) -> VenuePlan:
    pp, lb = normalize(paper_platform, live_broker)
    if mode == "live":
        return VenuePlan(lb, "live", trade=True)
    if pp == "ibkr":
        return VenuePlan("ibkr", "paper", trade=True)
    if pp == "schwab":
        return VenuePlan("schwab", "live", trade=False)
    # built-in simulator: borrow the live broker's feed, read-only
    return VenuePlan(lb, "paper" if lb == "ibkr" else "live", trade=False)


def venue_id(plan: VenuePlan) -> str:
    """The venue orders go to under ``plan`` (assuming its connection is up)."""
    if not plan.trade or plan.broker is None:
        return "paper"
    return f"ibkr-{plan.account}" if plan.broker == "ibkr" else plan.broker


def venue_label(vid: Optional[str]) -> str:
    return _VENUE_LABELS.get(vid or "paper", vid or "the built-in simulator")


# --------------------------------------------------------------------------- #
#  setup copy for the dashboard's Connections panel                           #
# --------------------------------------------------------------------------- #
IBKR_STEPS = (
    "In IBKR Client Portal, enable your paper trading account (its username starts with DU).",
    "Install IB Gateway and log in - the paper user for the paper port, your normal user for live.",
    "Gateway → Configure → Settings → API → Settings: tick Enable ActiveX and Socket Clients, set "
    "the socket port (4002 paper / 4001 live), add 127.0.0.1 to Trusted IPs, untick Read-Only API.",
    "Configure → Lock and Exit → Auto restart, so it survives IBKR's daily restart. For a hands-off "
    "daily login, run it through IBC (github.com/IbcAlpha/IBC).",
    "Click Test connection. There's no token to store - IBKR's login is the running Gateway.",
)
SCHWAB_STEPS = (
    "Create an app at developer.schwab.com (Trader API - Individual) and wait for Schwab to approve "
    "it (usually a few days).",
    "Set the app's callback URL to exactly https://127.0.0.1:8182.",
    "Paste the App Key and App Secret below and save.",
    "Click Sign in with Schwab. Log in on schwab.com and allow access. Your browser then warns about "
    "a certificate for 127.0.0.1:8182 - that page is served by this computer, so continue.",
    "Done. Schwab asks you to sign in again every 7 days; the dashboard reminds you a day ahead.",
)
