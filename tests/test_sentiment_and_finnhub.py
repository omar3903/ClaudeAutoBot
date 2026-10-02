"""Headline sentiment (with a stand-in for FinBERT) and Finnhub news (made-up stories)."""

from __future__ import annotations

import datetime as dt
import os
import sys
import types

from autotradebot.signals import sentiment
from autotradebot.signals.finnhub import FinnhubNews, finnhub_symbol, parse_company_news
from autotradebot.signals.sentiment import HeadlineSentiment, symbol_sentiment

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


def test_finbert_is_pinned_to_one_revision_and_a_cached_copy_loads_without_going_online(monkeypatch, tmp_path):
    """Stand-ins for transformers and huggingface_hub record how the model would be loaded."""
    calls, cached = [], {}
    transformers = types.ModuleType("transformers")
    transformers.pipeline = lambda task, **kw: calls.append(kw) or "a pipeline"
    hub = types.ModuleType("huggingface_hub")
    hub.try_to_load_from_cache = lambda repo_id, filename, revision=None: cached.get((repo_id, filename, revision))
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.setenv("DISABLE_SAFETENSORS_CONVERSION", "0")
    monkeypatch.delenv("DISABLE_SAFETENSORS_CONVERSION")

    assert sentiment._load_pipeline(sentiment.MODEL) == "a pipeline"           # not downloaded yet
    assert calls[-1]["model"] == sentiment.MODEL and calls[-1]["revision"] == sentiment.REVISION
    assert calls[-1]["local_files_only"] is False and calls[-1]["top_k"] is None
    assert os.environ["DISABLE_SAFETENSORS_CONVERSION"] == "1"

    snapshot = tmp_path / "snapshots" / sentiment.REVISION
    cached.update({(sentiment.MODEL, name, sentiment.REVISION): str(snapshot / name) for name in sentiment.FILES})
    sentiment._load_pipeline(sentiment.MODEL)                                  # in the cache: from its folder only
    assert calls[-1]["model"] == str(snapshot) and calls[-1]["local_files_only"] is True
    assert calls[-1]["revision"] == sentiment.REVISION

    del cached[(sentiment.MODEL, "pytorch_model.bin", sentiment.REVISION)]      # a download that was cut off
    sentiment._load_pipeline(sentiment.MODEL)
    assert calls[-1]["model"] == sentiment.MODEL and calls[-1]["local_files_only"] is False


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
