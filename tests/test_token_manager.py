"""Schwab token upkeep: 7-day refresh token, a reminder a day early, removal once expired."""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

from tos_bot.auth.token_manager import TokenManager


def _tm(tmp_path, events):
    auth = SimpleNamespace(refresh_token_ttl_days=7, rotate_before_days=1,
                           access_refresh_margin_seconds=120, backup_old_tokens=True)
    secrets = SimpleNamespace(token_path_for=lambda broker: tmp_path / f"{broker}.token.json")
    settings = SimpleNamespace(config=SimpleNamespace(auth=auth), secrets=secrets)
    return TokenManager(settings, bus=SimpleNamespace(publish=lambda topic, **k: events.append(topic)))


def _signed_in(tm, days_ago):
    tm.token_path.write_text("{}")
    issued = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)
    tm.meta_path.write_text(json.dumps({"refresh_issued_at": issued.isoformat()}))


def test_never_signed_in_is_quiet(tmp_path):
    events = []
    st = _tm(tmp_path, events).watchdog_tick()
    assert not st.exists and st.needs_reauth and events == []


def test_healthy_token(tmp_path):
    events = []
    tm = _tm(tmp_path, events)
    _signed_in(tm, 3)
    st = tm.watchdog_tick()
    assert st.exists and not st.needs_rotation and not st.needs_reauth and events == []
    assert 3.9 < st.days_until_expiry < 4.1


def test_reminds_once_near_expiry_without_deleting(tmp_path):
    events = []
    tm = _tm(tmp_path, events)
    _signed_in(tm, 6.5)
    tm.watchdog_tick()
    tm.watchdog_tick()
    assert events == ["auth.reauth_required"]          # once, not on every tick
    assert tm.token_path.exists()                       # still valid for another half day


def test_expired_token_is_backed_up_and_removed(tmp_path):
    events = []
    tm = _tm(tmp_path, events)
    _signed_in(tm, 8)
    st = tm.watchdog_tick()
    assert not tm.token_path.exists() and not st.exists
    assert list((tmp_path / "backups").glob("schwab_*/schwab.token.json"))
    assert events == ["auth.reauth_required"]


def test_signing_in_resets_the_clock(tmp_path):
    tm = _tm(tmp_path, [])
    _signed_in(tm, 6.5)
    tm.note_full_auth("test")
    st = tm.status()
    assert not st.needs_rotation and st.days_until_expiry > 6.9
