"""FastAPI app: REST + a WebSocket that streams engine events to the dashboard."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import get_settings
from ..core.eventbus import BUS
from ..engine import TradingEngine

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


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

    app = FastAPI(title="tos-trader", version="0.1.0", lifespan=lifespan)

    def eng(app_: FastAPI) -> TradingEngine:
        return app_.state.engine

    # ---- state / plays -------------------------------------------- #
    @app.get("/api/state")
    async def state():
        return eng(app).snapshot()

    @app.get("/api/plays")
    async def plays():
        return {"plays": eng(app).current_plays()}

    @app.get("/api/plays/{play_id}")
    async def play_detail(play_id: str):
        p = eng(app).get_play(play_id)
        if not p:
            raise HTTPException(404, "play not found")
        return p

    @app.post("/api/plays/{play_id}/assess")
    async def assess(play_id: str):
        return eng(app).assess_play(play_id)

    @app.post("/api/plays/{play_id}/approve")
    async def approve(play_id: str):
        res = eng(app).approve_play(play_id)
        return JSONResponse(res, status_code=200 if res.get("ok") else 400)

    @app.post("/api/plays/{play_id}/reject")
    async def reject(play_id: str):
        return eng(app).reject_play(play_id)

    # ---- trades / pnl ------------------------------------------ #
    @app.get("/api/trades")
    async def trades(status: Optional[str] = None, limit: int = 100):
        repo = eng(app).repo
        if status == "OPEN":
            return {"trades": repo.open_trades()}
        return {"trades": repo.recent_trades(limit)}

    @app.get("/api/trades/{trade_id}")
    async def trade(trade_id: str):
        t = eng(app).repo.get_trade(trade_id)
        if not t:
            raise HTTPException(404, "trade not found")
        return t

    @app.post("/api/trades/{trade_id}/close")
    async def close_trade(trade_id: str):
        res = eng(app).close_position(trade_id, reason="manual")
        return JSONResponse(res, status_code=200 if res.get("ok") else 400)

    @app.get("/api/pnl")
    async def pnl():
        return eng(app).repo.pnl_summary()

    @app.get("/api/equity-curve")
    async def equity_curve():
        return {"points": eng(app).repo.equity_curve()}

    # ---- broker mode (paper <-> live) --------------------- #
    @app.get("/api/broker")
    async def broker_state():
        s = eng(app).snapshot()
        return {"mode": s["mode"], "broker": s["broker"], "live": s["live"],
                "data_source": s["data_source"], "data_is_real": s["data_is_real"],
                "armed": s["armed"], "app_mode": s["app_mode"]}

    @app.post("/api/broker")
    async def set_broker(body: dict):
        res = eng(app).set_mode((body or {}).get("mode", ""))
        return JSONResponse(res, status_code=200 if res.get("ok") else 400)

    @app.post("/api/paper/reset")
    async def paper_reset(body: dict):
        cash = (body or {}).get("cash")
        res = eng(app).reset_paper(float(cash) if cash is not None else None)
        return JSONResponse(res, status_code=200 if res.get("ok") else 400)

    # ---- scan / strategies / auth -------------------------- #
    @app.post("/api/scan/now")
    async def scan_now():
        return eng(app).trigger_scan()

    @app.get("/api/scans")
    async def scans():
        return {"last": eng(app).snapshot().get("scan", {})}

    @app.get("/api/strategies")
    async def strategies():
        return {"strategies": eng(app).strategy_catalog()}

    @app.get("/api/auth")
    async def auth_status():
        return eng(app).snapshot().get("token", {})

    @app.post("/api/auth/reauth")
    async def reauth():
        return eng(app).reauthenticate()

    # ---- websocket ------------------------------------------- #
    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        BUS.add_queue(q)
        try:
            await sock.send_json({"topic": "hello", "payload": eng(app).snapshot()})
            await sock.send_json({"topic": "plays.updated",
                                  "payload": {"plays": eng(app).current_plays()}})
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
        async def index():
            return FileResponse(str(WEB_DIR / "index.html"))

    return app


app = create_app()
