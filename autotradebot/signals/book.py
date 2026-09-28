"""The latest signals for each stock - insider buying and selling, news sentiment and
recent material filings - and what they do to a play's score.

A signal only nudges. Unusual insider buying adds up to ``insider_buying`` to a long
play's score and takes as much from a short; unusual insider selling takes up to
``insider_selling`` from a long and adds half of that to a short; the news moves a
play by up to ``news`` either way once enough headlines are scored. Every nudge is
written into the play's evidence, so it can be seen and measured.
"""

from __future__ import annotations

import datetime as dt
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..core.enums import Side
from ..core.models import Play
from .insiders import InsiderSignal


@dataclass(frozen=True)
class BoostSettings:
    insider_buying: float = 0.10
    insider_selling: float = 0.08
    news: float = 0.06
    min_headlines: int = 2           # scored headlines needed before the news counts

    @classmethod
    def from_config(cls, cfg: Any) -> "BoostSettings":
        return cls(insider_buying=float(cfg.boost_insider_buying), insider_selling=float(cfg.boost_insider_selling),
                   news=float(cfg.boost_news))


@dataclass
class SymbolSignals:
    symbol: str
    buying: Optional[InsiderSignal] = None
    selling: Optional[InsiderSignal] = None
    news: Optional[Dict[str, Any]] = None                          # see sentiment.symbol_sentiment
    filings: List[Dict[str, Any]] = field(default_factory=list)    # recent material 8-K filings

    @property
    def empty(self) -> bool:
        return self.buying is None and self.selling is None and not self.news and not self.filings

    def as_dict(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "buying": self.buying.as_dict() if self.buying else None,
                "selling": self.selling.as_dict() if self.selling else None, "news": self.news,
                "filings": list(self.filings)}


def nudge(side: Side, signals: SymbolSignals, s: BoostSettings) -> Tuple[float, List[str]]:
    """How much the signals move a play on ``side``, and why."""
    long = side is Side.LONG
    delta, why = 0.0, []
    if signals.buying is not None and signals.buying.unusual:
        d = s.insider_buying * signals.buying.score * (1 if long else -1)
        delta += d
        why.append(f"unusual insider buying ({d:+.3f})")
    if signals.selling is not None and signals.selling.unusual:
        d = -s.insider_selling * signals.selling.score if long else 0.5 * s.insider_selling * signals.selling.score
        delta += d
        why.append(f"unusual insider selling ({d:+.3f})")
    news = signals.news or {}
    if news.get("headlines", 0) >= s.min_headlines:
        d = s.news * float(news["score"]) * (1 if long else -1)
        if abs(d) >= 0.001:
            delta += d
            why.append(f"news sentiment {float(news['score']):+.2f} ({d:+.3f})")
    return round(delta, 4), why


class SignalBook:
    """Shared by the signals service (which fills it), the scanner (score nudges and
    insider plays) and the dashboard."""

    def __init__(self, boosts: BoostSettings = BoostSettings()) -> None:
        self.boosts = boosts
        self._by_symbol: Dict[str, SymbolSignals] = {}
        self._stories: Dict[str, Tuple[dt.datetime, List[Dict[str, Any]]]] = {}
        self._earnings: Dict[str, List[Dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def set_insiders(self, found: Mapping[str, Tuple[Optional[InsiderSignal], Optional[InsiderSignal]]]) -> None:
        """Every stock's (buying, selling) signals at once; stocks left out have none any more."""
        with self._lock:
            for symbol, sig in list(self._by_symbol.items()):
                if symbol not in found:
                    sig.buying = sig.selling = None
            for symbol, (buying, selling) in found.items():
                sig = self._by_symbol.setdefault(symbol, SymbolSignals(symbol))
                sig.buying, sig.selling = buying, selling
            self._drop_empty()

    def set_news(self, symbol: str, news: Optional[Dict[str, Any]], filings: List[Dict[str, Any]]) -> None:
        with self._lock:
            sig = self._by_symbol.setdefault(symbol, SymbolSignals(symbol))
            sig.news, sig.filings = news, list(filings)
            self._drop_empty()

    def set_stories(self, stories: Mapping[str, List[Dict[str, Any]]], checked_at: dt.datetime) -> None:
        """Each followed stock's recent stories, as read at ``checked_at``; stocks left out aren't followed any more."""
        with self._lock:
            self._stories = {symbol: (checked_at, list(rows)) for symbol, rows in stories.items()}

    def news_reading(self, symbol: str) -> Optional[Dict[str, Any]]:
        """When the stock's news was last read and the stories found - None when it isn't followed."""
        with self._lock:
            found = self._stories.get(symbol)
        return {"checked_at": found[0], "stories": found[1]} if found else None

    def set_earnings(self, by_symbol: Mapping[str, List[Dict[str, Any]]]) -> None:
        """Every stock's earnings reports from the calendar (signals/calendar.py)."""
        with self._lock:
            self._earnings = dict(by_symbol)

    def earnings_for(self, symbol: str) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._earnings.get(symbol, []))

    def get(self, symbol: str) -> Optional[SymbolSignals]:
        with self._lock:
            return self._by_symbol.get(symbol)

    def all(self) -> List[SymbolSignals]:
        with self._lock:
            return list(self._by_symbol.values())

    def unusual_buying_symbols(self) -> List[str]:
        """Stocks with unusual insider buying, strongest first."""
        with self._lock:
            found = [s for s in self._by_symbol.values() if s.buying is not None and s.buying.unusual]
        return [s.symbol for s in sorted(found, key=lambda s: -s.buying.score)]

    def apply(self, play: Play) -> None:
        """Nudge ``play``'s score by its stock's signals and note why in its evidence."""
        signals = self.get(play.symbol)
        if signals is None:
            return
        delta, why = nudge(play.side, signals, self.boosts)
        if why:
            play.score = round(max(0.0, play.score + delta), 4)
            play.evidence["signal_nudge"] = delta
            play.evidence["signal_reasons"] = "; ".join(why)

    def _drop_empty(self) -> None:
        self._by_symbol = {k: v for k, v in self._by_symbol.items() if not v.empty}
