"""A session's biggest movers across the whole market: why each one moved, and what the bot made of it.

The journal reviews what the bot did. This part of the report looks at the market the other
way round: every stock the scanner can trade, ranked by how far it moved in the session. For
each of the biggest movers it asks two things.

- **Why it moved.** The candidates are earnings or another material 8-K filing, an analyst
  action, a news story, or its whole sector moving with it. It also asks how it moved: a gap
  at the open or a climb during the session, how much more volume than usual, and whether it
  closed at a 20-day high or low.
- **What the bot made of it.** Either the bot traded it (with the move or against it), sent an
  entry that never filled, offered a setup that wasn't taken (and how that setup would have
  gone), watched it without finding a setup, or never looked. When it never looked, the report
  says why: the morning's ranking put it too far down, it was too thin for the scanner's
  filters, or its sector is switched off.

Across many sessions, the share of movers the morning watchlist held shows how well the
pre-market scan picks the day's stocks. The number of moves that began before the open shows
how many it couldn't have seen: the morning scan only reads the previous day's candles.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from statistics import median
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import numpy as np
import pandas as pd

from ..data.sectors import sector_allowed
from ..scanner.heat import DailyMetrics, daily_metrics, liquid, rank_by_daily_heat
from ..scanner.watchlist import DayWatchlist
from ..signals.news import is_material
from ..util import clock
from .journal import SENT_UNFILLED

MIN_HISTORY = 30                  # daily candles before the session, as the scan's own ranking needs
SECTOR_MIN_STOCKS = 5             # a sector's median move means little over fewer
SECTOR_SHARE = 0.5                # a sector moving at least half as far the same way carried the stock
SECTOR_FLOOR_PCT = 1.0
BEFORE_OPEN_SHARE = 0.6           # this much of the move already in the opening price: it came before the bell
HEAVY_VOLUME = 2.0
MAX_STORIES = 12
ROLLING_SESSIONS = 20
_EARNINGS = re.compile(r"\b(earnings|quarter(ly)?|Q[1-4]|EPS|revenue|guidance|results|beats?|miss(es|ed)?)\b", re.I)
_KIND_ORDER = {"earnings": 0, "filing": 1, "analyst": 2, "news": 3}


@dataclass
class Move:
    symbol: str
    sector: str
    close: float
    prev_close: float
    change_pct: float
    gap_pct: float                # the opening price against the previous close
    session_pct: float            # the close against the opening price
    rvol: float                   # the session's volume against its 20-session average
    move_atr: float               # the move in average true ranges (of the 14 sessions before)
    atr_pct: float                # that average true range, % of the previous close
    dollar_volume: float          # the session's
    extreme: str = ""             # "high" | "low": closed beyond its 20-session range


@dataclass
class Universe:
    """One pass over every stock's daily candles: each one's move in the session, and the
    ranking the morning's scan made from the candles before it."""

    day: dt.date
    moves: List[Move] = field(default_factory=list)
    ranked: List[DailyMetrics] = field(default_factory=list)    # hottest first
    thin: Set[str] = field(default_factory=set)                 # failed the scanner's filters that morning
    filtered: Set[str] = field(default_factory=set)             # liquid, but in a sector switched off
    scanned: int = 0
    market_pct: Optional[float] = None
    sectors: Dict[str, Tuple[float, int]] = field(default_factory=dict)   # median change %, stocks

    def rank_of(self) -> Dict[str, int]:
        return {m.symbol: i for i, m in enumerate(self.ranked, 1)}


def _pct(a: float, b: float) -> float:
    return (a / b - 1.0) * 100.0 if b else 0.0


def session_move(symbol: str, sector: str, frame: pd.DataFrame) -> Optional[Move]:
    """The move in the last candle of ``frame``, against the candles before it."""
    tail = frame.iloc[-(MIN_HISTORY + 1):]
    if len(tail) < MIN_HISTORY + 1:
        return None
    o, h, l, c, v = (tail[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close", "volume"))
    prev = c[:-1]
    true_range = np.maximum(h[1:] - l[1:], np.maximum(abs(h[1:] - prev), abs(l[1:] - prev)))
    atr, avg_volume = true_range[-15:-1].mean(), v[-21:-1].mean()
    if c[-2] <= 0 or o[-1] <= 0 or atr <= 0 or avg_volume <= 0:
        return None
    hi, lo = h[-21:-1].max(), l[-21:-1].min()
    close, prev_close, open_ = float(c[-1]), float(c[-2]), float(o[-1])
    return Move(symbol=symbol, sector=sector, close=round(close, 4), prev_close=round(prev_close, 4),
                change_pct=round(_pct(close, prev_close), 2), gap_pct=round(_pct(open_, prev_close), 2),
                session_pct=round(_pct(close, open_), 2), rvol=round(float(v[-1] / avg_volume), 2),
                move_atr=round(float(abs(close - prev_close) / atr), 2),
                atr_pct=round(float(atr / prev_close * 100.0), 2), dollar_volume=round(float(close * v[-1]), 0),
                extreme="high" if c[-1] > hi else "low" if c[-1] < lo else "")


def _tradable_on_the_day(m: Move, prefilter: Mapping[str, float]) -> bool:
    low, high = sorted((m.prev_close, m.close))
    return (high >= prefilter.get("min_price", 0.0) and low <= prefilter.get("max_price", float("inf"))
            and m.dollar_volume >= prefilter.get("min_dollar_volume", 0.0))


def read_session(frames: Iterable[Tuple[str, Optional[pd.DataFrame]]], day: dt.date, *,
                 sector_of: Callable[[str], str], prefilter: Mapping[str, float],
                 sectors_allowed: Optional[Iterable[str]], benchmark: str = "SPY") -> Universe:
    """Reads one stock at a time, so the whole market never sits in memory."""
    out = Universe(day)
    oldest_morning = clock.prev_trading_day(clock.prev_trading_day(day))   # the scan allows one missing session
    allowed = tuple(sectors_allowed or ())
    for symbol, frame in frames:
        if frame is None or len(frame) <= MIN_HISTORY:
            continue
        at = np.flatnonzero(frame.index.date == day)
        if not len(at) or at[0] < MIN_HISTORY:
            continue
        upto = frame.iloc[:int(at[0]) + 1]
        if symbol == benchmark:
            out.market_pct = round(_pct(float(upto["close"].iloc[-1]), float(upto["close"].iloc[-2])), 2)
            continue
        out.scanned += 1
        sector = sector_of(symbol)
        before = upto.iloc[:-1]
        morning = daily_metrics(symbol, before) if before.index[-1].date() >= oldest_morning else None
        if morning is None or not liquid(morning, prefilter):
            out.thin.add(symbol)
        elif sector_allowed(sector, allowed):
            out.ranked.append(morning)
        else:
            out.filtered.add(symbol)
        move = session_move(symbol, sector, upto)
        if move is not None and _tradable_on_the_day(move, prefilter):
            out.moves.append(move)
    out.ranked = rank_by_daily_heat(out.ranked)
    by_sector: Dict[str, List[float]] = {}
    for m in out.moves:
        if m.sector:
            by_sector.setdefault(m.sector, []).append(m.change_pct)
    out.sectors = {s: (round(median(v), 2), len(v)) for s, v in by_sector.items() if len(v) >= SECTOR_MIN_STOCKS}
    return out


def top_movers(moves: Sequence[Move], per_side: int) -> Tuple[List[Move], List[Move]]:
    gainers = sorted((m for m in moves if m.change_pct > 0), key=lambda m: -m.change_pct)[:per_side]
    losers = sorted((m for m in moves if m.change_pct < 0), key=lambda m: m.change_pct)[:per_side]
    return gainers, losers


# ---------------------------------------------------------------- why it moved
def session_bounds(day: dt.date) -> Tuple[dt.datetime, dt.datetime, dt.datetime]:
    """The previous session's close, this session's open and its close, in UTC."""
    prev = clock.prev_trading_day(day)

    def utc(d: dt.date, t: dt.time) -> dt.datetime:
        return dt.datetime.combine(d, t, tzinfo=clock.NY).astimezone(dt.timezone.utc)
    return utc(prev, clock.regular_close_time(prev)), utc(day, dt.time(9, 30)), utc(day, clock.regular_close_time(day))


def _story_kind(row: Mapping[str, Any]) -> Optional[str]:
    items = {c.strip() for c in str(row.get("items") or "").split(",")}
    if row.get("kind") == "filing":
        if "2.02" in items:
            return "earnings"
        return "filing" if is_material(row.get("items") or "") else None
    if _EARNINGS.search(row.get("headline") or ""):
        return "earnings"
    return "analyst" if row.get("kind") == "analyst" else "news"


def explain(m: Move, stories: Optional[Sequence[Mapping[str, Any]]], sectors: Mapping[str, Tuple[float, int]],
            opened_at: dt.datetime) -> Dict[str, Any]:
    """``stories``: the session's news about the stock, oldest first - None when it couldn't be read."""
    reasons: List[str] = []
    before_open = abs(m.gap_pct) >= 1.0 and m.gap_pct * m.change_pct > 0 and \
        abs(m.gap_pct) >= BEFORE_OPEN_SHARE * abs(m.change_pct)
    if before_open:
        reasons.append(f"gapped {m.gap_pct:+.1f}% at the open, then {m.session_pct:+.1f}% during the session")
    else:
        reasons.append(f"moved {m.session_pct:+.1f}% during the session, after opening {m.gap_pct:+.1f}%")
    reasons.append(f"{m.rvol:.1f}x its usual volume" + (" - heavy buying and selling" if m.rvol >= HEAVY_VOLUME else ""))
    reasons.append(f"{m.move_atr:.1f} times its usual daily range of {m.atr_pct:.1f}%")
    if m.extreme:
        reasons.append(f"closed at a 20-day {m.extreme}")
    sector_pct, sector_n = sectors.get(m.sector, (None, 0))
    with_sector = bool(sector_pct is not None and sector_pct * m.change_pct > 0
                       and abs(sector_pct) >= max(SECTOR_FLOOR_PCT, SECTOR_SHARE * abs(m.change_pct)))
    if sector_pct is not None:
        reasons.append(f"its sector ({m.sector}) moved {sector_pct:+.1f}% (the median of {sector_n} stocks)"
                       + (" - it moved with it" if with_sector else ""))

    rows = []
    for s in stories or []:
        kind = _story_kind(s)
        if kind is None:
            continue
        at = dt.datetime.fromisoformat(s["published_at"])
        rows.append({"at": at.isoformat(), "before_open": at < opened_at, "kind": kind, "source": s.get("source"),
                     "provider": s.get("provider"), "headline": s.get("headline"), "url": s.get("url") or "",
                     "sentiment": s.get("sentiment")})
    lead = min(rows, key=lambda r: (_KIND_ORDER[r["kind"]], r["at"])) if rows else None
    if lead:
        catalyst = {"kind": lead["kind"], "label": lead["headline"], "before_open": lead["before_open"]}
    elif with_sector:
        catalyst = {"kind": "sector", "label": f"{m.sector} moved {sector_pct:+.1f}%", "before_open": False}
    elif stories is None:
        catalyst = {"kind": "unchecked", "label": "the news wasn't read", "before_open": False}
    else:
        catalyst = {"kind": "none", "label": "no news found", "before_open": False}
    return {"catalyst": catalyst, "reasons": reasons, "before_open": before_open, "with_sector": with_sector,
            "stories": rows[-MAX_STORIES:]}


# ---------------------------------------------------------------- what the bot made of it
def _utc_iso(stamp: Optional[str]) -> Optional[str]:
    """The tables keep naive UTC times."""
    if not stamp:
        return None
    t = dt.datetime.fromisoformat(stamp)
    return (t.replace(tzinfo=dt.timezone.utc) if t.tzinfo is None else t).isoformat()


def _sent_at(play: Mapping[str, Any]) -> Optional[str]:
    """When a play's entry went out, in UTC: the moment it was approved, kept in its evidence - the row
    itself can have been written a scan or two before. The row's own time when that wasn't kept."""
    try:
        at = _utc_iso(((play.get("evidence") or {}).get("at_entry") or {}).get("at"))
        return dt.datetime.fromisoformat(at).astimezone(dt.timezone.utc).isoformat() if at \
            else _utc_iso(play.get("created_at"))
    except (TypeError, ValueError):
        return _utc_iso(play.get("created_at"))


def _sent_label(p: Mapping[str, Any]) -> str:
    """'gap and go long at 98.65, 10:10 ET'"""
    at = dt.datetime.fromisoformat(p["sent_at"]).astimezone(clock.NY).strftime(", %H:%M ET") if p["sent_at"] else ""
    price = f" at {p['entry']:.2f}" if p["entry"] is not None else ""
    return f"{p['strategy'].replace('_', ' ')} {p['side'].lower()}{price}{at}"


def _best(rows: Sequence[Mapping[str, Any]]) -> Optional[float]:
    """The best of what the plays followed on the candles would have made, None when none would have filled."""
    return max((p["shadow_r"] for p in rows if p["shadow_filled"]), default=None)


def morning_watchlist(universe: Universe, sector_of: Callable[[str], str], hot_size: int,
                      queue_size: int) -> Dict[str, Tuple[str, str, int]]:
    """Where the morning's scan put each stock: symbol -> ("hot" | "buffer", sector, place)."""
    wl = DayWatchlist.build(universe.day, clock.prev_trading_day(universe.day), universe.ranked, sector_of,
                            hot_size, queue_size, universe=universe.scanned, liquid=len(universe.ranked))
    out = {c.symbol: ("hot", c.sector, i) for i, c in enumerate(wl.hot, 1)}
    for sector, queue in wl.queues.items():
        out.update({c.symbol: ("buffer", sector, i) for i, c in enumerate(queue, 1)})
    return out


def _watched(symbol: str, morning: Optional[Tuple[str, str, int]], saved: Optional[Mapping[str, Any]]) -> Optional[str]:
    """How the day's watchlist treated the stock, or None when it wasn't on it."""
    if saved:
        decisions = [d for d in saved.get("decisions") or [] if d.get("symbol") == symbol or d.get("replaced") == symbol]
        if any(c.get("symbol") == symbol for c in saved.get("hot") or []):
            adopted = next((d for d in decisions if d.get("action") == "adopted" and d.get("symbol") == symbol), None)
            return "adopted into the hot list during the day" if adopted or (morning and morning[0] != "hot") \
                else "on the hot list"
        for sector, queue in (saved.get("kept") or {}).items():
            if any(c.get("symbol") == symbol for c in queue):
                return f"scanned from the {sector} buffer and kept"
        for sector, queue in (saved.get("queues") or {}).items():
            place = next((i for i, c in enumerate(queue, 1) if c.get("symbol") == symbol), None)
            if place:
                return f"waiting in the {sector} buffer - the cycles never reached it (#{place} of {len(queue)} left)"
        if morning and morning[0] == "hot":
            return "on the morning's hot list until a hotter stock replaced it"
        if morning or decisions:
            return f"scanned from the {morning[1] if morning else decisions[0].get('sector')} buffer"
        return None
    if morning:
        return "on the morning's hot list" if morning[0] == "hot" else f"#{morning[2]} in the {morning[1]} buffer"
    return None


def involvement(m: Move, *, trades: Sequence[Mapping[str, Any]], plays: Sequence[Mapping[str, Any]],
                shadows: Sequence[Mapping[str, Any]], morning: Optional[Tuple[str, str, int]],
                saved: Optional[Mapping[str, Any]], rank: Optional[int], ranked: int, thin: bool, filtered: bool,
                active: bool, prefilter: Mapping[str, float]) -> Dict[str, Any]:
    """Traded, sent, offered, watched, or missed - and the details behind it."""
    watched = _watched(m.symbol, morning, saved)
    direction = "LONG" if m.change_pct > 0 else "SHORT"
    trade_rows = [{"side": t["side"], "strategy": t["strategy"], "entry_time": _utc_iso(t.get("entry_time")),
                   "entry": t.get("entry_price"), "exit_time": _utc_iso(t.get("exit_time")), "exit": t.get("exit_price"),
                   "r": t.get("r_multiple"), "pl": t.get("realized_pl"), "status": t.get("status"),
                   "with_move": t["side"] == direction} for t in trades]
    booked = {t.get("play_id") for t in trades}
    shadow_by_play = {s.get("play_id"): s for s in shadows}
    seen: Set[Tuple[str, str]] = set()
    play_rows, senders = [], set()
    for p in plays:
        # each setup once, where it was first seen - and every entry of it that went out and never
        # filled, which the setup's first sighting would otherwise hide
        went_out = p.get("status") in SENT_UNFILLED and p.get("id") not in booked
        if (p["strategy"], p["side"]) in seen and not went_out:
            continue
        seen.add((p["strategy"], p["side"]))
        if went_out:
            senders.add(p.get("decided_by") or "")
        shadow = shadow_by_play.get(p.get("id")) or {}
        # the review books an entry sent and never filled as no fill; taken as planned is what it would have
        # made had it filled (reviews saved before kept that as its result)
        planned = shadow.get("if_filled_r")
        play_rows.append({"side": p["side"], "strategy": p["strategy"], "seen_at": _utc_iso(p.get("created_at")),
                          "entry": p.get("entry"), "stop": p.get("stop"), "timeframe": p.get("timeframe"),
                          "status": p.get("status"), "with_move": p["side"] == direction,
                          "sent": went_out, "sent_at": _sent_at(p) if went_out else None,
                          "shadow_r": shadow.get("r") if planned is None else planned,
                          "shadow_filled": True if planned is not None else shadow.get("filled")})
    out: Dict[str, Any] = {"watched": watched, "trades": trade_rows, "plays": play_rows,
                           "rank": rank, "ranked": ranked}
    if trade_rows:
        closed = [t for t in trade_rows if t["r"] is not None]
        out.update(status="traded", r=round(sum(t["r"] for t in closed), 2) if closed else None,
                   pl=round(sum(float(t["pl"] or 0.0) for t in trade_rows), 2),
                   detail="; ".join(f"{t['side'].lower()} ({'with' if t['with_move'] else 'against'} the move)"
                                    for t in trade_rows))
        return out
    sent = [p for p in play_rows if p["sent"]]
    if sent:
        # an order went out and nothing came of it: not the same as a setup nobody acted on
        setups = {(p["strategy"], p["side"]) for p in sent}
        others = [p for p in play_rows if (p["strategy"], p["side"]) not in setups]
        best, other_best = _best(sent), _best(others)
        n = len(sent)
        lead = f"Autopilot sent {n} entr{'y' if n == 1 else 'ies'} that never filled" if senders == {"autopilot"} \
            else f"{n} entr{'y was' if n == 1 else 'ies were'} sent and never filled"
        out.update(status="sent", r=best, detail=(
            f"{lead} ({'; '.join(_sent_label(p) for p in sent)})"
            + (f" - taken as planned {'it' if n == 1 else 'the best'} would have made {best:+.2f}R"
               if best is not None else "")
            + (f"; {len(others)} other setup{'s' if len(others) != 1 else ''} offered"
               + (f" - the best would have made {other_best:+.2f}R" if other_best is not None else "")
               if others else "")))
        return out
    if play_rows:
        best = _best(play_rows)
        out.update(status="offered", r=best, detail=(
            f"{len(play_rows)} setup{'s' if len(play_rows) != 1 else ''} offered, none taken"
            + (f" - the best would have made {best:+.2f}R" if best is not None
               else " - the plays not taken weren't followed" if not shadows else "")))
        return out
    if not active:
        where = "" if morning is None else " (the morning's ranking would have put it on the hot list)" \
            if morning[0] == "hot" else f" (the morning's ranking would have put it in the {morning[1]} buffer)"
        out.update(status="offline", watched=None, detail="the app wasn't scanning this session" + where)
        return out
    if watched:
        out.update(status="watched", detail=watched + " - no setup triggered")
        return out
    if filtered:
        detail = f"its sector ({m.sector or 'unknown'}) is switched off in the scanner's filters"
    elif thin:
        detail = ("too thin or too quiet for the scanner's filters the day before (at least "
                  f"${prefilter.get('min_dollar_volume', 0) / 1e6:,.0f}M a day, a ${prefilter.get('min_price', 0):g} price "
                  f"and a {prefilter.get('min_atr_pct', 0):g}% daily range) - it only qualified on the day's own trading")
    elif rank:
        detail = f"the morning's ranking put it #{rank} of {ranked} - too far down for the hot list and its sector's buffer"
    else:
        detail = "not in the morning's ranking (no sector known, or its candles were out of date)"
    out.update(status="missed", detail=detail)
    return out


# ---------------------------------------------------------------- the section
def build_movers(universe: Universe, *, per_side: int, sector_of: Callable[[str], str],
                 news: Optional[Mapping[str, Sequence[Mapping[str, Any]]]], trades: Sequence[Mapping[str, Any]],
                 plays: Sequence[Mapping[str, Any]], shadows: Sequence[Mapping[str, Any]],
                 saved_watchlist: Optional[Mapping[str, Any]], hot_size: int, queue_size: int,
                 prefilter: Mapping[str, float], news_note: str = "") -> Dict[str, Any]:
    """``news``: stories per stock, oldest first - None when the news couldn't be read."""
    gainers, losers = top_movers(universe.moves, per_side)
    _, opened_at, _ = session_bounds(universe.day)
    morning = morning_watchlist(universe, sector_of, hot_size, queue_size)
    ranks, ranked = universe.rank_of(), len(universe.ranked)
    active = bool(saved_watchlist or plays or trades)

    def row(m: Move) -> Dict[str, Any]:
        stories = None if news is None else news.get(m.symbol, [])
        return {**asdict(m), **explain(m, stories, universe.sectors, opened_at),
                "bot": involvement(m, trades=[t for t in trades if t["symbol"] == m.symbol],
                                   plays=[p for p in plays if p["symbol"] == m.symbol],
                                   shadows=[s for s in shadows if s.get("symbol") == m.symbol],
                                   morning=morning.get(m.symbol), saved=saved_watchlist, rank=ranks.get(m.symbol),
                                   ranked=ranked, thin=m.symbol in universe.thin,
                                   filtered=m.symbol in universe.filtered, active=active, prefilter=prefilter)}

    rows = {"gainers": [row(m) for m in gainers], "losers": [row(m) for m in losers]}
    everyone = rows["gainers"] + rows["losers"]
    summary = capture(everyone, morning)
    return {
        "ok": True, "session": universe.day.isoformat(), "built_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "market_pct": universe.market_pct, "stocks": len(universe.moves), "scanned": universe.scanned,
        "sectors": dict(sorted(universe.sectors.items(), key=lambda kv: -abs(kv[1][0]))),
        **rows, "summary": summary, "news_note": news_note,
        "lessons": mover_lessons(summary, everyone, active, prefilter),
    }


def capture(rows: Sequence[Mapping[str, Any]], morning: Mapping[str, Tuple[str, str, int]]) -> Dict[str, Any]:
    status = Counter(r["bot"]["status"] for r in rows)
    traded = [r["bot"] for r in rows if r["bot"]["status"] == "traded"]
    offered = [r["bot"]["r"] for r in rows if r["bot"]["status"] == "offered" and r["bot"]["r"] is not None]
    return {
        "movers": len(rows), "traded": status["traded"], "sent": status["sent"], "offered": status["offered"],
        "watched": status["watched"], "missed": status["missed"], "offline": status["offline"],
        "traded_r": round(sum(b["r"] or 0.0 for b in traded), 2), "traded_pl": round(sum(b["pl"] or 0.0 for b in traded), 2),
        "offered_r": round(sum(offered), 2) if offered else None,
        "in_watchlist": sum(1 for r in rows if r["bot"]["watched"] or r["symbol"] in morning),
        "on_hot_list": sum(1 for r in rows if (morning.get(r["symbol"]) or ("",))[0] == "hot"),
        "thin": sum(1 for r in rows if r["bot"]["status"] == "missed" and r["bot"]["detail"].startswith("too thin")),
        "before_open": sum(1 for r in rows if r["before_open"]),
        "news": sum(1 for r in rows if r["catalyst"]["kind"] in _KIND_ORDER),
        "news_before_open": sum(1 for r in rows if r["before_open"] and r["catalyst"]["kind"] in _KIND_ORDER
                                and r["catalyst"]["before_open"]),
        "with_sector": sum(1 for r in rows if r["with_sector"]),
        "quiet": sum(1 for r in rows if r["catalyst"]["kind"] == "none"),
    }


def mover_lessons(s: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], active: bool,
                  prefilter: Mapping[str, float]) -> List[str]:
    n = s["movers"]
    if not n:
        return ["No stock the scanner could trade moved enough to list."]
    out: List[str] = []
    if not active:
        out.append(f"The app wasn't scanning this session, so this is the market's view only: the morning's ranking "
                   f"would have put {s['in_watchlist']} of the {n} biggest movers on the watchlist "
                   f"({s['on_hot_list']} on the hot list).")
    else:
        out.append(f"The morning's watchlist held {s['in_watchlist']} of the {n} biggest movers ({s['on_hot_list']} on "
                   f"the hot list). The bot traded {s['traded']} of them"
                   + (f" for {s['traded_r']:+.2f}R." if s["traded"] else "."))
    if s["offered"]:
        out.append(f"{s['offered']} had setups offered that weren't taken"
                   + (f"; taken as planned the best of each would have made {s['offered_r']:+.2f}R in all." if s["offered_r"]
                      is not None else "."))
    if s["sent"]:
        out.append(f"{s['sent']} had an entry sent that never filled.")
    against = [r["symbol"] for r in rows for t in r["bot"]["trades"] if not t["with_move"]]
    if against:
        out.append(f"Traded against the day's move: {', '.join(dict.fromkeys(against))}. A stock moving this hard on "
                   "heavy volume rarely turns the same day.")
    if s["before_open"] >= max(2, math.ceil(n / 3)):
        out.append(f"{s['before_open']} of the {n} made most of their move at the open, {s['news_before_open']} with news "
                   "out before the bell. The morning scan ranks on the previous day's candles, so a move that starts "
                   "overnight is out of its sight until the first cycle.")
    if s["thin"]:
        out.append(f"{s['thin']} {'were' if s['thin'] != 1 else 'was'} too thin for the scanner's filters the day before "
                   f"(${prefilter.get('min_dollar_volume', 0) / 1e6:,.0f}M a day) and only qualified on the day's own "
                   "volume - typical of news-driven small caps.")
    if s["with_sector"] >= 3:
        out.append(f"{s['with_sector']} moved with their whole sector - a sector move, not the company's own news.")
    if s["news"] and s["quiet"] >= math.ceil(n / 2):
        out.append(f"{s['quiet']} moved with no news found and no sector move behind them - often a story outside the news "
                   "feeds this account reads, or plain order flow.")
    return out


def rolling_capture(reviews: Iterable[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """How the watchlist has done against the movers over the recent sessions with a movers report
    and the app scanning."""
    got: List[Mapping[str, Any]] = []
    for r in reviews:
        s = ((r.get("movers") or {}).get("summary")) or {}
        if s.get("movers") and not s.get("offline"):
            got.append(s)
            if len(got) == ROLLING_SESSIONS:
                break
    movers = sum(s["movers"] for s in got)
    if not movers:
        return None
    return {"sessions": len(got), "movers": movers,
            "in_watchlist": round(sum(s["in_watchlist"] for s in got) / movers, 3),
            "traded": round(sum(s["traded"] for s in got) / movers, 3),
            "before_open": round(sum(s["before_open"] for s in got) / movers, 3),
            "traded_r": round(sum(s["traded_r"] for s in got), 2)}
