"""OAuth token lifecycle - the "no manual handling" part of the brief.

What it does
------------
* keeps the short-lived **access token** fresh (delegated to the broker
  adapter, which uses the SDK's own refresh)
* tracks the **refresh token's** age and, ``rotate_before_days`` before it
  hits ``refresh_token_ttl_days`` (default 60), performs a rotation:
  backup -> delete the old token file -> trigger a fresh authentication
* rotation is either **headless** (``scripts/reauth_headless.py`` with
  Playwright + OS keyring, opt-in) or **notify** (raises an event; the
  dashboard shows a one-click "Re-authenticate" banner)
* every refresh / rotation / re-auth is written to the ``token_audit`` table

Security
--------
This module never asks for or stores your brokerage password. The headless
path is your own opt-in script reading your own OS keyring. The token file
can optionally be encrypted at rest with Fernet (``TOKEN_ENCRYPTION_KEY``).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)


class ReauthRequired(RuntimeError):
    """Raised / signalled when a full browser re-authentication is needed."""


def generate_fernet_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


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
    needs_rotation: bool = False
    needs_reauth: bool = False
    message: str = ""

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        for k, v in d.items():
            if isinstance(v, dt.datetime):
                d[k] = v.isoformat()
        return d


class TokenManager:
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

        self.broker = settings.secrets.broker
        self.token_path: Path = settings.secrets.token_path
        self.meta_path: Path = self.token_path.with_suffix(".meta.json")
        self.backup_dir: Path = self.token_path.parent / "backups"
        self.token_path.parent.mkdir(parents=True, exist_ok=True)

        acfg = settings.config.auth
        self.ttl_days = int(acfg.refresh_token_ttl_days)
        self.rotate_before_days = int(acfg.rotate_before_days)
        self.access_margin_s = int(acfg.access_refresh_margin_seconds)
        self.auto_reauth = str(acfg.auto_reauth)
        self.backup_old = bool(acfg.backup_old_tokens)

        self._last_access_refresh: Optional[dt.datetime] = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    #  Meta (refresh-token issue time)                                   #
    # ------------------------------------------------------------------ #
    def _read_meta(self) -> dict:
        if self.meta_path.exists():
            try:
                return json.loads(self.meta_path.read_text())
            except Exception:  # noqa: BLE001
                return {}
        return {}

    def _write_meta(self, **fields) -> None:
        meta = self._read_meta()
        meta.update(fields)
        self.meta_path.write_text(json.dumps(meta, indent=2, default=str))

    def _refresh_issued_at(self) -> Optional[dt.datetime]:
        meta = self._read_meta()
        for key in ("refresh_issued_at", "creation_timestamp"):
            v = meta.get(key)
            if v:
                try:
                    return _parse_dt(v)
                except Exception:  # noqa: BLE001
                    pass
        # fall back to the token file's own timestamp if the SDK wrote one
        if self.token_path.exists():
            try:
                blob = json.loads(self.token_path.read_text())
                for key in ("creation_timestamp", "issued_at", "refresh_token_issued"):
                    if key in blob:
                        return _parse_dt(blob[key])
            except Exception:  # noqa: BLE001
                pass
            return dt.datetime.fromtimestamp(self.token_path.stat().st_mtime, dt.timezone.utc)
        return None

    # ------------------------------------------------------------------ #
    #  Public API                                                        #
    # ------------------------------------------------------------------ #
    def note_full_auth(self, source: str = "manual") -> None:
        """Call right after a successful browser OAuth. Resets the clock."""
        now = _utcnow()
        self._write_meta(refresh_issued_at=now.isoformat(), last_full_auth_source=source)
        self._audit("REAUTH_OK", detail=f"full auth via {source}")
        log.info("token: full auth recorded (source=%s); 60-day clock reset", source)

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
            st.message = "no token on disk - authenticate once to begin"
            return st
        if issued is not None:
            age = (_utcnow() - issued).total_seconds() / 86400.0
            st.refresh_age_days = round(age, 2)
            st.days_until_expiry = round(self.ttl_days - age, 2)
            st.days_until_rotation = round(self.ttl_days - self.rotate_before_days - age, 2)
            st.needs_rotation = age >= (self.ttl_days - self.rotate_before_days)
            st.needs_reauth = age >= self.ttl_days
            if st.needs_reauth:
                st.message = "refresh token expired - re-authentication required"
            elif st.needs_rotation:
                st.message = f"rotation window open ({st.days_until_expiry:.1f}d to expiry)"
            else:
                st.message = f"healthy ({st.days_until_rotation:.1f}d until scheduled rotation)"
        else:
            st.message = "token present but issue time unknown - will rotate on schedule"
        return st

    def watchdog_tick(self, broker_adapter=None) -> TokenStatus:
        """Run periodically by :class:`AuthWatchdog`."""
        with self._lock:
            st = self.status()

            # 1) keep the access token alive
            if broker_adapter is not None and hasattr(broker_adapter, "refresh_if_needed"):
                try:
                    if broker_adapter.refresh_if_needed(margin_s=self.access_margin_s):
                        self.note_access_refresh()
                        st = self.status()
                except Exception as e:  # noqa: BLE001
                    log.warning("access-token refresh failed: %s", e)
                    self._audit("ERROR", detail=f"access refresh: {e}")

            # 2) 60-day rotation
            if st.needs_rotation or st.needs_reauth:
                reason = ("expired" if st.needs_reauth else "scheduled")
                self.rotate_now(f"{reason} refresh-token rotation "
                                f"(age {st.refresh_age_days}d / ttl {self.ttl_days}d)")
                st = self.status()
            return st

    def rotate_now(self, reason: str) -> None:
        """The "delete and get" step: back up, remove the old token, obtain a
        new one (headless) or ask the operator (notify)."""
        with self._lock:
            st = self.status()
            log.warning("token rotation triggered: %s", reason)
            self._audit("ROTATE",
                        age_days=st.refresh_age_days,
                        expires_at=_add_days(st.refresh_issued_at, self.ttl_days),
                        detail=reason)

            if self.backup_old and self.token_path.exists():
                stamp = _utcnow().strftime("%Y%m%dT%H%M%SZ")
                dest = self.backup_dir / f"{self.broker}_{stamp}"
                dest.mkdir(parents=True, exist_ok=True)
                for p in (self.token_path, self.meta_path):
                    if p.exists():
                        shutil.copy2(p, dest / p.name)
                log.info("old token backed up to %s", dest)

            # delete the live token so the next client build forces a fresh grant
            try:
                if self.token_path.exists():
                    self.token_path.unlink()
                log.info("old token file deleted")
            except OSError as e:
                log.error("could not delete token file: %s", e)

            if self.auto_reauth == "headless":
                self._run_headless_reauth(reason)
            else:
                self._signal_reauth(st, reason)

    # ------------------------------------------------------------------ #
    def _run_headless_reauth(self, reason: str) -> None:
        script = self.settings.project_root / "scripts" / "reauth_headless.py"
        if not script.exists():
            log.error("headless re-auth requested but %s is missing - falling back to notify", script)
            self._signal_reauth(self.status(), reason)
            return
        try:
            log.info("launching headless re-auth: %s", script)
            proc = subprocess.run([sys.executable, str(script)], capture_output=True,
                                  text=True, timeout=300)
            if proc.returncode == 0 and self.token_path.exists():
                self.note_full_auth(source="headless")
                if self.bus:
                    self.bus.publish("auth.reauth_ok", broker=self.broker, source="headless")
            else:
                log.error("headless re-auth failed rc=%s: %s", proc.returncode, proc.stderr[-500:])
                self._audit("ERROR", detail=f"headless rc={proc.returncode}")
                self._signal_reauth(self.status(), reason)
        except Exception as e:  # noqa: BLE001
            log.exception("headless re-auth crashed")
            self._audit("ERROR", detail=f"headless crash: {e}")
            self._signal_reauth(self.status(), reason)

    def _signal_reauth(self, st: TokenStatus, reason: str) -> None:
        self._audit("REAUTH_REQUIRED", detail=reason)
        if self.bus:
            self.bus.publish("auth.reauth_required", broker=self.broker,
                             reason=reason, status=st.as_dict())
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
        # first check almost immediately
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
