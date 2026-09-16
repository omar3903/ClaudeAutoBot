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
from typing import Any, Dict, Optional

from ..brokers.venues import venue_label

log = logging.getLogger(__name__)


class QuitOps:
    # ------------------------------------------------------------------ #
    #  Quitting                                                          #
    # ------------------------------------------------------------------ #
    def quit_preview(self) -> Dict[str, Any]:
        brief = [{"id": t["id"], "symbol": t["symbol"], "side": t["side"], "quantity": t["quantity"],
                  "entry_price": t["entry_price"], "venue": t.get("broker") or "paper"}
                 for t in self._open_trades()]
        here = [b for b in brief if b["venue"] == self._venue]
        return {
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

    def begin_quit(self, close_all: bool = True, operator: str = "operator") -> Dict[str, Any]:
        """Paper: close everything, reset the simulator, shut down. Live: close
        everything and shut down once flat (``close_all=False`` cancels). Until the
        last position is out, nothing but exits may change."""
        with self._switch_lock:
            if self.quit_state:
                return {"ok": True, "note": "Already closing out before quitting.", "quit": self._quit_status()}
            held = self._positions_here()
            if self.mode == "live" and held and not close_all:
                return {"ok": False, "reason": "Quit cancelled - your live positions stay open and managed."}
            self.quit_state = {"started_at": dt.datetime.now(dt.timezone.utc).isoformat(), "mode": self.mode,
                               "venue": self._venue, "by": operator, "reset_sim": self._venue == "paper"}
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
            note = "No open positions - shutting down."
        elif failed:
            note = (f"Exits sent for {len(held) - len(failed)} of {len(held)} positions; retrying "
                    f"{', '.join(r['symbol'] for r in failed)}. The app stays locked until all are out.")
        else:
            note = f"Exit sent for {len(held)} position(s). Shutting down once they've all closed."
        return {"ok": True, "note": note, "results": results, "quit": self._quit_status()}

    def _quit_status(self) -> Optional[Dict[str, Any]]:
        if not self.quit_state:
            return None
        left = self._positions_here()
        return {**self.quit_state, "left": len(left), "symbols": sorted({t["symbol"] for t in left})}

    def _check_quit_progress(self) -> None:
        if not self.quit_state:
            return
        with self._quit_lock:
            if not self.quit_state:
                return
            left = self._positions_here()
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
                    left = self._positions_here()
            if left:
                self._publish("quit.progress", quit=self._quit_status())
                return

            state, self.quit_state = self.quit_state, None
            note = "All positions are closed."
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
