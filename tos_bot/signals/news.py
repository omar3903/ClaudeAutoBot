"""Company news in one shape, whatever it came from: IBKR's news feeds (on this
account Briefing.com columns and analyst actions), SEC 8-K filings, and Finnhub."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Dict, List, Mapping, Tuple


@dataclass
class NewsItem:
    symbol: str
    source: str                    # ibkr | sec | finnhub
    provider: str                  # BRFG, BRFUPDN, 8-K, or Finnhub's source name
    headline: str
    published_at: dt.datetime      # UTC
    kind: str = "news"             # news | analyst | filing
    url: str = ""
    ref: str = ""                  # the source's own id: IBKR article id, SEC accession number, Finnhub id
    items: str = ""                # an 8-K's item numbers, like "2.02,9.01"

    @property
    def key(self) -> str:
        """The same story is stored once."""
        return hashlib.sha1(f"{self.symbol}|{self.source}|{self.ref or self.headline}".encode()).hexdigest()[:32]

    def as_dict(self) -> Dict[str, object]:
        row = asdict(self)
        row.update(published_at=self.published_at.isoformat(), key=self.key)
        return row


_IBKR_META = re.compile(r"^\s*\{[^}]*\}")


def ibkr_headline(raw: str) -> Tuple[str, str]:
    """IBKR headlines open with metadata in braces, like ``{A:800015:L:en:K:n/a:C:0.97}``,
    and analyst actions with a "!". Returns (headline, kind)."""
    text = _IBKR_META.sub("", raw or "").strip()
    return (text[1:].strip(), "analyst") if text.startswith("!") else (text, "news")


#: what an 8-K's items report (SEC Form 8-K instructions)
EIGHT_K_ITEMS = {
    "1.01": "entered a material agreement", "1.02": "ended a material agreement",
    "1.03": "bankruptcy or receivership", "1.05": "a material cybersecurity incident",
    "2.01": "completed an acquisition or sale of assets", "2.02": "results of operations (earnings)",
    "2.03": "took on a material debt", "2.04": "a debt became due early", "2.05": "exit or restructuring costs",
    "2.06": "a material impairment", "3.01": "a delisting notice or failed listing rule",
    "3.02": "sold shares privately", "3.03": "changed shareholders' rights", "4.01": "changed auditor",
    "4.02": "earlier financial statements can no longer be relied on", "5.01": "a change in control",
    "5.02": "a director or officer left or was appointed", "5.03": "changed its articles or bylaws",
    "5.07": "shareholder vote results", "7.01": "a Regulation FD disclosure", "8.01": "other events",
    "9.01": "financial statements and exhibits",
}
#: items that matter on their own; the rest mostly come along with them
MATERIAL_ITEMS = frozenset({"1.01", "1.02", "1.03", "1.05", "2.01", "2.02", "2.03", "2.04", "2.05", "2.06",
                            "3.01", "3.02", "4.01", "4.02", "5.01", "5.02"})


def _codes(items: str) -> List[str]:
    return [c.strip() for c in (items or "").split(",") if c.strip()]


def is_material(items: str) -> bool:
    return any(c in MATERIAL_ITEMS for c in _codes(items))


def eight_k_headline(items: str) -> str:
    codes = [c for c in _codes(items) if c != "9.01"]
    material = [c for c in codes if c in MATERIAL_ITEMS]
    said = [EIGHT_K_ITEMS.get(c, f"item {c}") for c in (material or codes)]
    return "8-K: " + ("; ".join(said) if said else "current report")


def eight_k_news(symbol: str, submissions: Mapping, since: dt.datetime) -> List[NewsItem]:
    """A company's 8-K filings accepted since ``since`` (UTC), as news, from its SEC
    submissions file."""
    recent = (submissions.get("filings") or {}).get("recent") or {}
    cik = int(submissions.get("cik") or 0)
    out: List[NewsItem] = []
    for i, form in enumerate(recent.get("form", [])):
        if form not in ("8-K", "8-K/A"):
            continue
        accepted = _utc(recent["acceptanceDateTime"][i])
        if accepted < since:
            continue
        accession, items = recent["accessionNumber"][i], (recent.get("items") or [""] * (i + 1))[i] or ""
        out.append(NewsItem(
            symbol=symbol, source="sec", provider=form, headline=eight_k_headline(items), published_at=accepted,
            kind="filing", ref=accession, items=items,
            url=f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{recent['primaryDocument'][i]}"))
    return out


def _utc(stamp: str) -> dt.datetime:
    return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
