#!/usr/bin/env python
"""Say what became of the entries sent before the app saved it. An entry order that went out and never
filled left its play reading SUBMITTED in the play log, so the daily review counted it as taken.

    python scripts/repair_play_log.py            # say what it would change
    python scripts/repair_play_log.py --apply    # change it

A play is marked CANCELED when all of these hold:

* the log says it was sent (ACCEPTED, SUBMITTED or WORKING) and no trade was booked from it;
* the order audit shows its entry order went out - an ok PLACE for the play - and as a DAY order;
* that order's session is over (after 20:00 ET for today's), so it can't still be working.

Who decided and when stay as they were. The one case it can't tell apart: an entry that filled while the
app was off has no trade either, and its shares show as untracked on the dashboard - look there before
--apply. Only empty outcomes are filled in, so it is safe to run again.
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys
from typing import Dict, List

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from tos_bot.persistence.db import init_db, session_scope  # noqa: E402
from tos_bot.persistence.models_orm import OrderAudit, PlayLog, Trade  # noqa: E402
from tos_bot.util import clock  # noqa: E402

SENT = ("ACCEPTED", "SUBMITTED", "WORKING")
#: when a DAY order's life ends for certain - the extended session's close
DAY_ORDERS_END = dt.time(20, 0)


def _sessions_over() -> dt.date:
    """The latest session whose DAY orders can't still be working."""
    now = clock.now_ny()
    today = clock.session_date(now)
    return today if now.time() >= DAY_ORDERS_END else today - dt.timedelta(days=1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="change the rows (default: only say what it would change)")
    args = ap.parse_args()
    init_db()
    over = _sessions_over()
    found = 0
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with session_scope() as s:
        rows: List[PlayLog] = s.execute(select(PlayLog).where(PlayLog.status.in_(SENT))).scalars().all()
        ids = [r.id for r in rows]
        traded = set(s.execute(select(Trade.play_id).where(Trade.play_id.in_(ids))).scalars()) if ids else set()
        placed: Dict[str, List[OrderAudit]] = {}
        if ids:
            for a in s.execute(select(OrderAudit).where(OrderAudit.action == "PLACE", OrderAudit.ok.is_(True),
                                                        OrderAudit.play_id.in_(ids))).scalars():
                placed.setdefault(a.play_id, []).append(a)
        for r in sorted(rows, key=lambda r: r.created_at or dt.datetime.min):
            sent = placed.get(r.id) or []
            if r.id in traded or not sent:
                continue
            if any(str((a.request if isinstance(a.request, dict) else {}).get("tif", "")).upper() != "DAY"
                   for a in sent):
                continue                                 # a good-till-cancelled order may still be working
            created = (r.created_at or dt.datetime.min).replace(tzinfo=dt.timezone.utc)
            session = clock.session_date(created)
            if session > over:
                continue
            found += 1
            print(f"  {session}  {r.id}  {r.symbol:<6} {r.timeframe:<8} {r.status} -> CANCELED")
            if args.apply:
                r.status = "CANCELED"
                evidence = dict(r.evidence or {})
                evidence.setdefault("entry_outcome", {"status": "CANCELED", "at": stamp,
                                                      "reason": "sent and never filled (scripts/repair_play_log.py)"})
                r.evidence = evidence
        if not args.apply:
            s.rollback()
    print(f"{'marked' if args.apply else 'would mark'} {found} sent plays CANCELED"
          + ("" if args.apply else " - run with --apply to change them"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
