"""FastAPI app: REST + a WebSocket that streams engine events to the dashboard.

Endpoints that touch secrets, exit every position or quit the app are
same-machine only (see :mod:`tos_bot.server.security`). Handlers that call into
the engine are plain ``def``, so FastAPI runs them in its thread pool and a
slow broker call never stalls the event loop that feeds the WebSocket.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import Settings, get_settings
from ..core.eventbus import BUS
from ..data.sectors import SECTORS
from ..engine import TradingEngine
from ..scanner.filters import SIDES, TIMEFRAMES
from .security import require_local

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
LOCAL_ONLY = [Depends(require_local)]
# Windows can map .js to text/plain, and browsers refuse to run modules served that way
mimetypes.add_type("text/javascript", ".js")


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

    def eng() -> TradingEngine:
        return app.state.engine

    # ---- state and plays ------------------------------------------------ #
    @app.get("/api/state")
    def state():
        return eng().snapshot()

    @app.get("/api/plays")
    def plays():
        return {"plays": eng().current_plays()}

    @app.post("/api/plays/{play_id}/assess")
    def assess(play_id: str):
        return eng().assess_play(play_id)

    @app.post("/api/plays/{play_id}/approve")
    def approve(play_id: str):
        return _result(eng().approve_play(play_id))

    @app.post("/api/plays/{play_id}/reject")
    def reject(play_id: str):
        return eng().reject_play(play_id)

    # ---- trades and P/L ------------------------------------------------- #
    @app.get("/api/trades")
    def trades(status: Optional[str] = None, limit: int = 100):
        repo = eng().repo
        return {"trades": repo.open_trades() if status == "OPEN" else repo.recent_trades(limit)}

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

    # ---- quitting (same machine only) ----------------------------------- #
    @app.get("/api/quit", dependencies=LOCAL_ONLY)
    def quit_preview():
        return eng().quit_preview()

    @app.post("/api/quit", dependencies=LOCAL_ONLY)
    def quit_app(body: dict):
        return _result(eng().begin_quit(close_all=bool((body or {}).get("close_all", True))))

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
        return _result(eng().set_scan_settings(
            premarket_time=b.get("premarket_time"), cycle_minutes=b.get("cycle_minutes"),
            hot_list_size=b.get("hot_list_size"), sector_queue_size=b.get("sector_queue_size")))

    @app.get("/api/watchlist")
    def watchlist():
        return eng().watchlist_state()

    # ---- websocket -------------------------------------------------------- #
    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        BUS.add_queue(q)
        loop = asyncio.get_running_loop()
        try:
            snap = await loop.run_in_executor(None, eng().snapshot)
            await sock.send_json({"topic": "hello", "payload": snap})
            rows = await loop.run_in_executor(None, eng().current_plays)
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
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

        @app.get("/")
        def index():
            return FileResponse(str(WEB_DIR / "index.html"))

    return app


app = create_app()
