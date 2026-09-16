"""The engine's side of the movers report: after the close it downloads the market's daily candles
(the download the next morning's scan would make) and adds the session's movers to the review."""

from __future__ import annotations

import time

from test_engine import _connect, engine, gateway, port  # noqa: F401 - pytest fixtures
from tos_bot.engine import journal_ops
from tos_bot.scanner import schedule
from tos_bot.util import clock


def test_after_the_close_the_market_is_downloaded_and_the_movers_join_the_review(engine, port, gateway, monkeypatch):
    day = schedule.last_completed_session(clock.now_ny())
    monkeypatch.setattr(journal_ops, "review_day", lambda now, at: day)
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

    engine.scanner.market_daily = None                                   # the app restarted since
    monkeypatch.setattr(engine.repo, "pair_trades_closed_between", lambda first, last: [
        {"pair": "PRA/PRB", "side": "LONG_SPREAD", "r_multiple": 0.5, "realized_pl": 10.0}])
    assert engine.review_session(day)["review"]["movers"] == movers      # a rebuild keeps the movers it had
