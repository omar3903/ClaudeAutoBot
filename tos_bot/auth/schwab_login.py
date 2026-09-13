"""One-click Schwab / thinkorswim sign-in.

Schwab's API uses OAuth: you log in on schwab.com, Schwab redirects your
browser to the app's callback URL with a one-time code, and that code is
swapped for a token. ``schwab-py`` does the work - it starts a small HTTPS
server on the callback port (``https://127.0.0.1:8182``), opens the Schwab
login page in your browser, catches the redirect and writes the token file.
This class runs that flow in the background, one at a time, and reports its
progress to the dashboard.

The refresh token lasts 7 days, so expect to sign in about once a week; the
30-minute access token is refreshed automatically in between. Your browser
warns about the callback server's self-signed certificate - expected, since
that page is served by this computer.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from ..util.net import port_is_open

log = logging.getLogger(__name__)

CALLBACK_TIMEOUT_S = 300.0


class SchwabLogin:
    def __init__(
        self,
        settings,
        bus=None,
        on_success: Optional[Callable[[], None]] = None,
        flow: Optional[Callable[..., Any]] = None,
        port_in_use: Optional[Callable[[int], bool]] = None,
    ) -> None:
        self.settings = settings
        self.bus = bus
        self.on_success = on_success
        self._flow = flow                      # injectable for tests
        self._port_in_use = port_in_use or (lambda p: port_is_open("127.0.0.1", p))
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.state = "idle"                    # idle | waiting | ok | error
        self.message = ""
        self.started_at: Optional[dt.datetime] = None
        self.finished_at: Optional[dt.datetime] = None

    # ------------------------------------------------------------------ #
    def status(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "message": self.message,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }

    def problems(self) -> List[str]:
        sec = self.settings.secrets
        out = []
        if not (sec.schwab_api_key and sec.schwab_app_secret):
            out.append("Add your Schwab app key and secret first.")
        u = urlparse(sec.schwab_callback_url or "")
        try:
            port = u.port
        except ValueError:
            port = None
        if u.scheme != "https" or u.hostname != "127.0.0.1" or not port:
            out.append("The callback URL must be https://127.0.0.1:<port> and match your Schwab app.")
        return out

    def start(self) -> Dict[str, Any]:
        with self._lock:
            if self.state == "waiting":
                return self._refuse("A Schwab sign-in is already open - finish it in the browser tab.")
            problems = self.problems()
            if problems:
                return self._refuse(" ".join(problems))
            port = urlparse(self.settings.secrets.schwab_callback_url).port
            if self._port_in_use(port):
                return self._refuse(f"Port {port} is already in use - close whatever is using it "
                                    f"and try again.")
            try:
                flow = self._flow or _schwab_flow()
            except RuntimeError as e:
                return self._refuse(str(e))
            self.state = "waiting"
            self.message = ("Schwab's login page is opening in your browser. Log in, allow access, "
                            "then continue past the certificate warning for 127.0.0.1.")
            self.started_at, self.finished_at = _now(), None
            self._thread = threading.Thread(target=self._run, args=(flow,), name="schwab-login",
                                            daemon=True)
            self._thread.start()
        self._publish()
        return {"ok": True, "note": self.message, "login": self.status()}

    def wait(self, timeout: Optional[float] = None) -> str:
        """Block until the sign-in finishes (for the CLI and tests). Returns the final state."""
        if self._thread is not None:
            self._thread.join(timeout)
        return self.state

    # ------------------------------------------------------------------ #
    def _run(self, flow: Callable[..., Any]) -> None:
        sec = self.settings.secrets
        token_path = Path(sec.token_path_for("schwab"))
        token_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            flow(sec.schwab_api_key, sec.schwab_app_secret, sec.schwab_callback_url,
                 str(token_path), interactive=False, callback_timeout=CALLBACK_TIMEOUT_S)
            if not token_path.exists():
                raise RuntimeError("Schwab didn't return a token.")
            self.state, self.message = "ok", "Signed in to Schwab - token saved. It lasts 7 days."
            log.info("Schwab sign-in complete")
        except Exception as e:  # noqa: BLE001
            self.state, self.message = "error", _explain(e)
            log.warning("Schwab sign-in failed: %s", e)
        self.finished_at = _now()
        if self.state == "ok" and self.on_success:
            try:
                self.on_success()
            except Exception:  # noqa: BLE001
                log.exception("post sign-in hook failed")
        self._publish()

    def _refuse(self, reason: str) -> Dict[str, Any]:
        return {"ok": False, "reason": reason, "login": self.status()}

    def _publish(self) -> None:
        if self.bus is not None:
            self.bus.publish("auth.schwab_login", **self.status())


def _schwab_flow() -> Callable[..., Any]:
    try:
        from schwab.auth import client_from_login_flow
    except ImportError as e:
        raise RuntimeError("schwab-py isn't installed - run:  pip install schwab-py") from e
    return client_from_login_flow


def _explain(e: Exception) -> str:
    name, text = type(e).__name__, str(e)
    if name == "RedirectTimeoutError":
        return ("Timed out after 5 minutes waiting for Schwab to redirect back. Click Sign in again "
                "and finish the login in the browser tab.")
    if name == "RedirectServerExitedError":
        return "The local callback server couldn't start - is the callback port free?"
    low = text.lower()
    if "redirect_uri" in low or "callback" in low:
        return "Schwab says the callback URL doesn't match your app's settings."
    if "invalid_client" in low or "unauthorized" in low or "401" in low:
        return "Schwab rejected the app key or secret (or the app isn't approved yet)."
    return f"Schwab sign-in failed: {text[:200]}"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)
