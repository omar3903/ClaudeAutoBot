"""FastAPI app: REST + a WebSocket that streams engine events to the dashboard.

Endpoints that touch secrets or start a broker sign-in are same-machine only
(see :mod:`tos_bot.server.security`). Handlers that call into the engine are
plain ``def``, so FastAPI runs them in its thread pool and a slow broker call
never stalls the event loop that feeds the WebSocket.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import get_settings
from ..core.eventbus import BUS
from ..data.sectors import SECTORS
from ..engine import TradingEngine
from .security import require_local

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
LOCAL_ONLY = [Depends(require_local)]


def _result(res: Dict[str, Any]) -> JSONResponse:
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


def create_app() -> FastAPI:
    settings = get_settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        BUS.bind_loop(asyncio.get_running_loop())
        engine = TradingEngine(settings)
        app.state.engine = engine
        # start() spawns its own daemon threads and returns quickly
        await asyncio.get_running_loop().run_in_executor(None, engine.start)
        log.info("engine started; dashboard ready")
        try:
            yield
        finally:
            engine.stop()

    app = FastAPI(title="AutoTradeBot", version="0.2.0", lifespan=lifespan)

    def eng() -> TradingEngine:
        return app.state.engine

    # ---- state / plays -------------------------------------------- #
    @app.get("/api/state")
    def state():
        return eng().snapshot()

    @app.get("/api/plays")
    def plays():
        return {"plays": eng().current_plays()}

    @app.get("/api/plays/{play_id}")
    def play_detail(play_id: str):
        p = eng().get_play(play_id)
        if not p:
            raise HTTPException(404, "play not found")
        return p

    @app.post("/api/plays/{play_id}/assess")
    def assess(play_id: str):
        return eng().assess_play(play_id)

    @app.post("/api/plays/{play_id}/approve")
    def approve(play_id: str):
        return _result(eng().approve_play(play_id))

    @app.post("/api/plays/{play_id}/reject")
    def reject(play_id: str):
        return eng().reject_play(play_id)

    # ---- trades / pnl ------------------------------------------ #
    @app.get("/api/trades")
    def trades(status: Optional[str] = None, limit: int = 100):
        repo = eng().repo
        return {"trades": repo.open_trades() if status == "OPEN" else repo.recent_trades(limit)}

    @app.get("/api/trades/{trade_id}")
    def trade(trade_id: str):
        t = eng().repo.get_trade(trade_id)
        if not t:
            raise HTTPException(404, "trade not found")
        return t

    @app.post("/api/trades/{trade_id}/close")
    def close_trade(trade_id: str):
        return _result(eng().close_position(trade_id, reason="manual"))

    @app.post("/api/trades/{trade_id}/managed")
    def set_managed(trade_id: str, body: dict):
        return eng().set_trade_managed(trade_id, bool((body or {}).get("on", True)))

    @app.get("/api/pnl")
    def pnl():
        return eng().repo.pnl_summary()

    @app.get("/api/equity-curve")
    def equity_curve():
        return {"points": eng().repo.equity_curve()}

    # ---- account / market ------------------------------------ #
    @app.post("/api/account/refresh")
    def account_refresh():
        return eng().refresh_account_now()

    @app.get("/api/market")
    def market():
        from ..util import clock
        return clock.market_status()

    # ---- routing: paper <-> live ----------------------------- #
    @app.get("/api/broker")
    def broker_state():
        s = eng().snapshot()
        return {k: s[k] for k in ("mode", "broker", "venue", "connection", "data_source",
                                  "data_is_real", "armed", "app_mode")}

    @app.post("/api/broker")
    def set_mode(body: dict):
        return _result(eng().set_mode((body or {}).get("mode", "")))

    @app.post("/api/paper/reset")
    def paper_reset(body: dict):
        cash = (body or {}).get("cash")
        return _result(eng().reset_paper(float(cash) if cash is not None else None))

    @app.post("/api/paper/reconcile")
    def paper_reconcile():
        return _result(eng().reconcile_paper())

    # ---- connections (same machine only) ---------------------- #
    @app.get("/api/setup", dependencies=LOCAL_ONLY)
    def setup():
        return eng().setup_state()

    @app.post("/api/setup/brokers", dependencies=LOCAL_ONLY)
    def setup_brokers(body: dict):
        b = body or {}
        return _result(eng().set_broker_setup(paper_platform=b.get("paper_platform"),
                                              live_broker=b.get("live_broker")))

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

    @app.post("/api/setup/schwab/login", dependencies=LOCAL_ONLY)
    def schwab_login():
        return _result(eng().schwab_login.start())

    @app.get("/api/auth")
    def auth_status():
        return eng().token_manager.status().as_dict()

    # ---- sector filter --------------------------------------- #
    @app.get("/api/filters/sectors")
    def get_sectors():
        return {"all": list(SECTORS), "selected": eng().sectors}

    @app.post("/api/filters/sectors")
    def set_sectors(body: dict):
        return _result(eng().set_sectors((body or {}).get("sectors") or []))

    # ---- autopilot (hands-off entry) ---------------------- #
    @app.get("/api/autopilot")
    def autopilot_state():
        return eng().autopilot.status()

    @app.post("/api/autopilot")
    def set_autopilot(body: dict):
        return _result(eng().set_autopilot(**(body or {})))

    # ---- scan / strategies -------------------------------- #
    @app.post("/api/scan/now")
    def scan_now():
        return eng().trigger_scan()

    @app.get("/api/scans")
    def scans():
        return {"last": eng().snapshot().get("scan", {})}

    @app.get("/api/strategies")
    def strategies():
        return {"strategies": eng().strategy_catalog()}

    # ---- websocket ------------------------------------------- #
    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        BUS.add_queue(q)
        loop = asyncio.get_running_loop()
        try:
            snap = await loop.run_in_executor(None, eng().snapshot)
            await sock.send_json({"topic": "hello", "payload": snap})
            await sock.send_json({"topic": "plays.updated",
                                  "payload": {"plays": eng().current_plays()}})
            while True:
                evt = await q.get()
                await sock.send_json(evt.as_dict())
        except WebSocketDisconnect:
            pass
        except Exception as e:  # noqa: BLE001
            log.debug("ws closed: %s", e)
        finally:
            BUS.remove_queue(q)

    # ---- static dashboard --------------------------------- #
    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

        @app.get("/")
        def index():
            return FileResponse(str(WEB_DIR / "index.html"))

    return app


app = create_app()
