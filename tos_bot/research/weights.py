"""How much the evidence says to lean on each strategy.

A strategy's weight in the ranking starts at the one set in the Strategies panel. On top
of it sits an evidence multiplier from the strategy's record: the replayed trades
Autopilot would have taken, and the trades it has really made.

Chan's warnings about backtests (*Quantitative Trading*, ch. 3) shape how the two mix:

- a replay flatters - look-ahead, data snooping, fills that were never that clean - and
  paper or live trading is the only true out-of-sample test, so each real trade counts
  twice as much as a replayed one;
- a small sample proves little, so the pooled result is shrunk toward "no edge" as if
  ``PRIOR_TRADES`` trades at 0R had come first - thirty trades barely move the weight;
- the multiplier stays between 0.5 and 1.5, so the evidence tilts the ranking and never
  takes it over;
- a strategy that lost money on the replay's held-out last third, or that is losing in
  real trading, is never raised, however good the rest of its record looks.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Mapping, Optional

PRIOR_TRADES = 50.0
LIVE_WEIGHT = 2.0
SENSITIVITY = 2.0            # +0.1R of pooled edge -> x1.2
BOUNDS = (0.5, 1.5)
MIN_HELD_OUT = 10            # held-out trades before their result can hold a strategy back
MIN_LIVE = 10


@dataclass(frozen=True)
class Evidence:
    multiplier: float
    pooled_r: float
    replay_trades: int
    replay_r: Optional[float]
    held_out_trades: int
    held_out_r: Optional[float]
    live_trades: int
    live_r: Optional[float]
    note: str

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _record(record: Optional[Mapping[str, Any]]) -> tuple:
    trades = int((record or {}).get("trades") or 0)
    return trades, (float(record["expectancy_r"]) if trades else None)


def evidence_multiplier(replay: Optional[Mapping[str, Any]], live: Optional[Mapping[str, Any]]) -> Evidence:
    """``replay``: the strategy's replayed record (with ``out_of_sample``); ``live``: its
    closed real trades, as ``{"trades", "expectancy_r"}``."""
    n_replay, r_replay = _record(replay)
    n_live, r_live = _record(live)
    n_held, r_held = _record((replay or {}).get("out_of_sample"))
    weight = n_replay + LIVE_WEIGHT * n_live + PRIOR_TRADES
    pooled = ((r_replay or 0.0) * n_replay + LIVE_WEIGHT * (r_live or 0.0) * n_live) / weight
    multiplier = min(BOUNDS[1], max(BOUNDS[0], 1.0 + SENSITIVITY * pooled))
    notes = []
    if n_held >= MIN_HELD_OUT and (r_held or 0.0) <= 0 and multiplier > 1.0:
        multiplier = 1.0
        notes.append(f"not raised: it averaged {r_held:+.2f}R on the replay's held-out sessions")
    if n_live >= MIN_LIVE and (r_live or 0.0) < 0 and multiplier > 1.0:
        multiplier = 1.0
        notes.append(f"not raised: it is averaging {r_live:+.2f}R in real trading")
    if not n_replay and not n_live:
        notes.append("no record yet")
    return Evidence(multiplier=round(multiplier, 3), pooled_r=round(pooled, 4), replay_trades=n_replay,
                    replay_r=r_replay, held_out_trades=n_held, held_out_r=r_held, live_trades=n_live, live_r=r_live,
                    note="; ".join(notes))
