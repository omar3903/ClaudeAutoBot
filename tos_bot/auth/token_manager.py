"""Schwab OAuth token upkeep. IBKR (its login is the running Gateway) and the
built-in simulator need no token.

* The 30-minute **access token** is refreshed by schwab-py itself; the watchdog
  makes a cheap authenticated call so that happens in good time.
* The **refresh token** lasts 7 days (``auth.refresh_token_ttl_days``). From
  ``auth.rotate_before_days`` before expiry the dashboard reminds you to sign in
  again; once it has really expired the token file is backed up and removed so
  nothing keeps using a dead credential.
* Signing in again is one click - see :mod:`tos_bot.auth.schwab_login`.
* Sign-ins, refreshes and expiries go to the ``token_audit`` table.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)


@dataclass
class TokenStatus:
    exists: bool
    broker: str
    refresh_ttl_days: int
    rotate_before_days: int
    refresh_issued_at: Optional[dt.datetime] = None
    refresh_age_days: Optional[float] = None
    days_until_rotation: Optional[float] = None
    days_until_expiry: Optional[float] = None
    last_access_refresh: Optional[dt.datetime] = None
    needs_rotation: bool = False       # expiry is close - remind the operator
    needs_reauth: bool = False         # expired, or never signed in
    message: str = ""

    def as_dict(self) -> dict:
        return {k: (v.isoformat() if isinstance(v, dt.datetime) else v)
                for k, v in self.__dict__.items()}


class TokenManager:
    broker = "schwab"

    def __init__(
        self,
        settings,
        repo=None,
        bus=None,
        on_reauth_required: Optional[Callable[[TokenStatus], None]] = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.bus = bus
        self.on_reauth_required = on_reauth_required

        acfg = settings.config.auth
        self.ttl_days = int(acfg.refresh_token_ttl_days)
        self.rotate_before_days = int(acfg.rotate_before_days)
        self.access_margin_s = int(acfg.access_refresh_margin_seconds)
        self.backup_old = bool(acfg.backup_old_tokens)

        self._last_access_refresh: Optional[dt.datetime] = None
        self._reminded_for: Optional[str] = None     # token issue time we already reminded about
        self._lock = threading.RLock()

    # paths follow the live settings, so a TOKEN_DIR change applies at once
    @property
    def token_path(self) -> Path:
        return Path(self.settings.secrets.token_path_for(self.broker))

    @property
    def meta_path(self) -> Path:
        return self.token_path.with_suffix(".meta.json")

    @property
    def backup_dir(self) -> Path:
        return self.token_path.parent / "backups"

    # ------------------------------------------------------------------ #
    #  Meta (refresh-token issue time)                                   #
    # ------------------------------------------------------------------ #
    def _read_meta(self) -> dict:
        try:
            return json.loads(self.meta_path.read_text()) if self.meta_path.exists() else {}
        except Exception:  # noqa: BLE001
            return {}

    def _write_meta(self, **fields) -> None:
        meta = self._read_meta()
        meta.update(fields)
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)
        self.meta_path.write_text(json.dumps(meta, indent=2, default=str))

    def _refresh_issued_at(self) -> Optional[dt.datetime]:
        for key in ("refresh_issued_at", "creation_timestamp"):
            v = self._read_meta().get(key)
            if v:
                try:
                    return _parse_dt(v)
                except Exception:  # noqa: BLE001
                    pass
        if self.token_path.exists():
            try:
                blob = json.loads(self.token_path.read_text())
                if "creation_timestamp" in blob:          # schwab-py's token file
                    return _parse_dt(blob["creation_timestamp"])
            except Exception:  # noqa: BLE001
                pass
            return dt.datetime.fromtimestamp(self.token_path.stat().st_mtime, dt.timezone.utc)
        return None

    # ------------------------------------------------------------------ #
    #  Public API                                                        #
    # ------------------------------------------------------------------ #
    def note_full_auth(self, source: str = "manual") -> None:
        """Call right after a successful sign-in. Resets the 7-day clock."""
        self._write_meta(refresh_issued_at=_utcnow().isoformat(), last_full_auth_source=source)
        self._reminded_for = None
        self._audit("REAUTH_OK", detail=f"signed in via {source}")
        log.info("Schwab sign-in recorded (source=%s)", source)

    def note_access_refresh(self) -> None:
        self._last_access_refresh = _utcnow()
        self._write_meta(last_access_refresh=self._last_access_refresh.isoformat())
        self._audit("REFRESH", detail="access token refreshed")

    def status(self) -> TokenStatus:
        issued = self._refresh_issued_at()
        st = TokenStatus(
            exists=self.token_path.exists(), broker=self.broker,
            refresh_ttl_days=self.ttl_days, rotate_before_days=self.rotate_before_days,
            refresh_issued_at=issued,
            last_access_refresh=self._last_access_refresh
            or _maybe_dt(self._read_meta().get("last_access_refresh")),
        )
        if not st.exists:
            st.needs_reauth = True
            st.message = "not signed in to Schwab"
            return st
        if issued is None:
            st.message = "signed in (token age unknown)"
            return st
        age = (_utcnow() - issued).total_seconds() / 86400.0
        st.refresh_age_days = round(age, 2)
        st.days_until_expiry = round(self.ttl_days - age, 2)
        st.days_until_rotation = round(self.ttl_days - self.rotate_before_days - age, 2)
        st.needs_reauth = age >= self.ttl_days
        st.needs_rotation = not st.needs_reauth and age >= (self.ttl_days - self.rotate_before_days)
        if st.needs_reauth:
            st.message = "Schwab sign-in expired - sign in again"
        elif st.needs_rotation:
            st.message = f"Schwab sign-in expires in {st.days_until_expiry:.1f} days - sign in again soon"
        else:
            st.message = f"signed in ({st.days_until_expiry:.1f} days left)"
        return st

    def watchdog_tick(self, broker_adapter=None) -> TokenStatus:
        """Run periodically by :class:`AuthWatchdog`."""
        with self._lock:
            st = self.status()
            if not st.exists:
                return st                          # never signed in - nothing to maintain

            if broker_adapter is not None and hasattr(broker_adapter, "refresh_if_needed"):
                try:
                    if broker_adapter.refresh_if_needed(margin_s=self.access_margin_s):
                        self.note_access_refresh()
                except Exception as e:  # noqa: BLE001
                    log.warning("access-token refresh failed: %s", e)
                    self._audit("ERROR", detail=f"access refresh: {e}")

            if st.needs_reauth:
                self.expire_now(f"refresh token expired (age {st.refresh_age_days}d, "
                                f"ttl {self.ttl_days}d)")
            elif st.needs_rotation:
                issued = str(st.refresh_issued_at)
                if self._reminded_for != issued:  # remind once per token
                    self._reminded_for = issued
                    self._signal_reauth(st, st.message)
            return self.status()

    def expire_now(self, reason: str) -> None:
        """Back up and remove the token, then ask the operator to sign in again."""
        with self._lock:
            st = self.status()
            log.warning("Schwab token expired: %s", reason)
            self._audit("ROTATE", age_days=st.refresh_age_days,
                        expires_at=_add_days(st.refresh_issued_at, self.ttl_days), detail=reason)
            if self.backup_old and self.token_path.exists():
                dest = self.backup_dir / f"{self.broker}_{_utcnow().strftime('%Y%m%dT%H%M%SZ')}"
                dest.mkdir(parents=True, exist_ok=True)
                for p in (self.token_path, self.meta_path):
                    if p.exists():
                        shutil.copy2(p, dest / p.name)
            try:
                self.token_path.unlink(missing_ok=True)
            except OSError as e:
                log.error("could not delete token file: %s", e)
            self._signal_reauth(self.status(), reason)

    # ------------------------------------------------------------------ #
    def _signal_reauth(self, st: TokenStatus, reason: str) -> None:
        self._audit("REAUTH_REQUIRED", detail=reason)
        if self.bus:
            self.bus.publish("auth.reauth_required", broker=self.broker, reason=reason,
                             status=st.as_dict())
        if self.on_reauth_required:
            try:
                self.on_reauth_required(st)
            except Exception:  # noqa: BLE001
                log.exception("on_reauth_required callback failed")

    def _audit(self, event: str, age_days: Optional[float] = None,
               expires_at: Optional[dt.datetime] = None, detail: str = "") -> None:
        if self.repo is None:
            return
        try:
            self.repo.record_token_event(self.broker, event, age_days, expires_at, detail)
        except Exception:  # noqa: BLE001
            log.debug("token audit write failed", exc_info=True)


# --------------------------------------------------------------------------- #
class AuthWatchdog(threading.Thread):
    """Background loop that calls :meth:`TokenManager.watchdog_tick`."""

    def __init__(self, manager: TokenManager, broker_provider: Callable[[], object],
                 interval_s: int = 1800) -> None:
        super().__init__(name="auth-watchdog", daemon=True)
        self.manager = manager
        self.broker_provider = broker_provider
        self.interval_s = max(60, int(interval_s))
        self._stop = threading.Event()

    def run(self) -> None:
        log.info("auth watchdog started (every %ss)", self.interval_s)
        self._tick()
        while not self._stop.wait(self.interval_s):
            self._tick()

    def _tick(self) -> None:
        try:
            broker = None
            try:
                broker = self.broker_provider()
            except Exception:  # noqa: BLE001
                pass
            st = self.manager.watchdog_tick(broker)
            log.debug("auth watchdog: %s", st.message)
        except Exception:  # noqa: BLE001
            log.exception("auth watchdog tick failed")

    def stop(self) -> None:
        self._stop.set()


# --------------------------------------------------------------------------- #
def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_dt(v) -> dt.datetime:
    if isinstance(v, (int, float)):
        return dt.datetime.fromtimestamp(float(v), dt.timezone.utc)
    d = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def _maybe_dt(v) -> Optional[dt.datetime]:
    try:
        return _parse_dt(v) if v else None
    except Exception:  # noqa: BLE001
        return None


def _add_days(d: Optional[dt.datetime], days: int) -> Optional[dt.datetime]:
    return (d + dt.timedelta(days=days)) if d else None
