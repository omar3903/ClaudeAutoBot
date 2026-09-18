"""The training set: one row per play with its features and what happened to it.

Three populations, told apart by ``source``:

* ``live``   - trades the app really took (paper or live), with the features kept at the fill
               (trades.entry_context) and the real outcome;
* ``shadow`` - plays the app showed but didn't take, followed on the session's candles as if they
               had been (shadow_trades, written by the 16:15 review);
* ``replay`` - the replay's simulated trades (sim_trades), with the features at the signal.

Training on the live rows alone would learn the gates' choices rather than the market; the shadow
rows are what the gates turned away, and the replay rows are the bulk. ``held_out`` marks the
replay's out-of-sample sessions so a model can be judged the way the strategies are.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .features import FEATURE_KEYS, FEATURE_SCHEMA

log = logging.getLogger(__name__)

#: the columns before the features, in order
ROW_KEYS = ("source", "id", "run_id", "symbol", "strategy", "timeframe", "side", "entered_at", "exited_at",
            "exit_reason", "held_out", "r", "win", "mfe_r", "entry_slippage_bps", "exit_slippage_bps", "spread_bps")
#: the features that aren't already row keys (strategy, timeframe and side are both)
FEATURE_COLUMNS = tuple(k for k in FEATURE_KEYS if k != "schema" and k not in ROW_KEYS)
COLUMNS = ROW_KEYS + FEATURE_COLUMNS + ("schema",)


def _row(source: str, features: Optional[Mapping[str, Any]], **fields: Any) -> Dict[str, Any]:
    feats = dict(features or {})
    r = fields.get("r")
    out = {k: fields.get(k) for k in ROW_KEYS}
    out["source"] = source
    out["win"] = None if r is None else bool(float(r) > 0)
    for k in FEATURE_COLUMNS:
        out[k] = feats.get(k)
    out["schema"] = feats.get("schema", FEATURE_SCHEMA if feats else None)
    return out


def live_rows(trades: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Closed trades the app took, with an entry context (older trades have none and are left out)."""
    rows = []
    for t in trades:
        ctx = t.get("entry_context")
        if t.get("status") != "CLOSED" or not ctx or t.get("pair_id") or t.get("r_multiple") is None:
            continue
        rows.append(_row("live", ctx, id=t["id"], run_id=None, symbol=t["symbol"], strategy=t["strategy"],
                         timeframe=t["timeframe"], side=t["side"], entered_at=t.get("entry_time"),
                         exited_at=t.get("exit_time"), exit_reason=t.get("exit_reason"), held_out=False,
                         r=t.get("r_multiple"), mfe_r=_mfe_r(t), entry_slippage_bps=t.get("entry_slippage_bps"),
                         exit_slippage_bps=t.get("exit_slippage_bps"), spread_bps=t.get("spread_bps")))
    return rows


def shadow_rows(shadows: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Plays shown and not taken, whose shadow trade filled."""
    return [_row("shadow", s.get("features"), id=s["play_id"], run_id=None, symbol=s["symbol"],
                 strategy=s["strategy"], timeframe=s.get("timeframe"), side=s["side"], entered_at=s.get("entered_at"),
                 exited_at=s.get("exited_at"), exit_reason=s.get("exit_reason"), held_out=False, r=s.get("r"),
                 mfe_r=s.get("mfe_r"))
            for s in shadows if s.get("filled") and s.get("r") is not None]


def replay_rows(sims: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [_row("replay", s.get("features"), id=s.get("id"), run_id=s.get("run_id"), symbol=s["symbol"],
                 strategy=s["strategy"], timeframe=s["timeframe"], side=s["side"], entered_at=s.get("entered_at"),
                 exited_at=s.get("exited_at"), exit_reason=s.get("exit_reason"), held_out=bool(s.get("held_out")),
                 r=s.get("r"), mfe_r=s.get("mfe_r"))
            for s in sims]


def training_rows(repo, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every row the database can give: the latest replay run (or ``run_id``), every shadow trade
    and every closed live trade with a context."""
    rows = live_rows(repo.recent_trades(limit=100000))
    rows += shadow_rows(repo.shadow_trades())
    rows += replay_rows(repo.sim_trades(run_id=run_id or repo.latest_sim_run()))
    return rows


def counts(rows: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in rows:
        out[r["source"]] = out.get(r["source"], 0) + 1
    return out


def write_csv(rows: Sequence[Mapping[str, Any]], path: Path) -> Path:
    """One CSV with the same columns for every source; lists (noise, tags) are joined with '|'."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(COLUMNS), extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: "|".join(map(str, v)) if isinstance(v, (list, tuple)) else v for k, v in r.items()})
    return path


def load_csv(path: Path) -> List[Dict[str, Any]]:
    """A training set written by write_csv, read back: numbers as floats, the '|'-joined lists as
    lists, True/False as booleans, empty cells as None."""
    rows: List[Dict[str, Any]] = []
    with Path(path).open("r", newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            row: Dict[str, Any] = {}
            for k, v in raw.items():
                if k in ("noise", "tags"):
                    row[k] = [f for f in (v or "").split("|") if f]
                elif v is None or v == "":
                    row[k] = None
                elif v in ("True", "False"):
                    row[k] = v == "True"
                elif k in ROW_KEYS[:10]:                     # ids, names and times stay text
                    row[k] = v
                else:
                    try:
                        row[k] = float(v)
                    except ValueError:
                        row[k] = v
            rows.append(row)
    return rows


def _mfe_r(t: Mapping[str, Any]) -> Optional[float]:
    try:
        entry, stop, mfe = float(t["entry_price"]), float(t["initial_stop_price"]), float(t["mfe"])
    except (KeyError, TypeError, ValueError):
        return None
    risk = abs(entry - stop)
    return round(mfe / risk, 3) if risk > 0 else None
