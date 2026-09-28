"""Where the signals are kept: insider trades, the SEC filings already read (so none
is fetched twice), and company news with its sentiment."""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import func, select

from ..persistence.db import session_scope
from ..persistence.models_orm import FilingRead, InsiderTradeLog, NewsLog
from .edgar import FilingRef
from .form4 import BUY, InsiderTrade
from .news import NewsItem

_TRADE_FIELDS = ("accession", "line", "symbol", "issuer_cik", "issuer_name", "owner_cik", "owner_name", "role",
                 "title", "code", "trade_date", "shares", "price", "shares_after", "planned", "direct", "offering")
_CHUNK = 500                       # keys per IN (...) - SQLite caps a statement's parameters


class SignalStore:
    # ---- insider filings ----------------------------------------------------- #
    def unread(self, refs: Sequence[FilingRef]) -> List[FilingRef]:
        """The filings in ``refs`` that haven't been read yet."""
        done = set()
        keys = list({r.accession for r in refs})
        with session_scope() as s:
            for i in range(0, len(keys), _CHUNK):
                done.update(s.scalars(select(FilingRead.accession).where(FilingRead.accession.in_(keys[i:i + _CHUNK]))))
        return [r for r in refs if r.accession not in done]

    def save_filing(self, ref: FilingRef, trades: Sequence[InsiderTrade]) -> None:
        """A filing's trades, and that it was read - even when it had none."""
        with session_scope() as s:
            for t in trades:
                s.merge(InsiderTradeLog(**{f: getattr(t, f) for f in _TRADE_FIELDS}, filed=ref.filed))
            s.merge(FilingRead(accession=ref.accession, form=ref.form, filed=ref.filed, trades=len(trades)))

    def insider_trades(self, since: dt.date, symbols: Optional[Iterable[str]] = None) -> List[InsiderTrade]:
        q = select(InsiderTradeLog).where(InsiderTradeLog.trade_date >= since)
        with session_scope() as s:
            if symbols is None:
                return [_trade(r) for r in s.scalars(q)]
            wanted = list(dict.fromkeys(symbols))
            return [_trade(r) for i in range(0, len(wanted), _CHUNK)
                    for r in s.scalars(q.where(InsiderTradeLog.symbol.in_(wanted[i:i + _CHUNK])))]

    def insider_filings(self, since: dt.date, symbols: Optional[Iterable[str]] = None,
                        limit: int = 500) -> List[Dict[str, Any]]:
        """Insider trades filed since ``since``, the latest filings first, each with the day it was
        filed - for the dashboard."""
        q = (select(InsiderTradeLog).where(InsiderTradeLog.filed >= since)
             .order_by(InsiderTradeLog.filed.desc(), InsiderTradeLog.trade_date.desc()).limit(limit))
        if symbols is not None:
            q = q.where(InsiderTradeLog.symbol.in_(list(dict.fromkeys(symbols))[:_CHUNK]))
        with session_scope() as s:
            return [{**_trade(r).as_row(), "filed": r.filed.isoformat() if r.filed else None} for r in s.scalars(q)]

    def symbols_traded(self, since: dt.date) -> List[str]:
        """Stocks with insider trades on or after ``since``."""
        with session_scope() as s:
            return sorted(set(s.scalars(select(InsiderTradeLog.symbol).where(InsiderTradeLog.trade_date >= since))))

    def recent_buyers(self, since: dt.date) -> Dict[str, int]:
        """Stocks whose insiders bought since ``since`` - outside 10b5-1 plans and offerings -
        with their companies' CIKs."""
        q = (select(InsiderTradeLog.symbol, InsiderTradeLog.issuer_cik)
             .where(InsiderTradeLog.code == BUY, InsiderTradeLog.planned.is_(False),
                    InsiderTradeLog.offering.is_(False), InsiderTradeLog.trade_date >= since).distinct())
        with session_scope() as s:
            return {symbol: int(cik or 0) for symbol, cik in s.execute(q)}

    # ---- news -------------------------------------------------------------- #
    def save_news(self, items: Sequence[NewsItem]) -> int:
        """Keeps the stories not kept yet; returns how many were new."""
        by_key = {i.key: i for i in items}
        if not by_key:
            return 0
        with session_scope() as s:
            have = set(s.scalars(select(NewsLog.key).where(NewsLog.key.in_(list(by_key)))))
            for key, i in by_key.items():
                if key not in have:
                    s.add(NewsLog(key=key, symbol=i.symbol, source=i.source, provider=i.provider[:40], kind=i.kind,
                                  headline=i.headline[:500], url=i.url[:500], ref=i.ref[:80], items=i.items[:60],
                                  published_at=_naive_utc(i.published_at)))
        return len(by_key) - len(have)

    def news(self, symbols: Optional[Iterable[str]], since: dt.datetime,
             limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Stories about ``symbols`` (every stock: None) published since ``since``, newest first."""
        q = select(NewsLog).where(NewsLog.published_at >= _naive_utc(since)).order_by(NewsLog.published_at.desc())
        if symbols is not None:
            q = q.where(NewsLog.symbol.in_(list(symbols)))
        with session_scope() as s:
            return [_news_row(r) for r in s.scalars(q.limit(limit) if limit else q)]

    def sentiment_counts(self, since: dt.datetime) -> Dict[str, int]:
        """Headlines published since ``since`` and how many of them have a sentiment. Filings aren't
        scored, so they aren't counted."""
        q = select(func.count(), func.count(NewsLog.sentiment)).where(
            NewsLog.published_at >= _naive_utc(since), NewsLog.kind != "filing")
        with session_scope() as s:
            total, scored = s.execute(q).one()
        return {"headlines": int(total or 0), "scored": int(scored or 0)}

    def unscored_news(self, limit: int = 200) -> List[Tuple[str, str]]:
        """(key, headline) of the newest headlines without a sentiment yet. Filings are
        described by their items, not written as prose, so they aren't scored."""
        with session_scope() as s:
            rows = s.scalars(select(NewsLog).where(NewsLog.sentiment.is_(None), NewsLog.kind != "filing")
                             .order_by(NewsLog.published_at.desc()).limit(limit))
            return [(r.key, r.headline) for r in rows]

    def set_sentiment(self, scores: Dict[str, Tuple[float, float]]) -> None:
        """``scores``: key -> (sentiment from -1 to 1, the model's confidence)."""
        with session_scope() as s:
            for key, (score, confidence) in scores.items():
                row = s.get(NewsLog, key)
                if row is not None:
                    row.sentiment, row.sentiment_conf = float(score), float(confidence)


def _trade(r: InsiderTradeLog) -> InsiderTrade:
    row = {f: getattr(r, f) for f in _TRADE_FIELDS}
    row.update(shares=float(r.shares), price=float(r.price), shares_after=float(r.shares_after),
               issuer_cik=int(r.issuer_cik or 0), owner_cik=int(r.owner_cik or 0),
               planned=bool(r.planned), direct=bool(r.direct), offering=bool(r.offering))
    return InsiderTrade(**row)


def _news_row(r: NewsLog) -> Dict[str, Any]:
    return {"key": r.key, "symbol": r.symbol, "source": r.source, "provider": r.provider, "kind": r.kind,
            "headline": r.headline, "url": r.url, "ref": r.ref, "items": r.items,
            "published_at": r.published_at.replace(tzinfo=dt.timezone.utc).isoformat(),
            "sentiment": r.sentiment, "sentiment_conf": r.sentiment_conf}


def _naive_utc(when: dt.datetime) -> dt.datetime:
    return when.astimezone(dt.timezone.utc).replace(tzinfo=None) if when.tzinfo else when
