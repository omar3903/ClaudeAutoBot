"""One-click Schwab sign-in, with schwab-py's browser flow replaced by a fake."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from tos_bot.auth.schwab_login import SchwabLogin

FREE = lambda port: False  # noqa: E731


def _settings(tmp_path, **over):
    sec = dict(schwab_api_key="key", schwab_app_secret="secret",
               schwab_callback_url="https://127.0.0.1:8182")
    sec.update(over)
    ns = SimpleNamespace(**sec)
    ns.token_path_for = lambda broker: tmp_path / f"{broker}.token.json"
    return SimpleNamespace(secrets=ns)


def test_success_saves_token_runs_hook_and_reports(tmp_path):
    calls, hooked, events = [], [], []

    def flow(key, secret, callback, token_path, **kw):
        calls.append((key, secret, callback, kw))
        Path(token_path).write_text("{}")

    login = SchwabLogin(_settings(tmp_path), flow=flow, port_in_use=FREE,
                        on_success=lambda: hooked.append(1),
                        bus=SimpleNamespace(publish=lambda topic, **k: events.append(k["state"])))
    started = login.start()
    assert started["ok"] and started["login"]["state"] == "waiting"
    assert login.wait(3) == "ok"
    key, secret, callback, kw = calls[0]
    assert (key, secret, callback) == ("key", "secret", "https://127.0.0.1:8182")
    assert kw["interactive"] is False               # never blocks on a console prompt
    assert hooked == [1]
    assert events == ["waiting", "ok"]


def test_flow_errors_are_explained(tmp_path):
    class RedirectTimeoutError(Exception):
        pass

    def flow(*a, **k):
        raise RedirectTimeoutError("timed out")

    login = SchwabLogin(_settings(tmp_path), flow=flow, port_in_use=FREE)
    assert login.start()["ok"]
    assert login.wait(3) == "error" and "Timed out" in login.message


def test_no_token_after_the_flow_is_an_error(tmp_path):
    login = SchwabLogin(_settings(tmp_path), flow=lambda *a, **k: None, port_in_use=FREE)
    login.start()
    assert login.wait(3) == "error" and "didn't return a token" in login.message


def test_refuses_without_keys_or_with_a_bad_callback(tmp_path):
    ran = []
    flow = lambda *a, **k: ran.append(1)  # noqa: E731
    r = SchwabLogin(_settings(tmp_path, schwab_api_key=""), flow=flow, port_in_use=FREE).start()
    assert not r["ok"] and "app key" in r["reason"]
    r = SchwabLogin(_settings(tmp_path, schwab_callback_url="https://localhost:8182"),
                    flow=flow, port_in_use=FREE).start()
    assert not r["ok"] and "127.0.0.1" in r["reason"]
    assert ran == []


def test_refuses_when_the_port_is_busy_or_a_sign_in_is_open(tmp_path):
    r = SchwabLogin(_settings(tmp_path), flow=lambda *a, **k: None, port_in_use=lambda p: True).start()
    assert not r["ok"] and "8182" in r["reason"]

    gate = threading.Event()

    def slow(key, secret, callback, token_path, **kw):
        gate.wait(3)
        Path(token_path).write_text("{}")

    login = SchwabLogin(_settings(tmp_path), flow=slow, port_in_use=FREE)
    assert login.start()["ok"]
    second = login.start()
    assert not second["ok"] and "already open" in second["reason"]
    gate.set()
    assert login.wait(3) == "ok"
