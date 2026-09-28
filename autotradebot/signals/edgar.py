"""Finding filings on SEC EDGAR: the live feed of the newest filings, the daily index
of everything filed on a day, and one company's filing history. A filing is read
from its full submission text, which carries a Form 4's XML inside it."""

from __future__ import annotations

import datetime as dt
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Mapping, Optional

ARCHIVES = "https://www.sec.gov/Archives/"
LATEST_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={form}&owner=include"
              "&output=atom&count=100&start={start}")
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

_ATOM = "{http://www.w3.org/2005/Atom}"
_INDEX_LINK = re.compile(r"/data/(\d+)/\d+/(\d{10}-\d{2}-\d{6})-index\.html?$")
_INDEX_LINE = re.compile(r"^(?P<form>\S+(?: \S+)*?)\s{2,}.+?\s{2,}(?P<cik>\d+)\s{2,}(?P<date>\d{8})\s{2,}"
                         r"(?P<path>edgar/\S+\.txt)\s*$")


@dataclass(frozen=True)
class FilingRef:
    accession: str                 # 0001234567-26-000123
    form: str
    cik: int                       # who it was listed under: the company or the insider
    filed: dt.date
    url: str                       # the full submission text


def submission_url(cik: int, accession: str) -> str:
    return f"{ARCHIVES}edgar/data/{cik}/{accession.replace('-', '')}/{accession}.txt"


def daily_index_url(day: dt.date) -> str:
    return f"{ARCHIVES}edgar/daily-index/{day:%Y}/QTR{(day.month - 1) // 3 + 1}/form.{day:%Y%m%d}.idx"


def latest_filings(atom: bytes) -> List[FilingRef]:
    """The live feed of the newest filings. A Form 4 is listed once for the insider and
    once for the company under the same accession number; it is kept once."""
    out: List[FilingRef] = []
    seen = set()
    for entry in ET.fromstring(atom).findall(f"{_ATOM}entry"):
        link = entry.find(f"{_ATOM}link")
        found = _INDEX_LINK.search(link.get("href", "") if link is not None else "")
        if not found or found.group(2) in seen:
            continue
        seen.add(found.group(2))
        cik, accession = int(found.group(1)), found.group(2)
        updated = (entry.findtext(f"{_ATOM}updated") or "")[:10]
        out.append(FilingRef(accession=accession, form=(entry.findtext(f"{_ATOM}title") or "").split(" - ")[0].strip(),
                             cik=cik, filed=dt.date.fromisoformat(updated), url=submission_url(cik, accession)))
    return out


def daily_index(text: str, forms: Iterable[str] = ("4",),
                keep_cik: Optional[Callable[[int], bool]] = None) -> List[FilingRef]:
    """Every filing of ``forms`` in one day's form index, once each. A filing is listed
    under each party to it - for a Form 4, the company and the insider. With
    ``keep_cik``, only filings listed under a CIK it accepts are kept, under that CIK."""
    wanted = set(forms)
    out: List[FilingRef] = []
    seen = set()
    for line in text.splitlines():
        found = _INDEX_LINE.match(line)
        if not found or found.group("form") not in wanted:
            continue
        cik = int(found.group("cik"))
        accession = found.group("path").rsplit("/", 1)[-1][:-len(".txt")]
        if accession in seen or (keep_cik is not None and not keep_cik(cik)):
            continue
        seen.add(accession)
        out.append(FilingRef(accession=accession, form=found.group("form"), cik=cik,
                             filed=dt.datetime.strptime(found.group("date"), "%Y%m%d").date(),
                             url=ARCHIVES + found.group("path")))
    return out


def company_tickers(doc: Mapping) -> Dict[str, int]:
    """SEC's list of listed companies as {ticker: CIK} (tickers written like BRK-B)."""
    return {str(row["ticker"]).upper(): int(row["cik_str"]) for row in doc.values()}


def company_filings(submissions: Mapping, forms: Iterable[str], since: dt.date) -> List[FilingRef]:
    """One company's filings of ``forms`` since ``since``, from its SEC submissions file
    (which lists about its latest thousand filings)."""
    recent = (submissions.get("filings") or {}).get("recent") or {}
    cik, wanted = int(submissions.get("cik") or 0), set(forms)
    out: List[FilingRef] = []
    for i, form in enumerate(recent.get("form", [])):
        filed = dt.date.fromisoformat(recent["filingDate"][i])
        if form in wanted and filed >= since:
            accession = recent["accessionNumber"][i]
            out.append(FilingRef(accession=accession, form=form, cik=cik, filed=filed,
                                 url=submission_url(cik, accession)))
    return out
