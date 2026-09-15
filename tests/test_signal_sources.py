"""Finding filings on EDGAR and shaping company news (made-up companies and filings)."""

from __future__ import annotations

import datetime as dt

from tos_bot.signals.edgar import company_filings, daily_index, daily_index_url, latest_filings
from tos_bot.signals.news import NewsItem, eight_k_headline, eight_k_news, ibkr_headline, is_material

UTC = dt.timezone.utc


def _entry(title, cik, accession, updated="2026-09-15T08:32:11-04:00"):
    folder = accession.replace("-", "")
    return (f"<entry><title>{title}</title><link rel=\"alternate\" type=\"text/html\" "
            f"href=\"https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{accession}-index.htm\"/>"
            f"<updated>{updated}</updated></entry>")


def test_the_live_feed_lists_each_filing_once():
    atom = ("<?xml version=\"1.0\" encoding=\"ISO-8859-1\" ?><feed xmlns=\"http://www.w3.org/2005/Atom\">"
            + _entry("4 - Doe Jane (0000000202) (Reporting)", 202, "0000000202-26-000001")
            + _entry("4 - Example Holdings Inc (0000000101) (Issuer)", 101, "0000000202-26-000001")
            + _entry("4 - Roe Sam (0000000303) (Reporting)", 303, "0000000303-26-000007", "2026-09-14T17:02:00-04:00")
            + "</feed>").encode()
    first, second = latest_filings(atom)
    assert (first.accession, first.form, first.cik, first.filed) == ("0000000202-26-000001", "4", 202, dt.date(2026, 9, 15))
    assert first.url == "https://www.sec.gov/Archives/edgar/data/202/000000020226000001/0000000202-26-000001.txt"
    assert (second.accession, second.filed) == ("0000000303-26-000007", dt.date(2026, 9, 14))


def test_a_days_index_gives_every_form_4_once():
    def line(form, name, cik, file):
        return f"{form:<17}{name:<62}{cik:<12}{'20260914':<12}{file}    "
    text = "\n".join([
        "Form Type   Company Name                                                  CIK         Date Filed  File Name",
        "-" * 120,
        line("4", "Example Holdings Inc", 101, "edgar/data/101/0000000202-26-000001.txt"),
        line("4", "Doe Jane", 202, "edgar/data/202/0000000202-26-000001.txt"),
        line("4/A", "Example Holdings Inc", 101, "edgar/data/101/0000000202-26-000002.txt"),
        line("8-K", "Example Holdings Inc", 101, "edgar/data/101/0000000101-26-000010.txt"),
    ])
    [ref] = daily_index(text)
    assert (ref.accession, ref.form, ref.cik, ref.filed) == ("0000000202-26-000001", "4", 101, dt.date(2026, 9, 14))
    assert ref.url == "https://www.sec.gov/Archives/edgar/data/101/0000000202-26-000001.txt"
    assert daily_index_url(dt.date(2026, 9, 14)).endswith("/daily-index/2026/QTR3/form.20260914.idx")


def test_a_days_index_can_keep_only_filings_about_listed_companies():
    def row(name, cik, accession):
        return f"{'4':<17}{name:<62}{cik:<12}{'20260914':<12}edgar/data/{cik}/{accession}.txt"
    text = "\n".join([row("Doe Jane", 202, "0000000202-26-000001"),
                      row("Example Holdings Inc", 101, "0000000202-26-000001"),
                      row("Private Fund LP", 505, "0000000505-26-000001")])
    [ref] = daily_index(text, keep_cik={101}.__contains__)
    assert (ref.accession, ref.cik) == ("0000000202-26-000001", 101)
    assert ref.url.endswith("edgar/data/101/0000000202-26-000001.txt")


SUBMISSIONS = {
    "cik": "101",
    "filings": {"recent": {
        "form": ["4", "8-K", "4", "8-K"],
        "accessionNumber": ["0000000202-26-000001", "0000000101-26-000010", "0000000202-26-000000",
                            "0000000101-26-000004"],
        "filingDate": ["2026-09-14", "2026-09-10", "2025-06-01", "2026-07-30"],
        "acceptanceDateTime": ["2026-09-14T21:00:00.000Z", "2026-09-10T20:30:28.000Z", "2025-06-01T12:00:00.000Z",
                               "2026-07-30T20:30:00.000Z"],
        "items": ["", "2.02,9.01", "", "5.02"],
        "primaryDocument": ["xslF345X06/form4.xml", "exh-20260910.htm", "xslF345X06/form4.xml", "exh-20260730.htm"],
    }},
}


def test_a_companys_filing_history_is_read_from_its_submissions_file():
    [ref] = company_filings(SUBMISSIONS, ("4",), since=dt.date(2025, 9, 15))
    assert (ref.accession, ref.cik, ref.filed) == ("0000000202-26-000001", 101, dt.date(2026, 9, 14))


def test_8k_filings_become_news_with_what_they_report():
    [item] = eight_k_news("EXH", SUBMISSIONS, since=dt.datetime(2026, 9, 1, tzinfo=UTC))
    assert (item.source, item.kind, item.items, item.ref) == ("sec", "filing", "2.02,9.01", "0000000101-26-000010")
    assert item.headline == "8-K: results of operations (earnings)"
    assert item.published_at == dt.datetime(2026, 9, 10, 20, 30, 28, tzinfo=UTC)
    assert item.url == "https://www.sec.gov/Archives/edgar/data/101/000000010126000010/exh-20260910.htm"
    assert is_material("2.02,9.01") and not is_material("7.01,9.01")
    assert eight_k_headline("7.01,9.01") == "8-K: a Regulation FD disclosure"


def test_ibkr_headlines_lose_their_metadata_and_analyst_actions_are_marked():
    assert ibkr_headline("{A:800015:L:en}Example Holdings beats on revenue") == ("Example Holdings beats on revenue", "news")
    assert ibkr_headline("{A:800015:L:en:K:n/a:C:0.97}!A broker upgraded Example Holdings (EXH) to Buy") == \
        ("A broker upgraded Example Holdings (EXH) to Buy", "analyst")


def test_the_same_story_gets_the_same_key():
    at = dt.datetime(2026, 9, 15, 12, tzinfo=UTC)
    a = NewsItem("EXH", "ibkr", "BRFG", "Headline", at, ref="BRFG$1")
    b = NewsItem("EXH", "ibkr", "BRFG", "Headline, edited", at, ref="BRFG$1")
    assert a.key == b.key and a.key != NewsItem("EXH", "ibkr", "BRFG", "Headline", at, ref="BRFG$2").key
