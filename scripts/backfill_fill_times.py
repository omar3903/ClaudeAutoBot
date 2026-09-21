#!/usr/bin/env python
"""Fill in how long each earlier order took to fill, for the trades booked before the app kept it.

    python scripts/backfill_fill_times.py            # say what it would write
    python scripts/backfill_fill_times.py --apply    # write it

The seconds come from what the database already holds:

* the entry: from the order going out to the fill. The send time is the earlier of the trade's
  ``submitted_at`` and the entry order's first PLACE in the order audit - so an entry taken back after
  a restart, whose ``submitted_at`` was the restart, is measured from when it really went out;
* the exit: only an exit the app sent itself (tagged ``exit:<trade>``), from its last PLACE before the
  position closed to the close. A stop or target resting at the broker has none - it waited for the
  price, not for the broker - and neither does a trade closed some other way.

Trades on the in-app simulator are left out: it fills an order the moment it gets it.

Only empty values are filled, so it is safe to run again. The app may keep running meanwhile; its new
trades get the seconds as they are booked.
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys
from typing import Dict, List, Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from tos_bot.persistence.db import init_db, session_scope  # noqa: E402
from tos_bot.persistence.models_orm import OrderAudit, Trade  # noqa: E402


#: the in-app simulator's venue - it fills an order the moment it gets it, so it has no fill time to learn from
SIMULATOR = "paper"


def _tag(row: OrderAudit) -> str:
    req = row.request if isinstance(row.request, dict) else {}
    return str(req.get("tag") or req.get("client_tag") or "")


def _seconds(sent: Optional[dt.datetime], filled: Optional[dt.datetime]) -> Optional[float]:
    if sent is None or filled is None:
        return None
    took = (filled - sent).total_seconds()
    return round(took, 3) if took >= 0 else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the seconds (default: only say what it would write)")
    args = ap.parse_args()
    init_db()
    entries = exits = 0
    with session_scope() as s:
        trades: List[Trade] = s.execute(select(Trade).where(Trade.entry_time.is_not(None))).scalars().all()
        placed: Dict[str, List[OrderAudit]] = {}
        for row in s.execute(select(OrderAudit).where(OrderAudit.action == "PLACE", OrderAudit.ok.is_(True))
                             .order_by(OrderAudit.ts)).scalars():
            tag = _tag(row)
            if tag:
                placed.setdefault(tag, []).append(row)
        for t in trades:
            if (t.broker or "paper") == SIMULATOR:
                continue                                 # the simulator fills at once: its seconds say nothing
            if t.entry_latency_s is None:
                first = (placed.get(t.play_id or "") or [None])[0]
                sent = min((x for x in (t.submitted_at, first.ts if first else None) if x is not None), default=None)
                took = _seconds(sent, t.entry_time)
                if took is not None:
                    entries += 1
                    print(f"  entry  {t.id}  {took:9.3f}s")
                    if args.apply:
                        t.entry_latency_s = took
            if t.exit_latency_s is None and t.status == "CLOSED" and t.exit_time is not None:
                sent_exits = [x for x in placed.get(f"exit:{t.id}", []) if x.ts <= t.exit_time]
                if sent_exits:
                    took = _seconds(sent_exits[-1].ts, t.exit_time)
                    if took is not None:
                        exits += 1
                        print(f"  exit   {t.id}  {took:9.3f}s")
                        if args.apply:
                            t.exit_submitted_at, t.exit_latency_s = sent_exits[-1].ts, took
        if not args.apply:
            s.rollback()
    print(f"{'wrote' if args.apply else 'would write'} {entries} entry and {exits} exit fill times"
          + ("" if args.apply else " - run with --apply to write them"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
