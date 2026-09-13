#!/usr/bin/env python
"""Sign in to Schwab / thinkorswim from the terminal.

The dashboard does the same with one click (Connections -> Sign in with
Schwab). IBKR needs no sign-in here - its login is the running IB Gateway
(see scripts/ibkr_setup.py).

    python scripts/authenticate.py            # open Schwab's login, save the token
    python scripts/authenticate.py --check    # print the token status
    python scripts/authenticate.py --reset    # back up + remove the token, then sign in again

Schwab's refresh token lasts 7 days.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tos_bot.auth import SchwabLogin, TokenManager  # noqa: E402
from tos_bot.config import get_settings  # noqa: E402
from tos_bot.persistence.db import init_db  # noqa: E402
from tos_bot.persistence.repository import Repository  # noqa: E402
from tos_bot.util.logging_setup import setup_logging  # noqa: E402


def main() -> int:
    setup_logging("INFO")
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="print the token status and exit")
    ap.add_argument("--reset", action="store_true", help="remove the current token first")
    args = ap.parse_args()

    s = get_settings()
    init_db()
    tm = TokenManager(s, repo=Repository())
    if args.check:
        print(json.dumps(tm.status().as_dict(), indent=2))
        return 0
    if args.reset:
        tm.expire_now("manual --reset")

    login = SchwabLogin(s, on_success=lambda: tm.note_full_auth(source="authenticate.py"))
    started = login.start()
    if not started["ok"]:
        print(started["reason"])
        return 2
    print(started["note"])
    state = login.wait()
    print(login.message)
    print(json.dumps(tm.status().as_dict(), indent=2))
    return 0 if state == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
