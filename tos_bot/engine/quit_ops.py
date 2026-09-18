"""Quitting without stranding a position: the preview, the close-out and its progress, resuming a quit
after a restart.

A mixin of TradingEngine (engine.py): it works on the engine's own attributes and is kept apart
only so each concern reads on its own. Nothing here is instantiated by itself.
"""

from __future__ import annotations

import logging
import datetime as dt
import threading
import time
from typing import List, Any, Dict, Optional

from ..brokers.venues import venue_label

log = logging.getLogger(__name__)


class QuitOps:
    # ------------------------------------------------------------------ #
    #  Quitting                                                          #
    # ------------------------------------------------------------------ #
    def quit_preview(self) -> Dict[str, Any]:
        keepable = self._keepable_ids()
        brief = [{"id": t["id"], "symbol": t["symbol"], "side": t["side"], "quantity": t["quantity"],
                  "entry_price": t["entry_price"], "venue": t.get("broker") or "paper",
                  "timeframe": t.get("timeframe"), "keepable": t["id"] in keepable}
                 for t in self._open_trades()]
        here = [b for b in brief if b["venue"] == self._venue]
        return {
            "keepable": [b for b in here if b["keepable"]],
            "mode": self.mode, "paper": self.mode == "paper",
            "venue": self._venue, "venue_label": venue_label(self._venue),
            "positions": here, "left": len(here), "parked": [b for b in brief if b["venue"] != self._venue],
            "resets_simulator": self._venue == "paper",
            "reset_cash": self.settings.config.account.paper_start_cash,
            "quitting": bool(self.quit_state),
        }

    def request_quit_dialog(self) -> None:
        """Ask the dashboard to show the quit choices (Ctrl+C with live positions open)."""
        self._publish("quit.requested", **self.quit_preview())

    def _keepable_ids(self) -> set:
        """Positions that may stay open while the app is off: swing trades (a day trade must be flat
        by the close), not pair legs (they have no stop of their own), each with its stop order
        resting at the broker right now - that order is what protects it until the app is back."""
        executor = self.executor
        if executor is None or not executor.native_stops_on():
            return set()
        protected = {s["trade_id"] for s in executor.protective_stops()}
        return {t["id"] for t in self._positions_here()
                if t["id"] in protected and t.get("timeframe") == "SWING" and not t.get("pair_id")}

    def begin_quit(self, close_all: bool = True, operator: str = "operator", keep: bool = False) -> Dict[str, Any]:
        """Paper: close everything, reset the simulator, shut down. Live: close
        everything and shut down once flat (``close_all=False`` cancels). ``keep``: leave the
        swing positions that have a stop resting at the broker open (_keepable_ids) and close only
        the rest - the app picks them up again when it starts. Until the last position to be
        closed is out, nothing but exits may change."""
        with self._switch_lock:
            if self.quit_state:
                return {"ok": True, "note": "Already closing out before quitting.", "quit": self._quit_status()}
            kept = self._keepable_ids() if keep else set()
            held = [t for t in self._positions_here() if t["id"] not in kept]
            shut = self._exits_cant_fill()
            if shut and held:
                if not keep:
                    return {"ok": False, "market_closed": True,
                            "reason": shut + " Choose 'Keep them open & quit', or quit when the market is open."}
                # nothing can be closed now: keep every position - the ones without a stop at the broker too,
                # since trying to close them would only lock the app until the next session
                kept, held = {t["id"] for t in self._positions_here()}, []
            if self.mode == "live" and held and not close_all:
                return {"ok": False, "reason": "Quit cancelled - your live positions stay open and managed."}
            self.quit_state = {"started_at": dt.datetime.now(dt.timezone.utc).isoformat(), "mode": self.mode,
                               "venue": self._venue, "by": operator, "reset_sim": self._venue == "paper",
                               "keeping": sorted(kept)}
            self._quit_rounds = 1
            self._save_runtime()
            cancelled = self.executor.cancel_pending_entries() if self.executor else 0
            log.warning("quit by %s: closing %d position(s) on %s, cancelled %d working entr%s",
                        operator, len(held), self._venue, cancelled, "y" if cancelled == 1 else "ies")
            self._publish("quit.started", quit=self._quit_status())
            results = self._close_all(held, reason="quit")
            self._quit_retry_at = time.monotonic() + self.QUIT_RETRY_S
        self._check_quit_progress()
        failed = [r for r in results if not r["ok"]]
        if not held:
            note = (f"Keeping {len(kept)} swing position(s) open with their stops at the broker - shutting down."
                    if kept else "No open positions - shutting down.")
        elif failed:
            note = (f"Exits sent for {len(held) - len(failed)} of {len(held)} positions; retrying "
                    f"{', '.join(r['symbol'] for r in failed)}. The app stays locked until all are out.")
        else:
            note = f"Exit sent for {len(held)} position(s). Shutting down once they've all closed."
        return {"ok": True, "note": note, "results": results, "quit": self._quit_status()}

    def _exits_cant_fill(self) -> Optional[str]:
        executor = self.executor
        return executor._exchange_closed() if executor is not None else None

    def cancel_quit(self, operator: str = "operator") -> Dict[str, Any]:
        """Stop a quit in progress: the positions still open stay open and managed, and the app
        unlocks. For a quit that can't finish - the market closed under it - or a change of mind."""
        with self._switch_lock:
            if not self.quit_state:
                return {"ok": False, "reason": "The app isn't quitting."}
            left = len(self._to_close())
            self.quit_state = None
            self._save_runtime()
        log.warning("quit cancelled by %s with %d position(s) still open", operator, left)
        self._publish("quit.cancelled", left=left)
        self._publish("account.snapshot", state=self.snapshot())
        return {"ok": True, "note": f"Quit cancelled - {left} position(s) stay open and managed."}

    def _quit_status(self) -> Optional[Dict[str, Any]]:
        if not self.quit_state:
            return None
        left = self._to_close()
        return {**self.quit_state, "left": len(left), "symbols": sorted({t["symbol"] for t in left}),
                "waiting": self._exits_cant_fill() if left else None}

    def _to_close(self) -> List[Dict[str, Any]]:
        """The positions a quit in progress still has to get out of - not the ones it keeps."""
        keeping = set((self.quit_state or {}).get("keeping") or ())
        return [t for t in self._positions_here() if t["id"] not in keeping]

    def _check_quit_progress(self) -> None:
        if not self.quit_state:
            return
        with self._quit_lock:
            if not self.quit_state:
                return
            left = self._to_close()
            if left and time.monotonic() >= self._quit_retry_at:
                # a simulator close can only fail on a missing price; after a retry the
                # reset wipes those positions anyway, so don't stay stuck
                if self.quit_state.get("reset_sim") and self._quit_rounds >= 2:
                    left = []
                else:
                    busy = self.executor.pending_exit_trade_ids()
                    retry = [t for t in left if t["id"] not in busy]
                    if retry:
                        self._close_all(retry, reason="quit")
                        self._quit_rounds += 1
                    self._quit_retry_at = time.monotonic() + self.QUIT_RETRY_S
                    left = self._to_close()
            if left:
                self._publish("quit.progress", quit=self._quit_status())
                return

            state, self.quit_state = self.quit_state, None
            keeping = len(state.get("keeping") or ())
            note = ("All positions are closed." if not keeping else
                    f"{keeping} swing position{'s stay' if keeping != 1 else ' stays'} open, each with its stop order "
                    "resting at the broker. The app picks them up again when it starts.")
            if state.get("reset_sim") and self._venue == "paper":
                cash = self.settings.config.account.paper_start_cash
                self._reset_simulator(cash)
                self.board.clear()
                note += f" Paper account reset to ${cash:,.0f}."
            self._save_runtime()
        log.warning("quit finished: %s", note)
        self._publish("quit.done", note=note)
        if self.on_shutdown is not None:
            threading.Timer(1.5, self.on_shutdown).start()      # let the message reach the browser
