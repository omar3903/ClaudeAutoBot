#!/usr/bin/env python
"""One-time (and re-auth) OAuth for the live broker.

    python scripts/authenticate.py            # opens a browser, mints the token
    python scripts/authenticate.py --check    # just print token status
    python scripts/authenticate.py --force-rotate   # backup+delete+re-auth now

For BROKER=schwab this uses schwab-py's local-server login flow: it starts a
tiny HTTPS listener on your SCHWAB_CALLBACK_URL, opens the consent page, and
captures the redirect. Nothing but the resulting token touches disk.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tos_bot.config import get_settings
from tos_bot.persistence.db import init_db
from tos_bot.persistence.repository import Repository
from tos_bot.auth.token_manager import TokenManager
from tos_bot.util.logging_setup import setup_logging


def _authenticate_schwab(s) -> None:
    try:
        from schwab.auth import client_from_login_flow, client_from_manual_flow
    except ImportError:
        sys.exit("schwab-py not installed.  pip install schwab-py")

    key, secret = s.secrets.schwab_api_key, s.secrets.schwab_app_secret
    cb = s.secrets.schwab_callback_url
    token_path = str(s.secrets.token_path)
    if not (key and secret):
        sys.exit("Set SCHWAB_API_KEY and SCHWAB_APP_SECRET in .env first.")

    print(f"Opening Schwab consent flow; callback = {cb}")
    try:
        client_from_login_flow(key, secret, cb, token_path)
    except Exception as e:  # noqa: BLE001
        print(f"local-server flow failed ({e}); falling back to manual paste flow")
        client_from_manual_flow(key, secret, cb, token_path)
    print(f"token written to {token_path}")


def _authenticate_tda(s) -> None:
    try:
        from tda.auth import client_from_manual_flow
    except ImportError:
        sys.exit("tda-api not installed (note: the TDA API is retired).")
    key, redirect = s.secrets.tda_api_key, s.secrets.tda_redirect_uri
    client_from_manual_flow(key, redirect, str(s.secrets.token_path))
    print("token written")


def main() -> None:
    setup_logging("INFO")
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--force-rotate", action="store_true")
    args = ap.parse_args()

    s = get_settings()
    init_db()
    repo = Repository()
    tm = TokenManager(s, repo=repo)

    if args.check:
        print(json.dumps(tm.status().as_dict(), indent=2))
        return

    if args.force_rotate:
        tm.rotate_now("manual --force-rotate")

    broker = s.secrets.broker
    if broker == "schwab":
        _authenticate_schwab(s)
    elif broker == "tda":
        _authenticate_tda(s)
    elif broker == "paper":
        print("BROKER=paper needs no authentication.")
        return
    else:
        sys.exit(f"authenticate.py does not handle BROKER={broker}")

    tm.note_full_auth(source="authenticate.py")
    print("\nStatus now:")
    print(json.dumps(tm.status().as_dict(), indent=2))


if __name__ == "__main__":
    main()
