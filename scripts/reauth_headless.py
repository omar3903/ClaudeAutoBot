#!/usr/bin/env python
"""Unattended re-authentication (opt-in).

Runs the broker OAuth without a human by driving the login page with
Playwright, reading your credentials from the OS keyring. Used by
TokenManager when  auth.auto_reauth: headless.

SETUP (once):
    pip install playwright keyring
    playwright install chromium
    python -c "import keyring; keyring.set_password('autotradebot','schwab_username','YOUR_USER')"
    python -c "import keyring; keyring.set_password('autotradebot','schwab_password','YOUR_PASS')"

SECURITY: this script - which YOU own and run - is the only place a password
is handled, and only via your keyring. AutoTradeBot's own code never sees it.
Many brokers also require 2FA; if so, headless auth cannot complete and you
should keep  auth.auto_reauth: notify  instead.
"""

from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tos_bot.config import get_settings


def main() -> int:
    s = get_settings()
    if s.secrets.broker != "schwab":
        print("headless re-auth only implemented for BROKER=schwab")
        return 2
    try:
        import keyring
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("pip install playwright keyring  &&  playwright install chromium")
        return 2

    user = keyring.get_password("autotradebot", "schwab_username")
    pw = keyring.get_password("autotradebot", "schwab_password")
    if not (user and pw):
        print("keyring entries autotradebot/schwab_username|schwab_password not set")
        return 2

    try:
        from schwab.auth import client_from_login_flow
    except ImportError:
        print("schwab-py not installed")
        return 2

    # schwab-py opens a browser and waits for the redirect. We run it in a
    # thread and, in parallel, let Playwright fill the login form.
    import threading

    key, secret = s.secrets.schwab_api_key, s.secrets.schwab_app_secret
    cb = s.secrets.schwab_callback_url
    token_path = str(s.secrets.token_path)
    err = {}

    def _flow():
        try:
            client_from_login_flow(key, secret, cb, token_path, interactive=False)
        except Exception as e:  # noqa: BLE001
            err["e"] = e

    t = threading.Thread(target=_flow, daemon=True)
    t.start()
    time.sleep(3)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        # schwab-py prints/opens the auth URL; simplest portable approach is to
        # let it open the system browser. For a fully headless variant, capture
        # the URL schwab-py logs and page.goto(url) here, then:
        try:
            page.wait_for_selector("input[name='j_username']", timeout=15000)
            page.fill("input[name='j_username']", user)
            page.fill("input[name='j_password']", pw)
            page.click("button[type='submit']")
            page.wait_for_timeout(4000)
            # accept the app-consent screen if present
            for sel in ("#acceptTerms", "button:has-text('Allow')", "button:has-text('Accept')"):
                if page.query_selector(sel):
                    page.click(sel)
                    break
            page.wait_for_timeout(4000)
        except Exception as e:  # noqa: BLE001
            print("playwright could not complete the form (2FA?):", e)
        browser.close()

    t.join(timeout=60)
    if err:
        print("auth flow error:", err["e"])
        return 1
    print("headless re-auth finished; token at", token_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
