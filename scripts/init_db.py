#!/usr/bin/env python
"""Create the database schema (tables + indexes) on the configured DB.

    python scripts/init_db.py            # uses DATABASE_URL / DB_* from .env
    python scripts/init_db.py --sqlite   # force the local sqlite file
    python scripts/init_db.py --drop     # DROP then recreate (destructive!)
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tos_bot.config import get_settings
from tos_bot.persistence.db import DB
from tos_bot.persistence.models_orm import Base
from tos_bot.util.logging_setup import setup_logging


def main() -> None:
    setup_logging("INFO")
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", action="store_true", help="use the local sqlite fallback")
    ap.add_argument("--drop", action="store_true", help="drop all tables first")
    args = ap.parse_args()

    s = get_settings()
    url = s.secrets.sqlite_fallback_url() if args.sqlite else None
    eng = DB.init(url=url)
    print(f"target: {DB.url}  ({DB.dialect})")

    if args.drop:
        if input("really DROP every tos-trader table? type 'yes': ").strip() != "yes":
            sys.exit("aborted")
        Base.metadata.drop_all(eng)
        print("dropped.")

    Base.metadata.create_all(eng)
    print("tables:", ", ".join(t.name for t in Base.metadata.sorted_tables))
    print("done.")


if __name__ == "__main__":
    main()
