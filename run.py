#!/usr/bin/env python
"""Start the dashboard + engine.

    python run.py                 # normal
    python run.py --no-browser
    python run.py --port 9000

The dashboard is only served on this computer's own names (127.0.0.1,
localhost, ::1): any other host is refused unless you pass --allow-network.

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

from autotradebot.config import get_settings  # noqa: E402
from autotradebot.util.keep_awake import keep_awake  # noqa: E402
from autotradebot.util.logging_setup import setup_logging  # noqa: E402

#: names only this computer can reach. Any other host (0.0.0.0, a network address) opens the dashboard's
#: port to other devices
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
#: the app is never behind a proxy, so X-Forwarded-For is never read: no header can stand in for the
#: address a request really came from, which the same-machine check goes by
NO_PROXY_HEADERS = {"proxy_headers": False, "forwarded_allow_ips": ""}
#: the exit code of a refused start: scripts/run_24_7.bat stops on it instead of starting again every 30 s
REFUSED = 2


def host_problem(host: str, allow_network: bool = False) -> str:
    """Why the dashboard mustn't be served on ``host``, or "" when it may: only on this computer's own
    names, unless --allow-network says the network is meant."""
    if allow_network or host.lower() in LOCAL_HOSTS:
        return ""
    return (f"Refusing to serve the dashboard on {host!r}: that opens its port to other devices on your "
            "network (and to the internet, if your router forwards it). The dashboard has no password and "
            "isn't encrypted, and it can place orders on your IBKR account - only its same-machine check "
            "would stand in the way. Keep WEB_HOST=127.0.0.1 in .env (and leave out --host), or pass "
            "--allow-network if you really mean to serve it on the network.")


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
        if preview.get("keepable"):
            print(f"\n  {len(preview['keepable'])} swing position(s) have a stop resting at the broker. Choose "
                  "'Keep swing positions & quit' or 'Close all & quit' in the dashboard, or press Ctrl+C again "
                  "within 10 s to force quit (they stay open, protected by those stops).\n", flush=True)
            engine.request_quit_dialog()
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
    ap.add_argument("--allow-network", action="store_true",
                    help="serve the dashboard on a host other than this computer's own (refused without it)")
    args = ap.parse_args()

    problem = host_problem(args.host, args.allow_network)
    if problem:
        print(f"\n  {problem}\n", file=sys.stderr, flush=True)
        sys.exit(REFUSED)

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
    if s.config.app.keep_awake and keep_awake():          # this (main) thread lives as long as the app
        print("  Keeping this computer awake while the app runs (app.keep_awake in config.yaml).\n")
    if args.reload:
        uvicorn.run("autotradebot.server.app:app", host=args.host, port=args.port,
                    reload=True, log_level=log_level, **NO_PROXY_HEADERS)
        return

    from autotradebot.server.app import app

    server = GuardedServer(uvicorn.Config(app, host=args.host, port=args.port,
                                          log_level=log_level, **NO_PROXY_HEADERS), app)
    app.state.shutdown = lambda: setattr(server, "should_exit", True)
    server.run()


if __name__ == "__main__":
    main()
