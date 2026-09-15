"""SEC Form 4: what a company's insiders bought and sold, read from the filing.

Officers, directors and 10% owners file a Form 4 within two business days of
trading their company's stock. Only purchases (code P) and sales (code S) say
something about what an insider thinks the stock is worth - awards, option
exercises, shares withheld for tax and gifts don't, so they are left out. Trades
under a 10b5-1 plan were scheduled months ahead, and purchases in a private
placement or offering were arranged with the company; both are kept but marked.
Amendments (4/A) restate earlier filings and are skipped so nothing counts twice.
"""

from __future__ import annotations

import datetime as dt
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import List, Optional, Tuple

BUY, SELL = "P", "S"
#: best role first - a filing with several reporting owners takes the most senior
ROLES = ("ceo_cfo", "officer", "director", "ten_percent_owner", "other")
ROLE_LABELS = {"ceo_cfo": "top executive", "officer": "officer", "director": "director",
               "ten_percent_owner": "10% owner", "other": "insider"}

_TOP_TITLE = re.compile(r"chief executive|\bceo\b|chief financial|\bcfo\b|president|chair|chief operating|\bcoo\b|founder",
                        re.I)
_PLAN = re.compile(r"10b5-?1", re.I)
_OFFERING = re.compile(r"private placement|registered direct|public offering|underwritten offering|rights offering|"
                       r"subscription agreement|securities purchase agreement|\bPIPE\b|offering price", re.I)
_DOCUMENT = re.compile(rb"<ownershipDocument>.*?</ownershipDocument>", re.S)


@dataclass
class InsiderTrade:
    accession: str
    line: int                    # the transaction's place in the filing
    symbol: str
    issuer_cik: int
    issuer_name: str
    owner_cik: int
    owner_name: str
    role: str                    # one of ROLES
    title: str                   # the officer title as filed, if any
    code: str                    # P (purchase) | S (sale)
    trade_date: dt.date
    shares: float
    price: float
    shares_after: float          # what the insider holds afterwards, in this form of ownership
    planned: bool                # under a 10b5-1 trading plan
    direct: bool                 # held directly, not through a trust or fund
    offering: bool = False       # bought in a private placement or offering, per the filing's notes

    @property
    def value(self) -> float:
        return self.shares * self.price

    @property
    def new_holding(self) -> bool:
        """A purchase with nothing held before in this form of ownership - a first stake,
        or shares that are also held another way (directly as well as through a trust)."""
        return self.code == BUY and self.shares_after - self.shares <= 0

    @property
    def holding_change(self) -> float:
        """How much the trade changed the insider's holding: +0.5 is a buy that grew it by
        half, -1.0 a sale of all of it. A new holding counts as +0.5, since the insider
        may well hold more another way."""
        if self.code == BUY:
            before = self.shares_after - self.shares
            return 0.5 if before <= 0 else min(1.0, self.shares / before)
        before = self.shares_after + self.shares
        return -1.0 if before <= 0 else -min(1.0, self.shares / before)


def parse_form4(document: bytes, accession: str) -> List[InsiderTrade]:
    """The purchases and sales in one Form 4. ``document`` is the filing's XML, or the
    full submission text with the XML inside it."""
    found = _DOCUMENT.search(document)
    try:
        root = ET.fromstring(found.group(0) if found else document)
    except ET.ParseError:
        return []
    if _text(root, "documentType") != "4":
        return []
    symbol = _ticker(_text(root, "issuer/issuerTradingSymbol"))
    if not symbol:
        return []
    owner_cik, owner_name, role, title = _owner(root)
    notes = {n.get("id"): n.text or "" for n in root.iter("footnote")}
    plan_filing = _text(root, "aff10b5One").lower() in ("1", "true")
    offering_filing = bool(_OFFERING.search(_text(root, "remarks")))
    trades: List[InsiderTrade] = []
    for line, t in enumerate(root.findall("nonDerivativeTable/nonDerivativeTransaction"), start=1):
        code = _text(t, "transactionCoding/transactionCode")
        shares, price = _number(t, "transactionAmounts/transactionShares/value"), \
            _number(t, "transactionAmounts/transactionPricePerShare/value")
        day = _date(_text(t, "transactionDate/value"))
        if code not in (BUY, SELL) or not shares or not price or day is None:
            continue
        referenced = [notes.get(f.get("id"), "") for f in t.iter("footnoteId")]
        trades.append(InsiderTrade(
            accession=accession, line=line, symbol=symbol,
            issuer_cik=int(_number(root, "issuer/issuerCik") or 0), issuer_name=_text(root, "issuer/issuerName"),
            owner_cik=owner_cik, owner_name=owner_name, role=role, title=title, code=code, trade_date=day,
            shares=shares, price=price,
            shares_after=_number(t, "postTransactionAmounts/sharesOwnedFollowingTransaction/value") or 0.0,
            planned=plan_filing or any(_PLAN.search(note) for note in referenced),
            direct=_text(t, "ownershipNature/directOrIndirectOwnership/value").upper() != "I",
            offering=code == BUY and (offering_filing or any(_OFFERING.search(note) for note in referenced)),
        ))
    return trades


def _ticker(raw: str) -> str:
    """The issuer's ticker. A company with several share classes may list them all,
    like "EXH, EXH.B"; the first is taken."""
    first = re.split(r"[,;/]", raw or "")[0].strip().upper()
    return "" if first in ("NONE", "N/A", "NA") else first


def _owner(root: ET.Element) -> Tuple[int, str, str, str]:
    """The filing's most senior reporting owner: (cik, name, role, title)."""
    best: Optional[Tuple[int, int, str, str, str]] = None
    for owner in root.findall("reportingOwner"):
        rel = owner.find("reportingOwnerRelationship")
        role, title = _role(rel) if rel is not None else ("other", "")
        row = (ROLES.index(role), int(_number(owner, "reportingOwnerId/rptOwnerCik") or 0),
               _text(owner, "reportingOwnerId/rptOwnerName"), role, title)
        if best is None or row[0] < best[0]:
            best = row
    return (best[1], best[2], best[3], best[4]) if best else (0, "", "other", "")


def _role(rel: ET.Element) -> Tuple[str, str]:
    flag = lambda tag: _text(rel, tag).lower() in ("1", "true")  # noqa: E731
    title = _text(rel, "officerTitle")
    if title and _TOP_TITLE.search(title):
        return "ceo_cfo", title
    if flag("isOfficer"):
        return "officer", title
    if flag("isDirector"):
        return "director", title
    if flag("isTenPercentOwner"):
        return "ten_percent_owner", title
    return "other", title or _text(rel, "otherText")


def _text(el: ET.Element, path: str) -> str:
    return (el.findtext(path) or "").strip()


def _number(el: ET.Element, path: str) -> Optional[float]:
    try:
        return float(_text(el, path).replace(",", ""))
    except ValueError:
        return None


def _date(value: str) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None
