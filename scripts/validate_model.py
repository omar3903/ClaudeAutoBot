#!/usr/bin/env python
"""Judge a model of the plays on the training set - purged walk-forward folds, the baselines it must
beat, a shuffled-label baseline, and the metric table.

    python scripts/validate_model.py                        # the database's rows, every source
    python scripts/validate_model.py --source replay        # the replay's rows only
    python scripts/validate_model.py --csv data/research/training_set.csv --folds 6 --shuffles 50
    python scripts/validate_model.py --out data/research/validation.json

Prints the table and writes the full report as JSON (default data/research/validation.json). The
app may keep running meanwhile; nothing here changes how it trades.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from autotradebot.config import DATA_DIR  # noqa: E402
from autotradebot.research import validate  # noqa: E402
from autotradebot.research.dataset import load_csv, training_rows  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default=None, help="a training set written by scripts/export_training_set.py")
    ap.add_argument("--source", choices=["all", "live", "shadow", "replay"], default="all")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--embargo-days", type=float, default=1.0)
    ap.add_argument("--shuffles", type=int, default=20)
    ap.add_argument("--l2", type=float, default=validate.Logistic.L2, help="the logistic model's shrinkage")
    ap.add_argument("--out", default=str(DATA_DIR / "research" / "validation.json"))
    args = ap.parse_args()
    if args.csv:
        rows = load_csv(pathlib.Path(args.csv))
    else:
        from autotradebot.persistence.db import init_db
        from autotradebot.persistence.repository import Repository

        init_db()
        rows = training_rows(Repository())
    sources = None if args.source == "all" else [args.source]
    report = validate.run(rows, sources=sources, folds=args.folds, embargo_days=args.embargo_days,
                          shuffles=args.shuffles, l2=args.l2)
    print(validate.table(report))
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"report -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
