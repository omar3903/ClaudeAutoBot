#!/usr/bin/env python
"""Put right two things the trade records got wrong before the app was fixed to get them right.

    python scripts/repair_records.py                            # say what it would change
    python scripts/repair_records.py --apply                    # change it
    python scripts/repair_records.py --live-since 2026-09-23    # the first session on real-time prices

* **A stop that never moved, booked as a trailing stop.** A closed trade whose exit reason is
  ``trailing-stop`` while its stop is still where it began (within half a cent of the initial stop) went
  out at its first stop: it is relabelled ``stop``. Its P/L, R and prices stay as they are.
* **Slippage measured against delayed quotes.** Before the account had real-time prices the quote an
  entry or an exit was decided on was minutes old, so a fill measured against it says how far the price
  moved since, not what the fill cost - and the daily review read those figures as slippage. The entry's
  (``decision_price``, ``entry_slippage_bps``) is cleared on the trades entered before ``--live-since``,
  and the exit's (``exit_decision_price``, ``exit_slippage_bps``) on the trades that exited before it - an
  exit on or after it was measured on live prices and stays. The quote's spread is kept. New trades keep
  slippage only when the deciding quote was live.

The database is the one the app uses (its settings). Only trade ids are printed. Each change is made at
most once, so it is safe to run again.
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys
from typing import List, Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from autotradebot.persistence.db import init_db, session_scope  # noqa: E402
from autotradebot.persistence.models_orm import Trade  # noqa: E402
from autotradebot.util import clock  # noqa: E402

#: the first session the account had real-time prices
LIVE_SINCE = dt.date(2026, 9, 23)
#: a stop this close to where it began never moved (the order rests at the initial stop rounded to the cent)
UNMOVED = 0.005 + 1e-9
ENTRY_FIELDS = ("decision_price", "entry_slippage_bps")
EXIT_FIELDS = ("exit_decision_price", "exit_slippage_bps")


def _session(at: Optional[dt.datetime]) -> Optional[dt.date]:
    """The New York session a stored (naive UTC) time falls in."""
    if at is None:
        return None
    return clock.session_date(at.replace(tzinfo=dt.timezone.utc) if at.tzinfo is None else at)


def _never_moved(t: Trade) -> bool:
    return (t.status == "CLOSED" and t.exit_reason == "trailing-stop" and t.stop_price is not None
            and t.initial_stop_price is not None and abs(float(t.stop_price) - float(t.initial_stop_price)) <= UNMOVED)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="change the rows (default: only say what it would change)")
    ap.add_argument("--live-since", type=dt.date.fromisoformat, default=LIVE_SINCE,
                    help=f"the first session on real-time prices (default {LIVE_SINCE.isoformat()})")
    args = ap.parse_args()
    init_db()
    relabelled = entries = exits = 0
    with session_scope() as s:
        trades: List[Trade] = s.execute(select(Trade).order_by(Trade.entry_time, Trade.id)).scalars().all()
        for t in trades:
            if _never_moved(t):
                relabelled += 1
                print(f"  {t.id}  exit reason trailing-stop -> stop (its stop never moved)")
                if args.apply:
                    t.exit_reason = "stop"
            cleared = []
            entered, exited = _session(t.entry_time), _session(t.exit_time)
            if entered is not None and entered < args.live_since and any(getattr(t, f) is not None
                                                                         for f in ENTRY_FIELDS):
                entries += 1
                cleared.append("entry")
                if args.apply:
                    for f in ENTRY_FIELDS:
                        setattr(t, f, None)
            if exited is not None and exited < args.live_since and any(getattr(t, f) is not None for f in EXIT_FIELDS):
                exits += 1
                cleared.append("exit")
                if args.apply:
                    for f in EXIT_FIELDS:
                        setattr(t, f, None)
            if cleared:
                print(f"  {t.id}  {' and '.join(cleared)} slippage cleared (measured against a delayed quote)")
        if not args.apply:
            s.rollback()
    print(f"{'changed' if args.apply else 'would change'}: {relabelled} never-moved stop(s) relabelled 'stop', "
          f"entry slippage cleared on {entries} trade(s) and exit slippage on {exits} (before "
          f"{args.live_since.isoformat()})" + ("" if args.apply else " - run with --apply to change them"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
