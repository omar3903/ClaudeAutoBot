"""The daily review: learning from each session's trades, mistakes and missed chances.

The trading books the strategies learn from all come back to the same habit. Aziz keeps
a journal of every trade; Douglas wants the rules followed and results judged over many
trades, never one; Chan calls paper and live trading the only true out-of-sample test of
a strategy. After the close the review gathers, for one session:

- **the trades** that closed, each with what it was taken on - its noise flags, the scans
  that confirmed it, the price's character, the volatility forecast, the market's regime -
  kept in the play's evidence when it was approved;
- **the mistakes** - rule breaks and patterns that cost money: a loss beyond the planned
  1R, a winner of 1R or more closed at a loss, a trade taken through a noise flag or before
  it was confirmed, going straight back into a stock that had just stopped out;
- **the plays that weren't taken** and how each would have gone, followed on the session's
  5-minute candles exactly the way the replay follows a trade - grouped by noise flag, so
  every check is tested on live plays every day, and by whether Autopilot's checks
  passed them;
- **each strategy's real record** over the last 20 sessions against its replay record,
  and the evidence weight that follows (research/weights.py);
- **lessons** - plain sentences drawn from all of it.

Reviews are kept in the database (daily_reviews) and as JSON files under data/journal/.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

import pandas as pd

from ..core.enums import Side, StrategyKind, Timeframe
from ..core.models import Play
from ..scanner.noise import LABELS as NOISE_LABELS
from ..util import clock
from .features import play_features
from .replay import ReplaySettings, shadow_trade

log = logging.getLogger(__name__)

NY = "America/New_York"
ROLLING_SESSIONS = 20
MAX_SHADOWS = 150
BEYOND_STOP_R = -1.2              # a loss this deep went past the stop
GAVE_BACK_R = 1.0                 # a trade up this much shouldn't end in a loss
DRIFT_R = 0.3                     # real results this far under the replay's are drifting
MIN_LIVE = 10
MIN_GROUP = 5                     # plays on each side before a comparison is told
TURBULENT = 0.7
TAKEN = frozenset({"ACCEPTED", "SUBMITTED", "WORKING", "PARTIAL", "FILLED"})
LABELS = {**NOISE_LABELS, "unconfirmed": "seen in only one scan"}


def review_day(now: Optional[dt.datetime] = None, review_at: str = "16:15") -> dt.date:
    """The session a review is due for: today once ``review_at`` (ET) has passed on a trading
    day, otherwise the session before."""
    now = now or clock.now_ny()
    today = now.date()
    try:
        hour, minute = (int(x) for x in str(review_at).split(":"))
        at = dt.time(hour, minute)
    except ValueError:
        at = dt.time(16, 15)
    if clock.is_trading_day(today) and now.time() >= at:
        return today
    return clock.prev_trading_day(today)


# ---------------------------------------------------------------- records
def _stats(values: Iterable[Optional[float]]) -> Dict[str, Any]:
    rs = [float(r) for r in values if r is not None]
    if not rs:
        return {"trades": 0}
    wins = [r for r in rs if r > 0]
    return {"trades": len(rs), "win_rate": round(len(wins) / len(rs), 3), "expectancy_r": round(sum(rs) / len(rs), 3),
            "total_r": round(sum(rs), 2), "best_r": round(max(rs), 2), "worst_r": round(min(rs), 2)}


def live_records(trades: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Each strategy's closed real trades in R, with the R multiples themselves."""
    by_strategy: Dict[str, List[float]] = {}
    for t in trades:
        if t.get("r_multiple") is not None:
            by_strategy.setdefault(t["strategy"], []).append(round(float(t["r_multiple"]), 4))
    return {key: {**_stats(rs), "r": rs} for key, rs in by_strategy.items()}


def _risk_per_share(t: Mapping[str, Any]) -> float:
    stop = t.get("initial_stop_price") or t.get("stop_price")
    return abs(float(t["entry_price"]) - float(stop)) if stop and t.get("entry_price") else 0.0


def _at_entry(t: Mapping[str, Any]) -> Dict[str, Any]:
    return (((t.get("play") or {}).get("evidence") or {}).get("at_entry")) or {}


def _in_r(t: Mapping[str, Any], key: str) -> Optional[float]:
    risk = _risk_per_share(t)
    return round(float(t[key]) / risk, 2) if risk and t.get(key) is not None else None


def trade_rows(trades: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for t in trades:
        play = t.get("play") or {}
        rows.append({
            "id": t["id"], "symbol": t["symbol"], "side": t["side"], "strategy": t["strategy"],
            "timeframe": t["timeframe"], "venue": t.get("broker"), "entry_time": t.get("entry_time"),
            "exit_time": t.get("exit_time"), "entry": t.get("entry_price"), "exit": t.get("exit_price"),
            "quantity": t.get("quantity"), "r": t.get("r_multiple"), "pl": t.get("realized_pl"),
            "exit_reason": t.get("exit_reason"), "mfe_r": _in_r(t, "mfe"), "mae_r": _in_r(t, "mae"),
            "rationale": play.get("rationale", ""),
            "evidence": {k: v for k, v in (play.get("evidence") or {}).items() if k != "spark"},
        })
    return rows


def opened_rows(trades: Sequence[Mapping[str, Any]], marks: Mapping[str, float]) -> List[Dict[str, Any]]:
    """The positions opened this session and what each was taken on. For the ones still open,
    ``marks`` (symbol -> the session's close, or the latest price) says where they stood at the
    review, in R and in money; a position opened and closed the same session carries its result."""
    rows = []
    for t in trades:
        play, risk = t.get("play") or {}, _risk_per_share(t)
        still = t.get("status") == "OPEN"
        entry, qty = float(t.get("entry_price") or 0.0), float(t.get("quantity") or 0.0)
        sign = 1.0 if t.get("side") == "LONG" else -1.0
        mark = float(marks[t["symbol"]]) if still and marks.get(t["symbol"]) else None
        rows.append({
            "id": t["id"], "symbol": t["symbol"], "side": t["side"], "strategy": t["strategy"],
            "timeframe": t["timeframe"], "venue": t.get("broker"), "entry_time": t.get("entry_time"),
            "entry": t.get("entry_price"), "quantity": t.get("initial_quantity") or t.get("quantity"),
            "stop": t.get("initial_stop_price") or t.get("stop_price"),
            "target": t.get("initial_target_price") or t.get("target_price"),
            "risk": round(risk * float(t.get("initial_quantity") or qty), 2) if risk else None,
            "still_open": still, "expected_exit_at": t.get("expected_exit_at"),
            "mark": mark, "open_r": round(sign * (mark - entry) / risk, 2) if mark and risk else None,
            "open_pl": round(sign * (mark - entry) * qty + float(t.get("banked_pl") or 0.0), 2) if mark else None,
            "r": None if still else t.get("r_multiple"), "pl": None if still else t.get("realized_pl"),
            "exit_reason": None if still else t.get("exit_reason"),
            "entry_slippage_bps": t.get("entry_slippage_bps"), "rationale": play.get("rationale", ""),
            "evidence": {k: v for k, v in (play.get("evidence") or {}).items() if k != "spark"},
        })
    return rows


# ---------------------------------------------------------------- mistakes
def find_mistakes(trades: Sequence[Mapping[str, Any]], *, skip_noise: Iterable[str], min_confirmations: int,
                  styles: Mapping[str, str]) -> List[Dict[str, Any]]:
    skip = set(skip_noise)
    out: List[Dict[str, Any]] = []

    def add(kind: str, severity: str, t: Mapping[str, Any], detail: str) -> None:
        out.append({"kind": kind, "severity": severity, "trade_id": t["id"], "symbol": t["symbol"],
                    "strategy": t["strategy"], "r": t.get("r_multiple"), "detail": detail})

    stopped_out: Dict[str, str] = {}
    for t in sorted(trades, key=lambda t: t.get("entry_time") or ""):
        r, entry, mfe_r = t.get("r_multiple"), _at_entry(t), _in_r(t, "mfe")
        if r is not None and r < BEYOND_STOP_R:
            add("loss_beyond_stop", "high", t, f"lost {r:+.2f}R - more than the 1R planned: the stop was gapped "
                                               "or the exit came late")
        if r is not None and r <= 0 and mfe_r is not None and mfe_r >= GAVE_BACK_R:
            add("gave_back_winner", "medium", t, f"was up {mfe_r:+.2f}R and still closed at {r:+.2f}R")
        flags = [f for f in entry.get("noise", []) if f in skip]
        if flags:
            add("took_noise", "medium", t, "taken while flagged: " + ", ".join(LABELS.get(f, f) for f in flags))
        if t.get("timeframe") == "INTRADAY" and entry and int(entry.get("confirmations") or 1) < min_confirmations:
            add("unconfirmed", "medium", t, f"taken after {int(entry.get('confirmations') or 1)} scan(s), before the "
                                            f"{min_confirmations} in a row a day trade needs")
        if t["symbol"] in stopped_out and (t.get("entry_time") or "") > stopped_out[t["symbol"]]:
            add("reentered_after_loss", "medium", t, "went back into a stock that had already lost that session")
        if r is not None and r < 0 and t.get("exit_time"):
            stopped_out[t["symbol"]] = min(stopped_out.get(t["symbol"], t["exit_time"]), t["exit_time"])
        turbulent = (entry.get("market_regime") or {}).get("p_turbulent")
        if styles.get(t["strategy"]) == "momentum" and turbulent is not None and turbulent >= TURBULENT:
            add("against_regime", "info", t, f"a momentum setup taken while the market was turbulent "
                                             f"(P {turbulent:.2f})")
        if entry.get("unproven"):
            add("unproven_strategy", "info", t, str(entry["unproven"]))
        if t.get("overdue_notified"):
            add("held_past_plan", "info", t, "held past the time it was expected to take")
        if str(t.get("exit_reason") or "").startswith("manual") and r is not None and -1.0 < r < 0:
            add("closed_by_hand", "info", t, f"closed by hand at {r:+.2f}R, before its stop or target")
    return out


# ---------------------------------------------------------------- plays not taken
def _key(row: Mapping[str, Any]) -> tuple:
    return row.get("symbol"), row.get("strategy"), row.get("side")


def first_sightings(plays: Sequence[Mapping[str, Any]], limit: int = MAX_SHADOWS) -> List[Mapping[str, Any]]:
    """Each day-trade setup the first time it was offered, when it was never taken - the
    highest-scoring ones when there are too many to follow."""
    traded = {_key(p) for p in plays if p.get("status") in TAKEN}
    seen: set = set()
    out = []
    for p in sorted(plays, key=lambda p: p.get("created_at") or ""):
        if p.get("timeframe") != "INTRADAY" or p.get("kind") != "TECHNICAL" or _key(p) in traded or _key(p) in seen:
            continue
        seen.add(_key(p))
        out.append(p)
    return sorted(out, key=lambda p: -float(p.get("score") or 0.0))[:limit]


def _play(row: Mapping[str, Any]) -> Optional[Play]:
    try:
        play = Play(symbol=row["symbol"], side=Side(row["side"]), strategy=row["strategy"],
                    kind=StrategyKind(row["kind"]), timeframe=Timeframe(row["timeframe"]), entry=float(row["entry"]),
                    stop=float(row["stop"]), targets=[float(t) for t in (row.get("targets") or [])])
    except (KeyError, TypeError, ValueError):
        return None
    play.noise = list(row.get("noise") or [])
    play.confirmations = int(row.get("confirmations") or 1)
    return play if play.targets else None


def _seen_at(row: Mapping[str, Any]) -> Optional[pd.Timestamp]:
    try:
        stamp = pd.Timestamp(row["created_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return (stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp).tz_convert(NY)


def _compare(flagged: Sequence[Mapping[str, Any]], rest: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    def avg(rows):
        return round(sum(r["r"] for r in rows) / len(rows), 3) if rows else None
    out = {"flagged": len(flagged), "rest": len(rest), "flagged_avg_r": avg(flagged), "rest_avg_r": avg(rest)}
    if len(flagged) < MIN_GROUP or len(rest) < MIN_GROUP:
        out["verdict"] = "too few plays to tell"
    elif out["flagged_avg_r"] < out["rest_avg_r"]:
        out["verdict"] = "helped - the plays it flags did worse"
    else:
        out["verdict"] = "cost - the plays it flags did as well or better"
    return out


def shadow_outcomes(plays: Sequence[Mapping[str, Any]], bars: Mapping[str, pd.DataFrame], day: dt.date,
                    settings: ReplaySettings, passes: Callable[[Mapping[str, Any]], bool]) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for row in first_sightings(plays):
        frame, play, seen_at = bars.get(row["symbol"]), _play(row), _seen_at(row)
        if frame is None or play is None or seen_at is None:
            continue
        session = frame[frame.index.date == day]
        features = play_features(row, now=seen_at)
        trade = shadow_trade(play, session, seen_at, settings, features=features) if len(session) else None
        rows.append({"play_id": row.get("id"), "symbol": play.symbol, "side": play.side.value, "strategy": play.strategy,
                     "timeframe": play.timeframe.value, "seen_at": seen_at.isoformat(), "noise": play.noise,
                     "confirmations": play.confirmations,
                     "score": row.get("score"), "confidence": row.get("confidence"),
                     "reward_risk": row.get("reward_risk"), "passed_checks": bool(passes(row)),
                     "filled": trade is not None, "r": trade.r if trade else None,
                     "mfe_r": trade.mfe_r if trade else None,
                     "entered_at": trade.entered_at if trade else None, "exited_at": trade.exited_at if trade else None,
                     "entry": trade.entry if trade else None, "exit": trade.exit if trade else None,
                     "exit_reason": trade.exit_reason if trade else "no fill - the price had already moved away",
                     "features": features})
    filled = [r for r in rows if r["filled"]]
    flags = sorted({f for r in filled for f in r["noise"]})
    by_noise = {f: _compare([r for r in filled if f in r["noise"]], [r for r in filled if f not in r["noise"]])
                for f in flags}
    if flags:
        by_noise["all_checks"] = _compare([r for r in filled if r["noise"]], [r for r in filled if not r["noise"]])
    ranked = sorted(filled, key=lambda r: r["r"])
    return {
        "followed": len(rows), "filled": len(filled), "summary": _stats(r["r"] for r in filled),
        "checks": {"passed": _stats(r["r"] for r in filled if r["passed_checks"]),
                   "turned_away": _stats(r["r"] for r in filled if not r["passed_checks"])},
        "by_noise": by_noise,
        "by_strategy": {k: _stats(r["r"] for r in filled if r["strategy"] == k) for k in sorted({r["strategy"] for r in filled})},
        "best_missed": [r for r in reversed(ranked) if r["r"] > 0][:5],
        "worst_avoided": [r for r in ranked if r["r"] < 0][:5],
        "plays": rows,
    }


# ---------------------------------------------------------------- strategies
def strategy_table(live: Mapping[str, Mapping[str, Any]], replay: Mapping[str, Mapping[str, Any]],
                   evidence: Mapping[str, Mapping[str, Any]], titles: Mapping[str, str]) -> List[Dict[str, Any]]:
    rows = []
    for key in sorted(set(live) | set(replay)):
        real, replayed = live.get(key) or {}, replay.get(key) or {}
        if not real.get("trades") and not replayed.get("trades"):
            continue
        row = {"strategy": key, "title": titles.get(key, key), "live_trades": real.get("trades", 0),
               "live_r": real.get("expectancy_r"), "replay_trades": replayed.get("trades", 0),
               "replay_r": replayed.get("expectancy_r"),
               "held_out_r": (replayed.get("out_of_sample") or {}).get("expectancy_r"),
               "evidence_weight": (evidence.get(key) or {}).get("multiplier", 1.0)}
        row["drifting"] = bool(row["live_trades"] >= MIN_LIVE and row["replay_trades"]
                               and row["live_r"] < row["replay_r"] - DRIFT_R)
        rows.append(row)
    return rows


# ---------------------------------------------------------------- lessons
def lessons(day: Mapping[str, Any], mistakes: Sequence[Mapping[str, Any]], shadows: Mapping[str, Any],
            strategies: Sequence[Mapping[str, Any]], regime: Optional[Mapping[str, Any]], titles: Mapping[str, str],
            breakeven_at_r: float) -> List[str]:
    out: List[str] = []
    n = day.get("trades", 0)
    if n:
        out.append(f"{n} trade{'s' if n != 1 else ''} closed: {day['total_r']:+.2f}R in all, {day['expectancy_r']:+.2f}R "
                   f"a trade, {round(100 * day['win_rate'])}% winners.")
    else:
        out.append("No trades closed this session.")
    kinds: Dict[str, List[Mapping[str, Any]]] = {}
    for m in mistakes:
        kinds.setdefault(m["kind"], []).append(m)
    if "loss_beyond_stop" in kinds:
        worst = min(m["r"] for m in kinds["loss_beyond_stop"])
        out.append(f"{len(kinds['loss_beyond_stop'])} trade(s) lost more than the planned 1R (worst {worst:+.2f}R). A stop "
                   "that gets jumped is a gap or liquidity risk - thinner stocks and news days need smaller size.")
    if "gave_back_winner" in kinds:
        out.append(f"{len(kinds['gave_back_winner'])} trade(s) were up 1R or more and still closed at a loss. The stop moves "
                   f"to break-even at +{breakeven_at_r:g}R; if this keeps happening, replay an earlier break-even.")
    if "took_noise" in kinds:
        out.append(f"{len(kinds['took_noise'])} trade(s) were taken through noise flags Autopilot skips. The replay's "
                   "noise report says whether those flags earn their place - trading through them is the exception.")
    if "unconfirmed" in kinds:
        out.append(f"{len(kinds['unconfirmed'])} day trade(s) were taken before the setup showed up in enough scans in a row.")
    if "reentered_after_loss" in kinds:
        out.append("Going straight back into a stock that just lost turns one loss into chop - let it cool off for the day.")
    if "unproven_strategy" in kinds:
        out.append(f"{len(kinds['unproven_strategy'])} trade(s) came from a strategy the replay hasn't proven.")
    summary = shadows.get("summary") or {}
    if summary.get("trades"):
        many = shadows["filled"] != 1
        out.append(f"{shadows['filled']} play{'s were' if many else ' was'} offered and not taken; taken as planned "
                   f"{'they' if many else 'it'} would have averaged {summary['expectancy_r']:+.2f}R "
                   f"({summary['total_r']:+.2f}R in all).")
    passed, away = (shadows.get("checks") or {}).get("passed") or {}, (shadows.get("checks") or {}).get("turned_away") or {}
    if passed.get("trades", 0) >= MIN_GROUP and away.get("trades", 0) >= MIN_GROUP:
        better = passed["expectancy_r"] > away["expectancy_r"]
        out.append(f"The plays that passed Autopilot's checks would have averaged {passed['expectancy_r']:+.2f}R, the ones "
                   f"its checks turned away {away['expectancy_r']:+.2f}R - "
                   + ("the checks did their job." if better else "the checks turned away the better plays today. One "
                      "session proves little; keep watching it."))
    for flag, row in (shadows.get("by_noise") or {}).items():
        if flag == "all_checks" or row["verdict"].startswith("too few"):
            continue
        out.append(f"Plays flagged \"{LABELS.get(flag, flag)}\" would have averaged {row['flagged_avg_r']:+.2f}R against "
                   f"{row['rest_avg_r']:+.2f}R for the rest - the check {'helped' if row['verdict'].startswith('helped') else 'cost'} today.")
    best = (shadows.get("best_missed") or [None])[0]
    if best and best["r"] >= 1.0:
        out.append(f"The best play not taken: {best['symbol']} ({titles.get(best['strategy'], best['strategy'])}) would have "
                   f"made {best['r']:+.2f}R.")
    for row in strategies:
        if row["drifting"]:
            out.append(f"{row['title']} is averaging {row['live_r']:+.2f}R over {row['live_trades']} real trades against "
                       f"{row['replay_r']:+.2f}R in the replay - real results falling short of a backtest is the classic "
                       f"warning sign. Its evidence weight is x{row['evidence_weight']:.2f}.")
    if regime and regime.get("p_turbulent") is not None:
        turbulent = regime.get("regime") == "turbulent"
        out.append(f"The market's regime: {regime.get('regime')} (P(turbulent) {regime['p_turbulent']:.2f}). "
                   + ("Momentum setups tend to struggle in turbulent markets while short-term reversals do better."
                      if turbulent else "Calm markets favour momentum setups."))
    return out


# ---------------------------------------------------------------- the review
MIN_FILLS = 5                     # measured fills before the replay's cost assumption is questioned


#: a fill slower than this was a limit waiting for its price, not the broker taking its time - the
#: review's "how the orders filled" measures the broker (the trade keeps its own seconds either way)
BROKER_FILL_S = 60.0

def execution_quality(trades: Sequence[Mapping[str, Any]], assumed_bps: float) -> Dict[str, Any]:
    """Harris's implementation shortfall over the rolling sessions: what the fills cost against the
    price at the decision, entries and exits apart, next to what the replay assumes a side costs.
    A replay that charges less than the account really pays proves setups that lose money."""
    def mean(key: str) -> Optional[float]:
        values = [float(t[key]) for t in trades if t.get(key) is not None]
        return round(sum(values) / len(values), 2) if values else None

    def fills(key: str) -> List[float]:
        """The fills that measure the broker: a limit that rested longer was waiting for the price."""
        return [float(t[key]) for t in trades if t.get(key) is not None and float(t[key]) <= BROKER_FILL_S]

    def seconds(key: str) -> Optional[float]:
        values = sorted(fills(key))
        return round(values[len(values) // 2], 2) if values else None      # the middle one: a single slow fill can't skew it

    entries = sum(1 for t in trades if t.get("entry_slippage_bps") is not None)
    exits = sum(1 for t in trades if t.get("exit_slippage_bps") is not None)
    out: Dict[str, Any] = {"entries": entries, "exits": exits, "entry_slippage_bps": mean("entry_slippage_bps"),
                           "exit_slippage_bps": mean("exit_slippage_bps"), "spread_bps": mean("spread_bps"),
                           "assumed_bps": round(float(assumed_bps), 2),
                           # how long the broker took to fill, typically, in seconds
                           "entry_latency_s": seconds("entry_latency_s"), "exit_latency_s": seconds("exit_latency_s"),
                           "slowest_entry_s": max(fills("entry_latency_s"), default=None),
                           "rested_entries": sum(1 for t in trades if t.get("entry_latency_s") is not None
                                                 and float(t["entry_latency_s"]) > BROKER_FILL_S)}
    if out["entry_latency_s"] is not None:
        out["latency_note"] = (f"Orders filled in {out['entry_latency_s']:.1f}s typically going in"
                               + (f" and {out['exit_latency_s']:.1f}s coming out" if out["exit_latency_s"] is not None else "")
                               + (f" (slowest entry {out['slowest_entry_s']:.0f}s)" if out["slowest_entry_s"] else "")
                               + (f"; {out['rested_entries']} limit entr{'y' if out['rested_entries'] == 1 else 'ies'} "
                                  f"rested longer than {BROKER_FILL_S:.0f}s, waiting for the price" if out["rested_entries"] else "")
                               + ". A stop or target resting at the broker isn't counted - it waits for the price.")
    if entries >= MIN_FILLS and exits >= MIN_FILLS:
        worst = max(out["entry_slippage_bps"] or 0.0, out["exit_slippage_bps"] or 0.0)
        verdict = ("more than the replay charges - raise replay.slippage_bps or its records flatter the setups"
                   if worst > assumed_bps * 1.5 else "within what the replay charges")
        out["note"] = (f"Fills over the last {ROLLING_SESSIONS} sessions: entries paid {out['entry_slippage_bps']:+.1f} bps "
                       f"and exits {out['exit_slippage_bps']:+.1f} bps against the price at the decision "
                       f"({entries} and {exits} measured) - {verdict} ({assumed_bps:g} bps a side).")
    return out


def build_review(day: dt.date, *, trades: Sequence[Mapping[str, Any]], plays: Sequence[Mapping[str, Any]],
                 rolling: Sequence[Mapping[str, Any]], replay_records: Mapping[str, Mapping[str, Any]],
                 evidence: Mapping[str, Mapping[str, Any]], regime: Optional[Mapping[str, Any]],
                 bars: Optional[Mapping[str, pd.DataFrame]], settings: ReplaySettings, skip_noise: Sequence[str],
                 min_confirmations: int, passes: Callable[[Mapping[str, Any]], bool], styles: Mapping[str, str],
                 titles: Mapping[str, str], breakeven_at_r: float, opened: Sequence[Mapping[str, Any]] = (),
                 marks: Optional[Mapping[str, float]] = None) -> Dict[str, Any]:
    """``bars``: the session's 5-minute candles for the plays not taken - None when they
    couldn't be had, and those plays aren't followed. ``opened``: the trades opened this session,
    closed or not - a session whose entries are all still open is not a session without trades;
    ``marks``: where the open ones' stocks stood at the review."""
    rs = [t.get("r_multiple") for t in trades]
    entered = opened_rows(opened, marks or {})
    standing = [row["open_r"] for row in entered if row["still_open"] and row["open_r"] is not None]
    day_stats = {**_stats(rs), "realized_pl": round(sum(float(t.get("realized_pl") or 0.0) for t in trades), 2),
                 "by_strategy": {k: _stats(t.get("r_multiple") for t in trades if t["strategy"] == k)
                                 for k in sorted({t["strategy"] for t in trades})},
                 "opened": len(entered), "still_open": sum(1 for row in entered if row["still_open"]),
                 "open_r": round(sum(standing), 2),
                 "open_pl": round(sum(row["open_pl"] or 0.0 for row in entered if row["still_open"]), 2)}
    # what an entry was taken on is judged the day it is taken, not only the day it closes
    still_open = [t for t in opened if t.get("status") == "OPEN"]
    mistakes = find_mistakes(list(trades) + still_open, skip_noise=skip_noise, min_confirmations=min_confirmations,
                             styles=styles)
    if bars is None:
        shadows: Dict[str, Any] = {"followed": 0, "filled": 0, "summary": {"trades": 0},
                                   "note": "IB Gateway wasn't connected, so the plays not taken couldn't be followed."}
    else:
        shadows = shadow_outcomes(plays, bars, day, settings, passes)
    strategies = strategy_table(live_records(rolling), replay_records, evidence, titles)
    fills = execution_quality(rolling, settings.slippage_bps + settings.commission_bps)
    notes = lessons(day_stats, mistakes, shadows, strategies, regime, titles, breakeven_at_r)
    if fills.get("latency_note"):
        notes.append(fills["latency_note"])
    if entered:
        n, left = day_stats["opened"], day_stats["still_open"]
        where = (f"; {left} still open, standing at {day_stats['open_r']:+.2f}R in all at the review" if standing
                 else f"; {left} still open" if left else "")
        notes.insert(1, f"{n} position{'s' if n != 1 else ''} opened this session{where}.")
    if fills.get("note"):
        notes.append(fills["note"])
    return {
        "session": day.isoformat(), "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "regime": regime,
        "day": day_stats, "trades": trade_rows(trades), "opened": entered, "mistakes": mistakes, "shadows": shadows,
        "strategies": strategies, "plays_offered": len(plays), "execution": fills,
        "lessons": notes,
        "settings": {"skip_noise": list(skip_noise), "min_confirmations": min_confirmations,
                     "costs_bps": {"slippage": settings.slippage_bps, "commission": settings.commission_bps},
                     "rolling_sessions": ROLLING_SESSIONS},
    }


class Journal:
    """Where the reviews are kept: the database, and a JSON file per session."""

    def __init__(self, directory: Path, repo) -> None:
        self.directory = directory
        self.repo = repo

    def save(self, review: Mapping[str, Any]) -> None:
        day = dt.date.fromisoformat(review["session"])
        try:
            self.repo.save_review(day, dict(review))
        except Exception:  # noqa: BLE001
            log.exception("could not save the review for %s in the database", day)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            tmp = self._path(day).with_suffix(".tmp")
            tmp.write_text(json.dumps(review, default=str), encoding="utf-8")
            tmp.replace(self._path(day))
        except OSError:
            log.warning("could not write the review for %s", day, exc_info=True)

    def get(self, day: dt.date) -> Optional[Dict[str, Any]]:
        try:
            found = self.repo.get_review(day)
        except Exception:  # noqa: BLE001
            found = None
        if found is not None:
            return found
        try:
            return json.loads(self._path(day).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def has(self, day: dt.date) -> bool:
        return self._path(day).exists() or self.get(day) is not None

    def days(self, limit: int = 60) -> List[Dict[str, Any]]:
        try:
            return self.repo.list_reviews(limit)
        except Exception:  # noqa: BLE001
            log.debug("could not list the reviews", exc_info=True)
            return []

    def _path(self, day: dt.date) -> Path:
        return self.directory / f"{day.isoformat()}.json"
