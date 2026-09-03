#!/usr/bin/env python
"""Start the dashboard + engine.

    python run.py                 # normal
    python run.py --no-browser
    python run.py --host 0.0.0.0 --port 9000
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import threading
import time
import webbrowser

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from tos_bot.config import get_settings
from tos_bot.util.logging_setup import setup_logging


def main() -> None:
    s = get_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=s.secrets.web_host)
    ap.add_argument("--port", type=int, default=s.secrets.web_port)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--reload", action="store_true", help="dev auto-reload")
    args = ap.parse_args()

    setup_logging(s.config.app.log_level)

    url = f"http://{args.host}:{args.port}/"
    if not args.no_browser and s.secrets.open_browser_on_start:
        def _open():
            time.sleep(1.6)
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=_open, daemon=True).start()

    print(f"\n  AutoTradeBot -> {url}\n")
    import uvicorn

    uvicorn.run("tos_bot.server.app:app", host=args.host, port=args.port,
                reload=args.reload, log_level=s.config.app.log_level.lower())


if __name__ == "__main__":
    main()
