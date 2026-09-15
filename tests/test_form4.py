"""Reading insider trades out of SEC Form 4 filings (a made-up company and insiders)."""

from __future__ import annotations

import datetime as dt

from tos_bot.signals.form4 import BUY, SELL, parse_form4


def _tx(code, shares, price, after, direct="D", note=""):
    return f"""<nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-09-09</value></transactionDate>
      <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>{code}</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>{shares}</value></transactionShares>
        <transactionPricePerShare><value>{price}</value>{note}</transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>{"D" if code == "S" else "A"}</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>{after}</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
      <ownershipNature><directOrIndirectOwnership><value>{direct}</value></directOrIndirectOwnership></ownershipNature>
    </nonDerivativeTransaction>"""


def _owner(name, cik, title="", officer=False, director=False, ten_pct=False):
    return f"""<reportingOwner>
      <reportingOwnerId><rptOwnerCik>{cik}</rptOwnerCik><rptOwnerName>{name}</rptOwnerName></reportingOwnerId>
      <reportingOwnerRelationship><isDirector>{int(director)}</isDirector><isOfficer>{int(officer)}</isOfficer>
        <isTenPercentOwner>{int(ten_pct)}</isTenPercentOwner><officerTitle>{title}</officerTitle></reportingOwnerRelationship>
    </reportingOwner>"""


def _filing(transactions, owners, doc_type="4", symbol="exh", plan="0", footnotes="", remarks=""):
    xml = f"""<?xml version="1.0"?>
<ownershipDocument>
  <schemaVersion>X0508</schemaVersion>
  <documentType>{doc_type}</documentType>
  <periodOfReport>2026-09-09</periodOfReport>
  <issuer><issuerCik>0000000101</issuerCik><issuerName>Example Holdings Inc</issuerName>
    <issuerTradingSymbol>{symbol}</issuerTradingSymbol></issuer>
  {"".join(owners)}
  <aff10b5One>{plan}</aff10b5One>
  <nonDerivativeTable>{"".join(transactions)}</nonDerivativeTable>
  <footnotes>{footnotes}</footnotes>
  <remarks>{remarks}</remarks>
</ownershipDocument>"""
    return f"<SEC-DOCUMENT>\n<DOCUMENT>\n<TYPE>4\n<TEXT>\n<XML>\n{xml}\n</XML>\n</TEXT>\n</DOCUMENT>\n</SEC-DOCUMENT>".encode()


def test_open_market_buys_and_sales_are_read_and_awards_left_out():
    doc = _filing([_tx("P", 10000, "12.50", 30000), _tx("A", 500, "0", 30500),
                   _tx("S", 2000, "13.00", 28500, note='<footnoteId id="F1"/>')],
                  [_owner("Doe Jane", 202, title="Chief Executive Officer", officer=True, director=True)],
                  footnotes='<footnote id="F1">Sold under a Rule 10b5-1 trading plan adopted in March.</footnote>')
    buy, sale = parse_form4(doc, "0000000202-26-000001")

    assert (buy.symbol, buy.code, buy.role, buy.owner_name, buy.issuer_cik) == ("EXH", BUY, "ceo_cfo", "Doe Jane", 101)
    assert (buy.shares, buy.price, buy.value, buy.trade_date) == (10000, 12.5, 125000, dt.date(2026, 9, 9))
    assert buy.holding_change == 0.5 and not buy.new_holding and not buy.planned and not buy.offering and buy.direct
    assert (sale.code, sale.line, sale.planned) == (SELL, 3, True)
    assert round(sale.holding_change, 4) == round(-2000 / 30500, 4)


def test_a_filing_under_a_10b5_1_plan_marks_every_trade():
    [buy] = parse_form4(_filing([_tx("P", 100, "5", 100)], [_owner("Roe Sam", 303, director=True)], plan="1"),
                        "0000000303-26-000002")
    assert buy.planned and buy.role == "director"
    assert buy.new_holding and buy.holding_change == 0.5      # may be held another way too, so not "doubled"


def test_purchases_in_a_placement_or_offering_are_marked():
    note = '<footnoteId id="F2"/>'
    footnote = '<footnote id="F2">Shares purchased in the Issuer\'s private placement at $1.50 per share.</footnote>'
    [placed] = parse_form4(_filing([_tx("P", 1000, "1.50", 5000, note=note)], [_owner("Roe Sam", 303, director=True)],
                                   footnotes=footnote), "0000000303-26-000003")
    [offered] = parse_form4(_filing([_tx("P", 1000, "4.00", 5000)], [_owner("Roe Sam", 303, director=True)],
                                    remarks="Purchased in the underwritten public offering."), "0000000303-26-000004")
    assert placed.offering and offered.offering


def test_the_most_senior_of_several_reporting_owners_is_taken():
    owners = [_owner("Example Capital Fund LP", 404, ten_pct=True), _owner("Poe Alex", 405, title="VP Sales", officer=True)]
    [trade] = parse_form4(_filing([_tx("S", 50, "20", 0, direct="I")], owners), "0000000404-26-000003")
    assert (trade.owner_name, trade.role, trade.direct, trade.holding_change) == ("Poe Alex", "officer", False, -1.0)


def test_a_company_with_several_share_classes_takes_the_first_ticker():
    [trade] = parse_form4(_filing([_tx("P", 10, "3", 20)], [_owner("Doe Jane", 202, director=True)],
                                  symbol="EXH, EXH.B"), "0000000202-26-000009")
    assert trade.symbol == "EXH"


def test_amendments_filings_without_a_ticker_and_bad_xml_give_nothing():
    owners = [_owner("Doe Jane", 202, director=True)]
    assert parse_form4(_filing([_tx("P", 1, "1", 1)], owners, doc_type="4/A"), "a") == []
    assert parse_form4(_filing([_tx("P", 1, "1", 1)], owners, symbol="NONE"), "b") == []
    assert parse_form4(b"<ownershipDocument><documentType>4", "c") == []
