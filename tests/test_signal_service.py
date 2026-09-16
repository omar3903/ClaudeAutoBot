"""The signals service against a fake SEC and a fake IBKR news feed (made-up companies)."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from tos_bot.config import SignalsCfg
from tos_bot.signals.book import SignalBook
from tos_bot.signals.edgar import LATEST_URL, SUBMISSIONS_URL, TICKERS_URL, daily_index_url
from tos_bot.signals.sentiment import HeadlineSentiment
from tos_bot.signals.service import SignalService
from tos_bot.signals.store import SignalStore

TODAY = dt.date(2026, 9, 15)
SILENT = SimpleNamespace(publish=lambda *a, **k: None)


def _form4(symbol, owner, cik, title, code, shares, price, after, day):
    return f"""<SEC-DOCUMENT><XML><?xml version="1.0"?>
<ownershipDocument><documentType>4</documentType>
  <issuer><issuerCik>{cik}</issuerCik><issuerName>{symbol} Test Inc</issuerName><issuerTradingSymbol>{symbol}</issuerTradingSymbol></issuer>
  <reportingOwner><reportingOwnerId><rptOwnerCik>{owner}</rptOwnerCik><rptOwnerName>Insider {owner}</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>{title}</officerTitle></reportingOwnerRelationship></reportingOwner>
  <nonDerivativeTable><nonDerivativeTransaction>
    <transactionDate><value>{day}</value></transactionDate>
    <transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>
    <transactionAmounts><transactionShares><value>{shares}</value></transactionShares>
      <transactionPricePerShare><value>{price}</value></transactionPricePerShare></transactionAmounts>
    <postTransactionAmounts><sharesOwnedFollowingTransaction><value>{after}</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
  </nonDerivativeTransaction></nonDerivativeTable>
</ownershipDocument></XML></SEC-DOCUMENT>""".encode()


def _txt(cik, accession):
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{accession}.txt"


def _atom(*filings):
    entries = "".join(
        f"<entry><title>4 - Insider (Reporting)</title><link href=\"https://www.sec.gov/Archives/edgar/data/{cik}/"
        f"{acc.replace('-', '')}/{acc}-index.htm\"/><updated>2026-09-15T09:00:00-04:00</updated></entry>"
        for cik, acc in filings)
    return f"<feed xmlns=\"http://www.w3.org/2005/Atom\">{entries}</feed>".encode()


class FakeSec:
    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def content(self, url):
        self.asked.append(url)
        page = self.pages[url]                          # a missing page fails like a 404
        return page if isinstance(page, bytes) else str(page).encode()

    def text(self, url):
        return self.content(url).decode()

    def json(self, url):
        self.asked.append(url)
        return self.pages[url]


def _service(sec, tmp_path, book, **kw):
    cfg = SignalsCfg(backfill_days=1)
    return SignalService(cfg, SignalStore(), book, tmp_path / "signals.json", watched=kw.get("watched", lambda: []),
                         news_source=kw.get("news_source", lambda: None), con_ids=lambda syms: {s: 1 for s in syms},
                         finnhub_key=lambda: "", sec=sec, sentiment=kw.get("sentiment"), today=lambda: TODAY,
                         bus=SILENT)


def test_insider_filings_are_read_once_and_unusual_buying_reaches_the_book(tmp_path):
    index = (f"{'4':<17}{'SVCA Test Inc':<62}{601:<12}{'20260914':<12}edgar/data/601/0000000701-26-000001.txt\n")
    sec = FakeSec({
        daily_index_url(dt.date(2026, 9, 14)): index,
        "https://www.sec.gov/Archives/edgar/data/601/0000000701-26-000001.txt":
            _form4("SVCA", 701, 601, "Chief Executive Officer", "P", 100_000, "12.00", 100_000, "2026-09-14"),
        LATEST_URL.format(form="4", start=0): _atom((702, "0000000702-26-000001"), (801, "0000000801-26-000001")),
        _txt(702, "0000000702-26-000001"): _form4("SVCA", 702, 601, "VP Finance", "P", 20_000, "12.10", 20_000, "2026-09-15"),
        _txt(801, "0000000801-26-000001"): _form4("SVCB", 801, 602, "Director", "S", 1_000, "30.00", 9_000, "2026-09-15"),
        SUBMISSIONS_URL.format(cik=601): {"cik": "601", "filings": {"recent": {
            "form": ["4"], "accessionNumber": ["0000000701-26-000000"], "filingDate": ["2026-03-02"]}}},
        _txt(601, "0000000701-26-000000"):
            _form4("SVCA", 703, 601, "Director", "S", 500, "9.00", 4_500, "2026-03-01"),
    })
    book = SignalBook()
    service = _service(sec, tmp_path, book)
    service.poll_insiders()

    assert book.unusual_buying_symbols() == ["SVCA"]
    buying = book.get("SVCA").buying
    assert (buying.insiders, buying.value) == (2, 1_442_000.0)
    assert "No other open-market buying by its insiders in the previous year" in buying.reasons
    assert service.report["insiders"]["filings_read"] == 4

    sec.asked.clear()
    service.poll_insiders()                             # nothing new: no filing or index is fetched again
    assert [u for u in sec.asked if u.endswith(".txt") or u.endswith(".idx")] == []

    page = service.state()                              # the Signals page
    row = next(r for r in page["signals"] if r["symbol"] == "SVCA")
    assert row["effect"]["LONG"]["delta"] > 0 > row["effect"]["SHORT"]["delta"]
    assert row["effect"]["LONG"]["why"][0].startswith("unusual insider buying")
    assert {"SVCA", "SVCB"} <= {f["symbol"] for f in page["filings"]} and page["filing_delays"]["trades"] >= 3
    assert not page["finnhub"] and page["boosts"]["insider_buying"] == 0.10
    detail = service.symbol_state("SVCA")
    assert len(detail["filings"]) == 3 and detail["signals"]["buying"]["unusual"]
    assert service.symbol_state("NONE")["signals"] is None


def test_a_buyers_history_is_read_on_a_later_pass_when_it_couldnt_be_at_first(tmp_path):
    pages = {
        LATEST_URL.format(form="4", start=0): _atom((722, "0000000722-26-000001")),
        _txt(722, "0000000722-26-000001"): _form4("SVCH", 722, 621, "Chief Executive Officer", "P", 50_000, "8.00",
                                                  150_000, "2026-09-15"),
    }
    book = SignalBook()
    service = _service(FakeSec(pages), tmp_path, book)
    service.poll_insiders()                             # SEC's filing list for the company isn't available
    assert not any("previous year" in r for r in book.get("SVCH").buying.reasons)

    pages[SUBMISSIONS_URL.format(cik=621)] = {"cik": "621", "filings": {"recent": {
        "form": ["4"], "accessionNumber": ["0000000722-26-000000"], "filingDate": ["2026-05-01"]}}}
    pages[_txt(621, "0000000722-26-000000")] = _form4("SVCH", 722, 621, "Chief Executive Officer", "P", 10_000, "9.00",
                                                      100_000, "2026-05-01")
    service.poll_insiders()
    assert service.report["insiders"]["filings_read"] == 1
    assert not any("previous year" in r for r in book.get("SVCH").buying.reasons)    # the CEO bought in May too


def test_news_is_gathered_scored_and_summed_up_per_stock(tmp_path):
    now = dt.datetime.now(dt.timezone.utc)
    stamp = (now - dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    sec = FakeSec({
        TICKERS_URL: {"0": {"cik_str": 611, "ticker": "SVCN", "title": "SVCN Test Inc"}},
        SUBMISSIONS_URL.format(cik=611): {"cik": "611", "filings": {"recent": {
            "form": ["8-K"], "accessionNumber": ["0000000611-26-000009"], "filingDate": [now.date().isoformat()],
            "acceptanceDateTime": [stamp], "items": ["2.02,9.01"], "primaryDocument": ["svcn-8k.htm"]}}},
    })
    ibkr = SimpleNamespace(news_headlines=lambda con_ids, **kw: {"SVCN": [
        ((now - dt.timedelta(hours=1)).replace(tzinfo=None), "BRFG", "BRFG$1", "{A:800015:L:en}SVCN beats estimates"),
        ((now - dt.timedelta(hours=3)).replace(tzinfo=None), "BRFUPDN", "BRFUPDN$2",
         "{A:800015:L:en}!A broker upgraded SVCN Test (SVCN) to Buy, beats peers")]})

    def finbert(model):
        return lambda headlines, truncation=True: [[{"label": "positive", "score": 0.9}, {"label": "negative", "score": 0.05},
                                                    {"label": "neutral", "score": 0.05}] for _ in headlines]

    book = SignalBook()
    service = _service(sec, tmp_path, book, watched=lambda: ["SVCN"], news_source=lambda: ibkr,
                       sentiment=HeadlineSentiment(loader=finbert))
    service.poll_news()

    signals = book.get("SVCN")
    assert signals.news["headlines"] == 2 and signals.news["score"] == 0.85
    [filing] = signals.filings
    assert filing["headline"] == "8-K: results of operations (earnings)"
    assert service.report["news"]["new_stories"] == 3 and service.report["news"]["scored"] == 2


def test_a_check_can_be_asked_for_while_the_signals_are_on(tmp_path):
    service = _service(FakeSec({}), tmp_path, SignalBook())
    service.cfg.enabled = False
    assert not service.check_now()["ok"]
    service.cfg.enabled = True
    with service._checking:                             # a pass is already running
        assert service.check_now()["note"] == "A check is already running."
