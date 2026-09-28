"""How insider and news signals nudge plays (made-up stocks)."""

from __future__ import annotations

import datetime as dt

import pytest

from autotradebot.core.enums import Side, StrategyKind, Timeframe
from autotradebot.core.models import Play
from autotradebot.signals.book import BoostSettings, SignalBook, SymbolSignals, nudge
from autotradebot.signals.insiders import InsiderSignal

DAY = dt.date(2026, 9, 14)


def _insiders(direction, score, unusual=True):
    return InsiderSignal(symbol="EXH", direction=direction, score=score, unusual=unusual, insiders=2, trades=2,
                         value=500_000.0, first_date=DAY, last_date=DAY, top_role="ceo_cfo", reasons=["..."],
                         shares=50_000.0)


def _play(side=Side.LONG, symbol="EXH", score=0.5):
    return Play(symbol=symbol, side=side, strategy="week52_breakout", kind=StrategyKind.TECHNICAL,
                timeframe=Timeframe.SWING, entry=10.0, stop=9.0, targets=[13.0], score=score)


def test_unusual_insider_buying_lifts_longs_and_weighs_on_shorts():
    signals = SymbolSignals("EXH", buying=_insiders("buying", 0.8))
    assert nudge(Side.LONG, signals, BoostSettings()) == (0.08, ["unusual insider buying (+0.080)"])
    assert nudge(Side.SHORT, signals, BoostSettings())[0] == -0.08


def test_unusual_selling_weighs_on_longs_more_than_it_helps_shorts():
    signals = SymbolSignals("EXH", selling=_insiders("selling", 0.7))
    assert nudge(Side.LONG, signals, BoostSettings())[0] == pytest.approx(-0.056)
    assert nudge(Side.SHORT, signals, BoostSettings())[0] == pytest.approx(0.028)


def test_news_counts_once_enough_headlines_are_scored_and_ordinary_insider_trades_dont():
    few = SymbolSignals("EXH", news={"score": 0.5, "headlines": 1, "weight": 0.9})
    enough = SymbolSignals("EXH", news={"score": 0.5, "headlines": 3, "weight": 2.1},
                           buying=_insiders("buying", 0.4, unusual=False))
    assert nudge(Side.LONG, few, BoostSettings()) == (0.0, [])
    assert nudge(Side.LONG, enough, BoostSettings()) == (0.03, ["news sentiment +0.50 (+0.030)"])


def test_the_book_nudges_plays_and_says_why():
    book = SignalBook()
    book.set_insiders({"EXH": (_insiders("buying", 0.9), None)})
    play, other = _play(score=0.5), _play(symbol="OTHR", score=0.5)
    book.apply(play)
    book.apply(other)
    assert play.score == 0.59 and play.evidence["signal_reasons"] == "unusual insider buying (+0.090)"
    assert other.score == 0.5 and "signal_nudge" not in other.evidence

    short = _play(side=Side.SHORT, score=0.05)
    book.apply(short)
    assert short.score == 0.0                           # never below zero


def test_the_book_keeps_news_and_insiders_apart_and_forgets_what_is_gone():
    book = SignalBook()
    book.set_insiders({"EXH": (_insiders("buying", 0.9), None), "ABC": (_insiders("buying", 0.6), None),
                       "LOW": (_insiders("buying", 0.3, unusual=False), None)})
    book.set_news("EXH", {"score": -0.2, "headlines": 4, "weight": 3.0}, [])
    assert book.unusual_buying_symbols() == ["EXH", "ABC"]

    book.set_insiders({"ABC": (_insiders("buying", 0.6), None)})
    assert book.get("EXH").buying is None and book.get("EXH").news["score"] == -0.2
    assert book.get("LOW") is None
    book.set_news("EXH", None, [])
    assert book.get("EXH") is None
