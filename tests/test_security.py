"""Same-machine guard: on every request, and again (header included) on the secrets endpoints; the
server run.py starts, which only listens on this computer and never trusts proxy headers; and the log,
which masks the IBKR account id and which the tests write to their own folder."""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import logging
import os
import pathlib
import re
import sys
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient

from autotradebot.server import security

OK = {"X-ATB-Request": "1"}
HOME = {"Host": "127.0.0.1:8787"}           # the dashboard as run.py opens it


def _app() -> FastAPI:
    app = FastAPI()

    @app.post("/secret", dependencies=[Depends(security.require_local)])
    def secret():
        return {"ok": True}

    return app


@pytest.fixture
def client(monkeypatch):
    # TestClient reports client "testclient" and Host "testserver"
    monkeypatch.setattr(security, "ALLOWED_CLIENTS", security.ALLOWED_CLIENTS | {"testclient"})
    monkeypatch.setattr(security, "ALLOWED_HOSTS", security.ALLOWED_HOSTS | {"testserver"})
    return TestClient(_app())


def test_dashboard_request_is_allowed(client):
    assert client.post("/secret", headers=OK).status_code == 200


def test_missing_header_is_refused(client):
    assert client.post("/secret").status_code == 403


def test_foreign_origin_is_refused(client):
    assert client.post("/secret", headers={**OK, "Origin": "https://evil.example"}).status_code == 403


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8787", "http://localhost:8787"])
def test_local_origin_is_allowed(client, origin):
    # the dashboard's own page, by either of this computer's names, on the same port
    assert client.post("/secret", headers={**OK, **HOME, "Origin": origin}).status_code == 200


def test_a_page_on_another_local_port_is_refused(client):
    # a dev server or a notebook on this computer: the same site to the browser, but not the dashboard
    assert client.post("/secret", headers={**OK, **HOME, "Origin": "http://localhost:3000"}).status_code == 403
    assert client.post("/secret", headers={**OK, "Sec-Fetch-Site": "same-site"}).status_code == 403
    assert client.post("/secret", headers={**OK, "Sec-Fetch-Site": "same-origin"}).status_code == 200


def test_rebound_host_is_refused(client):
    assert client.post("/secret", headers={**OK, "Host": "evil.example:8787"}).status_code == 403


def test_remote_client_is_refused(monkeypatch):
    monkeypatch.setattr(security, "ALLOWED_HOSTS", security.ALLOWED_HOSTS | {"testserver"})
    assert TestClient(_app()).post("/secret", headers=OK).status_code == 403


def test_the_share_count_fix_is_same_machine_only():
    # it books P/L and can send a market exit; the guard answers before the handler needs an engine
    from autotradebot.server.app import create_app

    client = TestClient(create_app(lambda settings: None))
    assert client.get("/api/positions/mismatch/AAA", headers=OK).status_code == 403
    assert client.post("/api/positions/mismatch/AAA/fix", json={"action": "match"}, headers=OK).status_code == 403


# ---- every request: the app's middleware ------------------------------------------------------ #
class _Engine:
    """Records what reached it: a refused request must be answered before the engine is asked."""

    def __init__(self):
        self.calls = []

    def _ok(self, *call):
        self.calls.append(call)
        return {"ok": True}

    def approve_play(self, play_id):
        return self._ok("approve", play_id)

    def close_untracked(self, symbol):
        return self._ok("close_untracked", symbol)

    def set_mode(self, mode):
        return self._ok("mode", mode)

    def snapshot(self):
        return self._ok("snapshot")

    def price_of(self, symbol):
        return self._ok("price", symbol)

    def current_plays(self):
        self.calls.append(("plays",))
        return []


def _dashboard(monkeypatch, *, local_client=True):
    """The real app on a stub engine, TestClient's host allowed as this computer (and its client too,
    unless the test wants a remote one)."""
    from autotradebot.server.app import create_app

    if local_client:
        monkeypatch.setattr(security, "ALLOWED_CLIENTS", security.ALLOWED_CLIENTS | {"testclient"})
    monkeypatch.setattr(security, "ALLOWED_HOSTS", security.ALLOWED_HOSTS | {"testserver"})
    engine = _Engine()
    app = create_app(lambda settings: None)
    app.state.engine = engine
    return TestClient(app), engine


def test_the_dashboards_own_requests_get_through(monkeypatch):
    client, engine = _dashboard(monkeypatch)
    assert client.post("/api/plays/P01/approve", headers=OK).status_code == 200
    assert client.get("/api/state").status_code == 200                 # a read needs no header
    assert client.get("/api/price/AAA", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert client.get("/").status_code == 200
    assert engine.calls == [("approve", "P01"), ("snapshot",), ("price", "AAA")]


def test_a_cross_site_form_post_is_refused(monkeypatch):
    # a plain HTML form on another website: no custom header, a foreign Origin
    client, engine = _dashboard(monkeypatch)
    evil = {"Origin": "https://evil.example"}
    answer = client.post("/api/plays/P01/approve", data={"x": "1"}, headers=evil)
    assert answer.status_code == 403
    assert isinstance(answer.json()["detail"], str)         # the dashboard shows the reason
    # a foreign Origin is refused even with the header
    assert client.post("/api/plays/P01/approve", headers={**OK, **evil}).status_code == 403
    assert engine.calls == []


def test_a_post_without_the_dashboard_header_is_refused(monkeypatch):
    client, engine = _dashboard(monkeypatch)
    assert client.post("/api/positions/untracked/AAA/close").status_code == 403
    assert engine.calls == []


def test_a_rebound_host_is_refused_on_every_route(monkeypatch):
    client, engine = _dashboard(monkeypatch)
    rebound = {"Host": "rebind.evil:8787"}
    assert client.post("/api/mode", json={"mode": "live"}, headers={**OK, **rebound}).status_code == 403
    assert client.get("/api/state", headers=rebound).status_code == 403
    assert engine.calls == []


def test_only_the_dashboards_own_origin_gets_through(monkeypatch):
    client, engine = _dashboard(monkeypatch)
    for host in ("127.0.0.1:8787", "localhost:8787"):
        for origin in ("http://127.0.0.1:8787", "http://localhost:8787"):
            own = {**OK, "Host": host, "Origin": origin, "Sec-Fetch-Site": "same-origin"}
            assert client.post("/api/plays/P01/approve", headers=own).status_code == 200, (host, origin)
    # a script sends no Origin and no Sec-Fetch-Site, only the dashboard's header
    assert client.post("/api/plays/P02/approve", headers=OK).status_code == 200
    # a page another program serves on this computer, with the header too
    other = {**HOME, "Origin": "http://localhost:3000"}
    assert client.post("/api/plays/P03/approve", headers={**OK, **other}).status_code == 403
    assert client.get("/api/state", headers=other).status_code == 403
    # an <img> or <script> on it sends no Origin, but the browser marks it same-site
    assert client.get("/api/price/AAA", headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
    assert engine.calls == [("approve", "P01")] * 4 + [("approve", "P02")]


def test_another_sites_page_cant_load_the_api(monkeypatch):
    # an <img> or <script> on another website sends no Origin, but the browser marks it cross-site
    client, engine = _dashboard(monkeypatch)
    cross = {"Sec-Fetch-Site": "cross-site"}
    assert client.get("/api/price/AAA", headers=cross).status_code == 403
    assert engine.calls == []
    # a link from another website still opens the dashboard itself
    assert client.get("/", headers=cross).status_code == 200


def test_a_remote_client_is_refused_on_every_route(monkeypatch):
    client, engine = _dashboard(monkeypatch, local_client=False)
    assert client.get("/api/state").status_code == 403
    assert client.get("/").status_code == 403
    assert engine.calls == []


def test_no_other_website_can_frame_the_dashboard(monkeypatch):
    # inside another site's hidden frame the user's clicks would be the dashboard's own, and pass every check
    client, engine = _dashboard(monkeypatch)
    engine.snapshot = lambda: {"ok": True, "rows": ["x" * 64] * 64}          # big enough to be gzipped
    gzip = {"Accept-Encoding": "gzip"}
    answers = {"/": client.get("/"), "main.js": client.get("/static/js/main.js"),
               "/api/state": client.get("/api/state", headers=gzip),
               "refused": client.post("/api/mode", json={"mode": "live"})}
    for name, answer in answers.items():
        assert answer.headers["x-frame-options"] == "DENY", name
        assert "frame-ancestors 'none'" in answer.headers["content-security-policy"], name
        assert answer.headers["x-content-type-options"] == "nosniff", name
    assert answers["refused"].status_code == 403
    # the answers' own headers are kept
    assert answers["/api/state"].headers["content-encoding"] == "gzip"
    assert answers["/"].headers["cache-control"] == answers["main.js"].headers["cache-control"] == "no-cache"


def test_the_page_runs_only_the_dashboards_own_script_files(monkeypatch):
    # a headline that ever reached the page unescaped couldn't run as script and drive the API
    client, _ = _dashboard(monkeypatch)
    policy = client.get("/").headers["content-security-policy"]
    rules = dict(rule.strip().split(" ", 1) for rule in policy.split(";"))
    assert rules["script-src"] == "'self'" and rules["object-src"] == "'none'"
    assert rules["frame-ancestors"] == "'none'"
    theme = client.get("/static/js/theme.js")                     # the theme is picked by a file now
    assert theme.status_code == 200 and "javascript" in theme.headers["content-type"]


def test_the_dashboard_has_no_inline_script():
    # the policy blocks inline script, so any left behind would quietly stop working: the theme picked
    # after first paint, or Enter in a settings field reloading the page and losing what was typed
    from autotradebot.server.app import WEB_DIR

    page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert all(" src=" in tag for tag in re.findall(r"<script\b[^>]*>", page))
    handler = re.compile(r"""\son[a-z]+\s*=\s*["'`]""")               # onsubmit="...", onclick='...'
    files = [path for path in WEB_DIR.rglob("*") if path.suffix in {".html", ".js"}]
    assert len(files) > 10
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert not handler.search(text) and "javascript:" not in text, path.name


# ---- the live feed: the WebSocket, which the HTTP middleware never sees ------------------------ #
def test_the_dashboards_own_live_feed_opens(monkeypatch):
    client, engine = _dashboard(monkeypatch)
    own = {**HOME, "Origin": "http://127.0.0.1:8787", "Sec-Fetch-Site": "same-origin"}
    with client.websocket_connect("/ws", headers=own) as ws:
        assert ws.receive_json()["topic"] == "hello"
        assert ws.receive_json()["topic"] == "plays.updated"
    assert engine.calls == [("snapshot",), ("plays",)]


@pytest.mark.parametrize("headers", [{"Origin": "https://evil.example"},      # another website's page
                                     {"Host": "rebind.evil:8787"},            # a rebound domain
                                     {**HOME, "Origin": "http://localhost:3000"},   # another program's page
                                     {"Sec-Fetch-Site": "same-site"}])        # ...as the browser marks it
def test_a_foreign_live_feed_is_refused_before_the_snapshot(monkeypatch, headers):
    client, engine = _dashboard(monkeypatch)
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect("/ws", headers=headers):
            pass
    assert refused.value.code == 1008
    assert engine.calls == []


def test_a_remote_live_feed_is_refused(monkeypatch):
    client, engine = _dashboard(monkeypatch, local_client=False)
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect("/ws"):
            pass
    assert refused.value.code == 1008 and engine.calls == []


def test_an_ipv6_loopback_request_is_allowed():
    host = {"host": "[::1]:8787"}
    assert security.refusal("::1", host, "POST", "/api/mode") == "Missing dashboard request header."
    assert security.refusal("::1", {**host, "x-atb-request": "1"}, "POST", "/api/mode") is None


def test_an_origin_must_be_this_requests_own():
    def why(origin, host="127.0.0.1:8787"):
        headers = {"host": host, "origin": origin, "x-atb-request": "1"}
        return security.refusal("127.0.0.1", headers, "POST", "/api/mode")

    # the same scheme, port and name - or another of this computer's names on the same port
    for origin, host in [("http://127.0.0.1:8787", "127.0.0.1:8787"), ("http://localhost:8787", "127.0.0.1:8787"),
                         ("http://[::1]:8787", "localhost:8787"), ("http://localhost", "localhost")]:
        assert why(origin, host) is None, origin
    refused = [("http://localhost:3000", "127.0.0.1:8787"),          # another program on this computer
               ("http://localhost:8787", "localhost"),               # the Host's port is the default, 80
               ("http://localhost", "localhost:8787"),
               ("https://127.0.0.1:8787", "127.0.0.1:8787"),         # other schemes
               ("ws://127.0.0.1:8787", "127.0.0.1:8787"),
               ("chrome-extension://abc", "127.0.0.1:8787"),
               ("null", "127.0.0.1:8787"),                           # a file, a sandboxed frame
               ("http://127.0.0.1:8787/", "127.0.0.1:8787"),         # not an origin as browsers send one
               ("http://x@127.0.0.1:8787", "127.0.0.1:8787"),
               ("http://127.0.0.1:port", "127.0.0.1:8787")]
    for origin, host in refused:
        assert why(origin, host) == "Cross-site request refused.", origin


def test_the_api_and_the_live_feed_take_only_the_dashboards_own_requests():
    home = {"host": "127.0.0.1:8787"}
    for site, allowed in [(None, True), ("none", True), ("same-origin", True),
                          ("same-site", False), ("cross-site", False), ("something-new", False)]:
        headers = home if site is None else {**home, "sec-fetch-site": site}
        for path in ("/api/state", "/ws"):
            assert (security.refusal("127.0.0.1", headers, "GET", path) is None) is allowed, (site, path)
    # a link from another page still opens the dashboard itself
    assert security.refusal("127.0.0.1", {**home, "sec-fetch-site": "same-site"}, "GET", "/") is None


# ---- the server itself: run.py and the app it serves ------------------------------------------ #
RUN_PY = pathlib.Path(__file__).resolve().parents[1] / "run.py"


@pytest.fixture
def run_py(monkeypatch):
    """run.py loaded as a module with the server and the steps around it stubbed: main() records the
    server it would start (or what it would hand uvicorn.run) instead of starting it."""
    from autotradebot.server.app import app

    spec = importlib.util.spec_from_file_location("atb_run", RUN_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    served = []

    class _Server:
        def __init__(self, config, app):
            served.append(config)

        def run(self):
            pass

    monkeypatch.setattr(module, "GuardedServer", _Server)
    monkeypatch.setattr(module, "setup_logging", lambda level: None)     # the real one stays for the whole run
    monkeypatch.setattr(module, "keep_awake", lambda: False)
    monkeypatch.setattr(module.uvicorn, "run", lambda target, **options: served.append(options))
    monkeypatch.setattr(module.uvicorn.Config, "configure_logging", lambda self: None)
    monkeypatch.setattr(app.state, "shutdown", app.state.shutdown)        # main() wires it to the stub server
    module.served = served
    return module


def _start(run_py, monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["run.py", "--no-browser", *args])
    run_py.main()
    return run_py.served


def test_the_server_never_trusts_proxy_headers(run_py, monkeypatch):
    # an X-Forwarded-For header could otherwise stand in for the address the same-machine check goes by
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    config = _start(run_py, monkeypatch)[0]
    assert config.host == "127.0.0.1"
    assert config.proxy_headers is False and config.forwarded_allow_ips == ""
    config.load()
    assert not isinstance(config.loaded_app, ProxyHeadersMiddleware)
    # the dev auto-reload starts its own server, the same way
    options = _start(run_py, monkeypatch, "--reload")[-1]
    assert options["reload"] is True
    assert options["proxy_headers"] is False and options["forwarded_allow_ips"] == ""


def test_the_dashboard_is_only_served_on_this_computers_own_names(run_py):
    for host in ("127.0.0.1", "localhost", "::1", "LOCALHOST"):
        assert run_py.host_problem(host) == "", host
    for host in ("0.0.0.0", "::", "", "192.168.1.20", "my-pc"):            # every device on the network
        problem = run_py.host_problem(host)
        assert repr(host) in problem and "--allow-network" in problem, host
        assert run_py.host_problem(host, allow_network=True) == "", host


def test_a_network_host_is_refused_before_anything_starts(run_py, monkeypatch, capsys):
    for args in (["--host", "0.0.0.0"], ["--host", "192.168.1.20", "--reload"]):
        with pytest.raises(SystemExit) as refused:
            _start(run_py, monkeypatch, *args)
        assert refused.value.code == run_py.REFUSED == 2                  # scripts/run_24_7.bat stops on it
        assert "--allow-network" in capsys.readouterr().err
    # WEB_HOST in .env is held to it too
    real = run_py.get_settings()
    lan = SimpleNamespace(secrets=real.secrets.model_copy(update={"web_host": "0.0.0.0"}), config=real.config)
    monkeypatch.setattr(run_py, "get_settings", lambda: lan)
    with pytest.raises(SystemExit):
        _start(run_py, monkeypatch)
    assert run_py.served == []
    # ...unless the network is asked for, out loud
    config = _start(run_py, monkeypatch, "--allow-network")[0]
    assert config.host == "0.0.0.0" and config.proxy_headers is False


def test_the_app_serves_no_generated_api_docs(monkeypatch):
    # they'd hand anything that reaches the port a map of every endpoint that trades
    client, engine = _dashboard(monkeypatch)
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert client.get(path).status_code == 404, path
    assert client.get("/").status_code == 200
    assert engine.calls == []


# ---- the log: where it is written, and what it never shows ------------------------------------ #
@pytest.fixture
def fresh_logging(monkeypatch):
    """logging_setup as at a first start, its console a string (``.sys.stdout``); the root logger is put back
    afterwards."""
    from autotradebot.util import logging_setup

    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    noisy = {name: logging.getLogger(name).level for name in ("httpx", "urllib3", "asyncio", "ib_async")}
    monkeypatch.setattr(logging_setup, "_CONFIGURED", False)
    monkeypatch.setattr(logging_setup, "sys", SimpleNamespace(stdout=io.StringIO(), stderr=io.StringIO()))
    yield logging_setup
    for handler in [h for h in root.handlers if h not in handlers]:
        root.removeHandler(handler)
        handler.close()
    root.setLevel(level)
    for name, was in noisy.items():
        logging.getLogger(name).setLevel(was)


def _flushed():
    for handler in logging.getLogger().handlers:
        handler.flush()


def test_the_log_goes_where_atb_log_dir_says_so_tests_never_write_yours(fresh_logging, tmp_path, monkeypatch):
    from autotradebot import config

    # conftest points it at this run's own folder: an engine a test starts logs there, not to logs/
    assert config.LOG_DIR == pathlib.Path(os.environ["ATB_LOG_DIR"])
    assert config.PROJECT_ROOT not in config.LOG_DIR.parents
    monkeypatch.setattr(fresh_logging, "LOG_DIR", tmp_path / "run" / "logs")
    fresh_logging.setup_logging("INFO")
    logging.getLogger("autotradebot.test").warning("a line for the log")
    _flushed()
    assert "a line for the log" in (tmp_path / "run" / "logs" / "autotradebot.log").read_text(encoding="utf-8")


def test_the_log_masks_every_ibkr_account_id(fresh_logging, tmp_path, monkeypatch):
    # ib_async's own warning prints the whole order and its fills when IBKR rejects or cancels one
    from ib_async import CommissionReport, Execution, Fill, Order, Position, Stock, Trade

    monkeypatch.setattr(fresh_logging, "LOG_DIR", tmp_path)
    fresh_logging.setup_logging("INFO")
    stock = Stock("AAA", "SMART", "USD")
    trade = Trade(contract=stock, order=Order(action="SELL", totalQuantity=10, orderType="MKT", account="DU1234567"))
    trade.fills.append(Fill(stock, Execution(acctNumber="DU1234567", shares=10), CommissionReport(),
                            dt.datetime(2026, 1, 2, 15, 0)))
    logging.getLogger("ib_async.wrapper").warning(f"Canceled order: {trade}")
    log = logging.getLogger("autotradebot.test")
    log.warning("positions: %s", [Position("U7654321", stock, 10, 5.0)])
    log.warning("connected to DUT765432")                                    # a letter more before the digits
    try:
        raise RuntimeError("refused for account F7654321")
    except RuntimeError:
        log.exception("a traceback names it too")
    log.warning("AAA order 1234567 filled - tag paper_f1234567, U.S. hours")      # nothing else looks like one
    _flushed()
    for text in ((tmp_path / "autotradebot.log").read_text(encoding="utf-8"), fresh_logging.sys.stdout.getvalue()):
        assert "account='…4567'" in text and "acctNumber='…4567'" in text
        assert "Position(account='…4321'" in text and "refused for account …4321" in text
        assert "connected to …5432" in text
        assert not re.search(r"DU1234567|U7654321|F7654321|DUT765432", text)
        assert "AAA order 1234567 filled - tag paper_f1234567, U.S. hours" in text
