"""The engine's side of the movers report: after the close it downloads the market's daily candles
(the download the next morning's scan would make) and adds the session's movers to the review; a
rebuild after a restart downloads them again - never while the market is open - or says why not."""

from __future__ import annotations

import datetime as dt
import threading
import time

from test_engine import _connect, engine, gateway, port  # noqa: F401 - pytest fixtures
from tos_bot.engine import journal_ops
from tos_bot.scanner import schedule
from tos_bot.util import clock


def _session(monkeypatch):
    """The last completed session, due for its review, with the market closed."""
    day = schedule.last_completed_session(clock.now_ny())
    monkeypatch.setattr(journal_ops, "review_day", lambda now, at: day)
    monkeypatch.setattr(clock, "is_market_open", lambda ts=None: False)
    return day


def _downloads(engine, monkeypatch, wait=0.0):
    """Every download of the market's daily candles, each taking ``wait`` seconds longer."""
    calls, real = [], engine.scanner.update_market_daily

    def counted(through):
        calls.append(through)
        time.sleep(wait)
        return real(through)
    monkeypatch.setattr(engine.scanner, "update_market_daily", counted)
    return calls


def test_after_the_close_the_market_is_downloaded_and_the_movers_join_the_review(engine, port, gateway, monkeypatch):
    day = _session(monkeypatch)
    engine._started_at -= engine.JOURNAL_GATEWAY_WAIT_S
    engine._review_if_due()                                              # IB Gateway is away: nothing to build yet
    assert engine.journal_review(day) is None and engine._movers_retry_at > time.monotonic()

    _connect(engine, port)
    engine._movers_retry_at = 0.0
    engine._review_if_due()
    movers = engine.journal_review(day)["movers"]
    rows = movers["gainers"] + movers["losers"]
    assert movers["ok"] and rows and engine._journal_checked == day and engine.scanner.market_daily[0] == day
    assert {r["bot"]["status"] for r in rows} == {"offline"}                # nothing was scanned that session
    assert any(bar == "1 day" for _, bar, _ in gateway.requests)
    assert "signals are off" in movers["news_note"]

    chart = engine.mover_chart(day, rows[0]["symbol"])
    assert chart["ok"] and chart["daily"] and chart["row"]["symbol"] == rows[0]["symbol"]
    assert not engine.mover_chart(day, "NOPE")["ok"]


def test_a_rebuild_after_a_restart_reads_the_candles_and_rebuilds_the_movers(engine, port, monkeypatch):
    day = _session(monkeypatch)
    _connect(engine, port)
    first = engine.review_session(day)["review"]["movers"]
    mover = (first["gainers"] + first["losers"])[0]
    assert first["ok"] and mover["bot"]["status"] == "offline"

    engine.scanner.market_daily = None                                   # the app restarted since
    calls = _downloads(engine, monkeypatch)
    monkeypatch.setattr(engine.repo, "trades_on", lambda d: [            # and the record changed: the mover was traded
        {"symbol": mover["symbol"], "side": "LONG", "strategy": "gap_and_go", "entry_price": 10.0,
         "exit_price": 11.0, "r_multiple": 1.0, "realized_pl": 10.0, "status": "CLOSED"}])
    movers = engine.review_session(day)["review"]["movers"]
    row = next(r for r in movers["gainers"] + movers["losers"] if r["symbol"] == mover["symbol"])
    assert calls == [day] and engine.scanner.market_daily[0] == day
    assert row["bot"]["status"] == "traded" and movers["summary"]["traded"] == 1 and not movers.get("stale")


def test_while_the_market_is_open_or_without_ib_gateway_a_rebuild_keeps_the_movers_marked_stale(engine, port,
                                                                                              monkeypatch):
    day = _session(monkeypatch)
    _connect(engine, port)
    engine.review_session(day)
    first = engine.journal.get(day)["movers"]                            # as it was kept
    built = dt.datetime.fromisoformat(first["built_at"]).astimezone(clock.NY).strftime("Built %a %H:%M ET, ")

    engine.scanner.market_daily = None                                   # the app restarted since
    calls = _downloads(engine, monkeypatch)
    monkeypatch.setattr(engine.repo, "pair_trades_closed_between", lambda first, last: [
        {"pair": "PRA/PRB", "side": "LONG_SPREAD", "r_multiple": 0.5, "realized_pl": 10.0}])   # something to review
    monkeypatch.setattr(clock, "is_market_open", lambda ts=None: True)   # the download shares IB with the orders
    movers = engine.review_session(day)["review"]["movers"]
    assert calls == [] and movers["stale"] and movers["stale_note"].startswith(built + "before this rebuild")
    assert "while the market is open" in movers["stale_note"]
    assert "between the close and 16:15 ET" in movers["stale_note"]    # before the button moves on to the new session
    assert {k: v for k, v in movers.items() if k not in ("stale", "stale_note")} == first
    assert engine.journal_review(day)["movers"]["stale"]                  # the saved report says so too

    monkeypatch.setattr(clock, "is_market_open", lambda ts=None: False)
    monkeypatch.setattr(engine.md, "_source", None)                      # closed, but IB Gateway is away
    movers = engine.review_session(day)["review"]["movers"]
    assert calls == [] and movers["stale"] and "IB Gateway wasn't connected" in movers["stale_note"]
    assert movers["built_at"] == first["built_at"]


def test_two_rebuilds_at_once_download_the_market_once(engine, port, monkeypatch):
    day = _session(monkeypatch)
    _connect(engine, port)
    calls = _downloads(engine, monkeypatch, wait=0.3)                    # the Rebuild button and the journal loop
    out = []
    threads = [threading.Thread(target=lambda: out.append(engine.review_session(day))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert calls == [day] and len(out) == 2 and all(o["ok"] and o["review"]["movers"]["ok"] for o in out)
