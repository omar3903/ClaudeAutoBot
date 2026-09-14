#!/usr/bin/env python
"""Start the dashboard + engine.

    python run.py                 # normal
    python run.py --no-browser
    python run.py --host 0.0.0.0 --port 9000

Ctrl+C doesn't walk away from open positions: in paper it closes them, resets
the simulator and exits; in live with positions open it asks you to choose in
the dashboard (press Ctrl+C again within 10 s to force-quit anyway).
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import threading
import time
import webbrowser

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import uvicorn  # noqa: E402

from tos_bot.config import get_settings  # noqa: E402
from tos_bot.util.logging_setup import setup_logging  # noqa: E402


class GuardedServer(uvicorn.Server):
    """uvicorn server whose Ctrl+C goes through the engine's quit rules."""

    FORCE_WINDOW_S = 10.0

    def __init__(self, config: uvicorn.Config, app) -> None:
        super().__init__(config)
        self.app = app
        self._last_signal = 0.0

    def handle_exit(self, sig, frame) -> None:
        engine = getattr(self.app.state, "engine", None)
        now = time.monotonic()
        if engine is None or self.should_exit:
            return super().handle_exit(sig, frame)
        if now - self._last_signal < self.FORCE_WINDOW_S:
            print("\n  Force quit. Any open positions stay open at the broker and are NOT managed.\n",
                  flush=True)
            return super().handle_exit(sig, frame)
        self._last_signal = now
        try:
            preview = engine.quit_preview()
        except Exception:  # noqa: BLE001
            return super().handle_exit(sig, frame)

        if engine.quit_state:
            print(f"\n  Still closing {preview['left']} position(s) before quitting. "
                  "Press Ctrl+C again within 10 s to force quit.\n", flush=True)
            return None
        if preview["paper"]:
            print("\n  Quitting paper: closing open positions"
                  + (" and resetting the simulator" if preview["resets_simulator"] else "")
                  + "...\n", flush=True)
            threading.Thread(target=engine.begin_quit,
                             kwargs={"close_all": True, "operator": "console"}, daemon=True).start()
            return None
        if preview["positions"]:
            print(f"\n  {preview['left']} LIVE position(s) are open. Choose 'Exit all & quit' or "
                  "'Cancel' in the dashboard, or press Ctrl+C again within 10 s to force quit "
                  "(the positions stay open and unmanaged).\n", flush=True)
            engine.request_quit_dialog()
            return None
        return super().handle_exit(sig, frame)


def main() -> None:
    s = get_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=s.secrets.web_host)
    ap.add_argument("--port", type=int, default=s.secrets.web_port)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--reload", action="store_true", help="dev auto-reload (no quit guard)")
    args = ap.parse_args()

    setup_logging(s.config.app.log_level)
    log_level = s.config.app.log_level.lower()

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
    if args.reload:
        uvicorn.run("tos_bot.server.app:app", host=args.host, port=args.port,
                    reload=True, log_level=log_level)
        return

    from tos_bot.server.app import app

    server = GuardedServer(uvicorn.Config(app, host=args.host, port=args.port,
                                          log_level=log_level), app)
    app.state.shutdown = lambda: setattr(server, "should_exit", True)
    server.run()


if __name__ == "__main__":
    main()
