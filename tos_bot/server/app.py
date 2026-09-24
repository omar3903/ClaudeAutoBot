"""FastAPI app: REST + a WebSocket that streams engine events to the dashboard.

Every request, and the live feed's WebSocket, must come from the dashboard on
this computer, and the endpoints that touch secrets, exit every position, fix a
share count or quit the app check it again, header included (see
:mod:`tos_bot.server.security`). Every answer also forbids framing by another
website and content sniffing, and lets the page run only the dashboard's own
script files. Handlers that call into the engine are plain
``def``, so FastAPI runs them in its thread pool and a slow broker call never
stalls the event loop that feeds the WebSocket.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import logging
import mimetypes
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import Settings, get_settings
from ..core.eventbus import BUS
from ..data.sectors import SECTORS
from ..engine import TradingEngine
from ..scanner.filters import SIDES, TIMEFRAMES
from .security import refusal, require_local

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
LOCAL_ONLY = [Depends(require_local)]
# Windows can map .js to text/plain, and browsers refuse to run modules served that way
mimetypes.add_type("text/javascript", ".js")

#: the dashboard is plain ES modules; a browser that reuses a cached copy of one of them after an
#: update mixes old and new code and the page stops working, so every load re-checks each file
NO_CACHE = {"Cache-Control": "no-cache"}

#: the page runs only the dashboard's own script files - no inline script, nothing from another website.
#: Every outside text (a headline, a filing) is escaped as it's drawn; if one ever isn't, it still can't
#: run as script and approve plays or close positions with the dashboard's own header. Inline styles stay
#: allowed (the templates set widths with style="..."), and connect-src 'self' covers the live feed's ws://
CONTENT_POLICY = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                  "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
                  "form-action 'self'; frame-ancestors 'none'")

#: on every answer, refusals included. No other website may show the dashboard in a frame: a hidden
#: frame can line the user's clicks up with Exit all or Quit, and those clicks are the dashboard's own,
#: so no same-machine check can tell them apart. And a browser takes each file as the type it's served as
FRAME_GUARD = {"X-Frame-Options": "DENY", "Content-Security-Policy": CONTENT_POLICY,
               "X-Content-Type-Options": "nosniff"}


def web_build() -> str:
    """A stamp of the dashboard's files as they are on disk now. A tab that was open before the app was
    updated still runs the scripts it loaded; it compares this with the stamp it started on and reloads."""
    stamp = hashlib.sha1()
    for path in sorted(WEB_DIR.rglob("*")):
        if path.is_file():
            stat = path.stat()
            stamp.update(f"{path.relative_to(WEB_DIR).as_posix()}:{stat.st_size}:{stat.st_mtime_ns};".encode())
    return stamp.hexdigest()[:12]


class _FreshStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.update(NO_CACHE)
        return response


def _result(res: Dict[str, Any]) -> JSONResponse:
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


def create_app(engine_factory: Callable[[Settings], TradingEngine] = TradingEngine) -> FastAPI:
    settings = get_settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        BUS.bind_loop(asyncio.get_running_loop())
        engine = engine_factory(settings)

        def shutdown() -> None:
            if app.state.shutdown is not None:
                app.state.shutdown()
            else:
                log.warning("quit finished - stop the server process to exit")

        engine.on_shutdown = shutdown
        app.state.engine = engine
        # start() spawns its own daemon threads and returns quickly
        await asyncio.get_running_loop().run_in_executor(None, engine.start)
        log.info("engine started; dashboard ready")
        try:
            yield
        finally:
            engine.stop()

    app = FastAPI(title="AutoTradeBot", version="0.4.0", lifespan=lifespan)
    app.state.shutdown = None           # run.py wires this to the uvicorn server
    # the snapshot and the plays are tens of KB of JSON, fetched all session: gzipped they're a fraction of
    # that. A small answer isn't worth compressing, and the WebSocket doesn't pass through here
    app.add_middleware(GZipMiddleware, minimum_size=2048)

    # every request, not only the LOCAL_ONLY routes: otherwise another website open in the browser
    # could approve a play or close a position with a plain form POST, and a rebound domain could
    # read and drive the whole API. The refusal is returned, not raised - an HTTPException raised
    # in a middleware isn't turned into an answer
    @app.middleware("http")
    async def local_only(request: Request, call_next):
        client = request.client.host if request.client else ""
        why = refusal(client, request.headers, request.method, request.url.path)
        if why:
            response = JSONResponse({"detail": why}, status_code=403)
        else:
            response = await call_next(request)
        # added beside the answer's own headers (no-cache, gzip); one a route already set is left as it is
        for name, value in FRAME_GUARD.items():
            response.headers.setdefault(name, value)
        return response

    def eng() -> TradingEngine:
        return app.state.engine

    # ---- state and plays ------------------------------------------------ #
    @app.get("/api/state")
    def state():
        return eng().snapshot()

    @app.get("/api/price/{symbol}")
    def price(symbol: str):
        # one stock's latest price, pre-market and after-hours included, for a panel that shows it
        return eng().price_of(symbol.upper())

    @app.get("/api/plays")
    def plays(full: bool = False):
        # what the table shows of each play, as the board's push sends it; ?full=1 for every play whole
        return {"plays": eng().current_plays(full=full)}

    @app.get("/api/plays/{play_id}")
    def play(play_id: str):
        row = eng().play_row(play_id)
        if row is None:
            raise HTTPException(404, "play not found (it may have expired)")
        return row

    @app.post("/api/plays/{play_id}/assess")
    def assess(play_id: str):
        # the play on the screen streams first, so its Execute click is priced off the stream - here and not in
        # assess_play, which Autopilot calls for every play it looks at
        eng().watch_play(play_id)
        return eng().assess_play(play_id)

    @app.post("/api/plays/{play_id}/approve")
    def approve(play_id: str):
        return _result(eng().approve_play(play_id))

    @app.get("/api/plays/{play_id}/chart")
    def play_chart(play_id: str):
        return eng().play_chart(play_id)

    @app.post("/api/plays/{play_id}/reject")
    def reject(play_id: str):
        return eng().reject_play(play_id)

    # ---- trades and P/L ------------------------------------------------- #
    @app.get("/api/trades")
    def trades(status: Optional[str] = None, limit: int = 100):
        e = eng()
        return {"trades": e.open_positions() if status == "OPEN" else e.repo.recent_trades(limit)}

    @app.get("/api/orders")
    def orders(fresh: bool = False):
        return eng().active_orders(max_age_s=0.0 if fresh else None)

    @app.post("/api/trades/close-all", dependencies=LOCAL_ONLY)
    def close_all():
        return _result(eng().close_all_positions())

    @app.get("/api/trades/{trade_id}/record")
    def trade_record(trade_id: str):
        rec = eng().trade_record(trade_id)
        if not rec:
            raise HTTPException(404, "trade record not found (it may have been removed)")
        return rec

    @app.post("/api/trades/{trade_id}/close")
    def close_trade(trade_id: str):
        return _result(eng().close_position(trade_id, reason="manual"))

    @app.post("/api/positions/untracked/{symbol}/close")
    def close_untracked(symbol: str):
        return _result(eng().close_untracked(symbol.upper()))

    # a share-count warning's Fix: it books P/L and can send a market exit, so same machine only
    @app.get("/api/positions/mismatch/{symbol}", dependencies=LOCAL_ONLY)
    def mismatch_preview(symbol: str):
        return eng().mismatch_preview(symbol.upper())

    @app.post("/api/positions/mismatch/{symbol}/fix", dependencies=LOCAL_ONLY)
    def fix_mismatch(symbol: str, body: dict):
        b = body or {}
        # the counts the preview showed: the engine refuses when they've changed since
        expect = {k: b[k] for k in ("recorded", "held") if isinstance(b.get(k), (int, float))}
        return _result(eng().fix_mismatch(symbol.upper(), str(b.get("action") or ""), expect=expect or None))

    @app.post("/api/trades/{trade_id}/managed")
    def set_managed(trade_id: str, body: dict):
        return _result(eng().set_trade_managed(trade_id, bool((body or {}).get("on", True))))

    @app.get("/api/pnl")
    def pnl():
        return eng().repo.pnl_summary()

    # ---- account and routing -------------------------------------------- #
    @app.post("/api/account/refresh")
    def account_refresh():
        return eng().refresh_account_now()

    @app.post("/api/mode")
    def set_mode(body: dict):
        return _result(eng().set_mode((body or {}).get("mode", "")))

    @app.post("/api/paper/reset")
    def paper_reset(body: dict):
        cash = (body or {}).get("cash")
        return _result(eng().reset_paper(float(cash) if cash is not None else None))

    @app.get("/api/capital")
    def capital():
        return {"capital": eng().capital_state()}

    @app.post("/api/capital")
    def set_capital(body: dict):
        return _result(eng().set_capital((body or {}).get("amount")))

    @app.post("/api/capital/split")
    def set_capital_split(body: dict):
        return _result(eng().set_capital_split((body or {}).get("day_pct")))

    # ---- quitting (same machine only) ----------------------------------- #
    @app.get("/api/quit", dependencies=LOCAL_ONLY)
    def quit_preview():
        return eng().quit_preview()

    @app.post("/api/quit", dependencies=LOCAL_ONLY)
    def quit_app(body: dict):
        return _result(eng().begin_quit(close_all=bool((body or {}).get("close_all", True)),
                                        keep=bool((body or {}).get("keep", False))))

    @app.post("/api/quit/cancel", dependencies=LOCAL_ONLY)
    def quit_cancel():
        return _result(eng().cancel_quit())

    @app.post("/api/orders/cancel-all", dependencies=LOCAL_ONLY)
    def orders_cancel_all():
        return _result(eng().cancel_working_orders())

    # ---- connections (same machine only) -------------------------------- #
    @app.get("/api/setup", dependencies=LOCAL_ONLY)
    def setup():
        return eng().setup_state()

    @app.post("/api/setup/paper-platform", dependencies=LOCAL_ONLY)
    def setup_paper_platform(body: dict):
        return _result(eng().set_paper_platform((body or {}).get("paper_platform")))

    @app.post("/api/setup/secrets", dependencies=LOCAL_ONLY)
    def setup_secrets(body: dict):
        return _result(eng().save_secrets((body or {}).get("values") or {}))

    # a probe that finds nothing listening is an answer, not a bad request - 200 + ok flag
    @app.post("/api/setup/reconnect", dependencies=LOCAL_ONLY)
    def setup_reconnect():
        return eng().reconnect()

    @app.post("/api/setup/ibkr/test", dependencies=LOCAL_ONLY)
    def ibkr_test(body: dict):
        return eng().probe_ibkr((body or {}).get("account", "paper"))

    # ---- filters, strategies, Autopilot --------------------------------- #
    @app.get("/api/filters")
    def get_filters():
        return {"filters": eng().filters.as_dict(), "all_sectors": list(SECTORS),
                "sides": list(SIDES), "timeframes": list(TIMEFRAMES)}

    @app.post("/api/filters")
    def set_filters(body: dict):
        b = body or {}
        return _result(eng().set_filters(sides=b.get("sides"), timeframes=b.get("timeframes"),
                                         sectors=b.get("sectors")))

    @app.get("/api/strategies")
    def strategies():
        return {"strategies": eng().strategy_state()}

    @app.post("/api/strategies/reset")
    def reset_strategies():
        return _result(eng().reset_strategies())

    @app.post("/api/strategies/{key}")
    def set_strategy(key: str, body: dict):
        b = body or {}
        return _result(eng().set_strategy(key, enabled=b.get("enabled"), weight=b.get("weight")))

    @app.post("/api/autopilot")
    def set_autopilot(body: dict):
        return _result(eng().set_autopilot(**(body or {})))

    @app.get("/api/signals")
    def signals_state():
        return eng().signals_state()

    @app.get("/api/signals/stock/{symbol}")
    def signal_detail(symbol: str):
        return eng().signal_detail(symbol.upper())

    @app.post("/api/signals/check")
    def signals_check():
        return _result(eng().check_signals())

    @app.get("/api/replay")
    def replay():
        return eng().replay_state()

    @app.post("/api/replay")
    def start_replay(body: dict):
        b = body or {}
        return _result(eng().start_replay(sessions=b.get("sessions"), swing_sessions=b.get("swing_sessions")))

    @app.get("/api/replay/history")
    def replay_history(limit: int = 30):
        return {"runs": eng().replay_history(max(1, min(200, limit)))}

    # ---- the journal: one review per session ------------------------------- #
    @app.get("/api/journal")
    def journal(limit: int = 60):
        return eng().journal_state(max(1, min(400, limit)))

    @app.get("/api/journal/{session}")
    def journal_session(session: str):
        try:
            day = dt.date.fromisoformat(session)
        except ValueError:
            raise HTTPException(400, "the session must be a date like 2026-09-15")
        review = eng().journal_review(day)
        if review is None:
            raise HTTPException(404, "no review for that session")
        return review

    @app.get("/api/journal/{session}/movers/{symbol}/chart")
    def mover_chart(session: str, symbol: str):
        try:
            day = dt.date.fromisoformat(session)
        except ValueError:
            raise HTTPException(400, "the session must be a date like 2026-09-15")
        return eng().mover_chart(day, symbol.upper())

    @app.post("/api/journal/review")
    def journal_build(body: dict):
        session = (body or {}).get("session")
        try:
            day = dt.date.fromisoformat(session) if session else None
        except ValueError:
            return _result({"ok": False, "reason": "the session must be a date like 2026-09-15"})
        return _result(eng().review_session(day))

    # ---- pairs trading --------------------------------------------------------- #
    @app.get("/api/pairs")
    def pairs(live: bool = False):
        return eng().pairs_state(live=live)

    @app.post("/api/pairs/enter")
    def enter_pair(body: dict):
        return _result(eng().enter_pair(str((body or {}).get("pair") or "")))

    @app.post("/api/pairs/trades/{pair_trade_id}/close")
    def close_pair(pair_trade_id: str):
        return _result(eng().close_pair(pair_trade_id))

    @app.get("/api/pairs/chart")
    def pair_chart(pair: str):
        return eng().pair_chart(pair)

    # ---- scans, scan settings, watchlist -------------------------------- #
    @app.post("/api/scan")
    def scan(body: dict):
        return _result(eng().request_scan((body or {}).get("kind", "cycle")))

    @app.get("/api/settings")
    def scan_settings():
        return eng().scan_status()

    @app.post("/api/settings")
    def set_scan_settings(body: dict):
        b = body or {}
        # every field of the Settings drawer: the wide scan's four were left out, so changing them did nothing
        keys = ("premarket_time", "gapper_time", "cycle_minutes", "hot_list_size", "sector_queue_size",
                "wide_minutes", "wide_stocks", "movers", "yesterday_movers")
        return _result(eng().set_scan_settings(**{k: b.get(k) for k in keys}))

    @app.get("/api/watchlist")
    def watchlist():
        return eng().watchlist_state()

    # ---- websocket -------------------------------------------------------- #
    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        # the HTTP middleware doesn't see a WebSocket, and a browser lets any website open one here:
        # the same check, before the snapshot is built. Closing before accept answers the handshake 403
        client = sock.client.host if sock.client else ""
        if refusal(client, sock.headers, "GET", sock.url.path):
            await sock.close(code=1008)
            return
        await sock.accept()
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        BUS.add_queue(q)
        loop = asyncio.get_running_loop()
        try:
            snap = await loop.run_in_executor(None, eng().snapshot)
            await sock.send_json({"topic": "hello", "payload": {**snap, "web_build": web_build()}})
            rows = await loop.run_in_executor(None, eng().current_plays)     # slim, like every later push
            await sock.send_json({"topic": "plays.updated", "payload": {"plays": rows}})
            while True:
                evt = await q.get()
                await sock.send_json(evt.as_dict())
        except WebSocketDisconnect:
            pass
        except Exception as e:  # noqa: BLE001
            log.debug("ws closed: %s", e)
        finally:
            BUS.remove_queue(q)

    # ---- the dashboard ------------------------------------------------------ #
    if WEB_DIR.exists():
        app.mount("/static", _FreshStaticFiles(directory=str(WEB_DIR)), name="static")

        @app.get("/")
        def index():
            return FileResponse(str(WEB_DIR / "index.html"), headers=NO_CACHE)

    return app


app = create_app()
