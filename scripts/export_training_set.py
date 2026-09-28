#!/usr/bin/env python
"""Write the training set - every play with its features and outcome - to one CSV.

    python scripts/export_training_set.py                       # data/research/training_set.csv
    python scripts/export_training_set.py --out some/file.csv
    python scripts/export_training_set.py --run rpl_20260916160653   # a particular replay run

The rows come from the app's database (see autotradebot/research/dataset.py): the trades it took, the
plays it showed and didn't take, and the latest replay. The app may keep running meanwhile.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from autotradebot.config import DATA_DIR  # noqa: E402
from autotradebot.persistence.db import init_db  # noqa: E402
from autotradebot.persistence.repository import Repository  # noqa: E402
from autotradebot.research.dataset import counts, training_rows, write_csv  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(DATA_DIR / "research" / "training_set.csv"))
    ap.add_argument("--run", default=None, help="a replay run id (default: the latest)")
    args = ap.parse_args()
    init_db()
    rows = training_rows(Repository(), run_id=args.run)
    path = write_csv(rows, pathlib.Path(args.out))
    by = counts(rows)
    print(f"{len(rows)} rows -> {path}")
    for source in ("live", "shadow", "replay"):
        print(f"  {source:7s} {by.get(source, 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
