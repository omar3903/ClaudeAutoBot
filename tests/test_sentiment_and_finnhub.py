"""Headline sentiment (with a stand-in for FinBERT) and Finnhub news (made-up stories)."""

from __future__ import annotations

import datetime as dt

from tos_bot.signals.finnhub import FinnhubNews, finnhub_symbol, parse_company_news
from tos_bot.signals.sentiment import HeadlineSentiment, symbol_sentiment

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 15, 12, tzinfo=UTC)


def _fake_finbert(model):
    def run(headlines, truncation=True):
        out = []
        for h in headlines:
            good = "beats" in h
            out.append([{"label": "positive", "score": 0.9 if good else 0.05},
                        {"label": "negative", "score": 0.05 if good else 0.85},
                        {"label": "neutral", "score": 0.05 if good else 0.10}])
        return out
    return run


def test_headlines_get_a_sentiment_and_a_confidence():
    model = HeadlineSentiment(loader=_fake_finbert, batch_size=1)
    assert model.score(["Example Holdings beats estimates", "Example Holdings misses badly"]) == [(0.85, 0.9), (-0.8, 0.85)]
    assert model.status()["loaded"]


def test_a_model_that_fails_to_load_leaves_headlines_unscored():
    def broken(model):
        raise OSError("no connection to download the model")
    model = HeadlineSentiment(loader=broken)
    assert model.score(["anything"]) == [] and "OSError" in model.status()["error"]


def test_recent_confident_headlines_count_most():
    rows = [{"published_at": NOW.isoformat(), "sentiment": 0.8, "sentiment_conf": 0.9},
            {"published_at": (NOW - dt.timedelta(days=3)).isoformat(), "sentiment": -0.9, "sentiment_conf": 0.9},
            {"published_at": NOW.isoformat(), "sentiment": None, "sentiment_conf": None}]
    reading = symbol_sentiment(rows, NOW)
    assert reading["headlines"] == 2 and 0.6 < reading["score"] < 0.8
    assert symbol_sentiment(rows[2:], NOW) is None


def test_finnhub_stories_become_news():
    rows = [{"category": "company", "datetime": 1789473600, "headline": "Example Holdings wins a contract",
             "id": 7, "source": "Example Wire", "url": "https://example.com/story"},
            {"datetime": 1789473600, "headline": ""}]
    [item] = parse_company_news("BRK B", rows)
    assert (item.source, item.provider, item.ref, item.url) == ("finnhub", "Example Wire", "7", "https://example.com/story")
    assert item.published_at == dt.datetime.fromtimestamp(1789473600, UTC)
    assert finnhub_symbol("BRK B") == "BRK.B"


def test_the_finnhub_key_goes_in_a_header_not_the_address():
    sent = {}

    class Session:
        def get(self, url, params=None, headers=None, timeout=None):
            sent.update(url=url, params=params, headers=headers)
            return type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: []})()

    assert FinnhubNews("", session=Session()).company_news("EXH", NOW.date(), NOW.date()) == []
    assert not sent                                     # no key, no request
    FinnhubNews("secret-key", session=Session()).company_news("EXH", NOW.date(), NOW.date())
    assert sent["headers"] == {"X-Finnhub-Token": "secret-key"} and "secret-key" not in str(sent["params"])
