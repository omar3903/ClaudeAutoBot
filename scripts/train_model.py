"""Train the meta-label model on every row the database holds, judge it walking forward, and keep it.

    python scripts/train_model.py                 # live + shadow + the latest replay's rows
    python scripts/train_model.py --source replay # one population only
    python scripts/train_model.py --out some/dir  # keep the model somewhere else (a dry run)

The app trains by itself after each day's review; this is the same thing by hand. The model is saved
whether or not it is usable - an unusable one only runs in shadow (its odds are logged, never acted
on). See docs/AutoTradeBot-learning.pdf and autotradebot/research/model.py.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autotradebot.config import DATA_DIR  # noqa: E402
from autotradebot.persistence.db import init_db  # noqa: E402
from autotradebot.persistence.repository import Repository  # noqa: E402
from autotradebot.research import model as meta  # noqa: E402
from autotradebot.research.dataset import counts, training_rows  # noqa: E402
from autotradebot.research.validate import table  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(DATA_DIR / "research" / "models"))
    ap.add_argument("--run", default=None, help="a replay run id (default: the latest)")
    ap.add_argument("--source", action="append", help="live, shadow or replay (repeat for several; default: all)")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--embargo-days", type=float, default=1.0)
    ap.add_argument("--shuffles", type=int, default=10)
    args = ap.parse_args()
    if not meta.available():
        print("scikit-learn isn't installed:  pip install scikit-learn")
        return 2
    init_db()
    rows = training_rows(Repository(), run_id=args.run)
    print("rows:", counts(rows))
    out = meta.train(rows, Path(args.out), folds=args.folds, embargo_days=args.embargo_days, shuffles=args.shuffles,
                     sources=args.source)
    if not out.get("saved"):
        print("not trained:", out.get("why"))
        return 1
    card = out["card"]
    print(table(out["report"]))
    print(f"\nmodel {card['id']} kept in {args.out}  -  usable: {card['usable']}")
    for check, passed in (card["verdict"].get("checks") or {}).items():
        print(f"  {'pass' if passed else 'FAIL'}  {check}")
    print("\nwhat the model leans on (out-of-sample loss when shuffled):")
    for row in card["importance"][:10]:
        print(f"  {row['feature']:32s} {row['loss_increase']:+.5f}")
    print("\nrank correlation with the outcome in R:")
    for row in card["information_coefficients"][:8]:
        print(f"  {row['feature']:32s} {row['ic']:+.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
