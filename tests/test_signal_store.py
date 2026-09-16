"""Keeping insider trades, the filings already read, and news (made-up companies)."""

from __future__ import annotations

import datetime as dt

from tos_bot.signals.edgar import FilingRef
from tos_bot.signals.form4 import BUY, InsiderTrade
from tos_bot.signals.insiders import filing_delays
from tos_bot.signals.news import NewsItem
from tos_bot.signals.store import SignalStore

UTC = dt.timezone.utc


def _trade(accession, line=1, symbol="STOR", day=dt.date(2026, 9, 12)):
    return InsiderTrade(accession=accession, line=line, symbol=symbol, issuer_cik=909, issuer_name="Store Test Inc",
                        owner_cik=910, owner_name="Doe Jane", role="director", title="", code=BUY, trade_date=day,
                        shares=1000.0, price=10.5, shares_after=5000.0, planned=False, direct=True)


def test_filings_are_read_once_and_their_trades_come_back():
    store = SignalStore()
    read = FilingRef("0000000909-26-000001", "4", 909, dt.date(2026, 9, 14), "https://example/1.txt")
    empty = FilingRef("0000000909-26-000002", "4", 909, dt.date(2026, 9, 14), "https://example/2.txt")
    fresh = FilingRef("0000000909-26-000003", "4", 909, dt.date(2026, 9, 14), "https://example/3.txt")
    trade = _trade(read.accession)
    store.save_filing(read, [trade])
    store.save_filing(read, [trade])                      # reading it again changes nothing
    store.save_filing(empty, [])

    assert store.unread([read, empty, fresh]) == [fresh]
    assert store.insider_trades(dt.date(2026, 9, 1), ["STOR"]) == [trade]
    assert store.insider_trades(dt.date(2026, 9, 13), ["STOR"]) == []
    assert "STOR" in store.symbols_traded(dt.date(2026, 9, 1))
    assert store.last_filed() >= dt.date(2026, 9, 14)


def test_news_is_kept_once_and_scored_later():
    store = SignalStore()
    at = dt.datetime(2026, 9, 15, 12, tzinfo=UTC)
    story = NewsItem("NWSS", "ibkr", "BRFG", "News Test Inc raises its outlook", at, ref="BRFG$9")
    filing = NewsItem("NWSS", "sec", "8-K", "8-K: results of operations (earnings)", at - dt.timedelta(hours=1),
                      kind="filing", ref="0000000909-26-000009", items="2.02,9.01")
    assert store.save_news([story, filing]) == 2
    assert store.save_news([story]) == 0

    unscored = dict(store.unscored_news())
    assert story.key in unscored and filing.key not in unscored
    store.set_sentiment({story.key: (0.8, 0.93)})
    assert story.key not in dict(store.unscored_news())

    rows = store.news(["NWSS"], since=at - dt.timedelta(days=1))
    assert [(r["headline"], r["sentiment"]) for r in rows] == [
        ("News Test Inc raises its outlook", 0.8), ("8-K: results of operations (earnings)", None)]
    assert rows[0]["published_at"] == "2026-09-15T12:00:00+00:00"


def test_the_signals_page_reads_filings_with_their_delays_and_every_stocks_news():
    store = SignalStore()
    prompt = FilingRef("0000000911-26-000001", "4", 911, dt.date(2026, 9, 14), "https://example/11.txt")
    late = FilingRef("0000000911-26-000002", "4", 911, dt.date(2026, 9, 14), "https://example/12.txt")
    store.save_filing(prompt, [_trade(prompt.accession, symbol="LAGS", day=dt.date(2026, 9, 10))])   # Thu -> Mon
    store.save_filing(late, [_trade(late.accession, symbol="LAGS", day=dt.date(2026, 9, 1))])
    rows = store.insider_filings(dt.date(2026, 9, 14), ["LAGS"])
    assert [(r["trade_date"], r["filed"], r["value"]) for r in rows] == [
        ("2026-09-10", "2026-09-14", 10_500.0), ("2026-09-01", "2026-09-14", 10_500.0)]
    assert filing_delays(rows) == {"trades": 2, "median_days": 5.5, "on_time": 0.5, "late": 1, "max_days": 9}
    assert filing_delays([]) is None

    at = dt.datetime(2031, 1, 6, 15, tzinfo=UTC)
    scored = NewsItem("PAGA", "finnhub", "Reuters", "Page Test A wins a contract", at, ref="fh1")
    plain = NewsItem("PAGB", "ibkr", "BRFG", "Page Test B names a new CFO", at + dt.timedelta(hours=1), ref="BRFG$1")
    store.save_news([scored, plain])
    store.set_sentiment({scored.key: (0.7, 0.9)})
    assert [r["symbol"] for r in store.news(None, at)] == ["PAGB", "PAGA"]
    assert [r["symbol"] for r in store.news(None, at, limit=1)] == ["PAGB"]
    assert store.sentiment_counts(at) == {"headlines": 2, "scored": 1}
    store.set_sentiment({plain.key: (0.0, 0.8)})         # left unscored, it would be scored by other tests
