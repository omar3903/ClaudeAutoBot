from __future__ import annotations

from tos_bot.core.enums import Side, StrategyKind, Timeframe
from tos_bot.core.models import Play


def _play(**kw):
    d = dict(symbol="ZZZ", side=Side.LONG, strategy="vwap_reclaim",
             kind=StrategyKind.TECHNICAL, timeframe=Timeframe.INTRADAY,
             entry=100.0, stop=98.0, targets=[104.0])
    d.update(kw)
    p = Play(**d)
    p.suggested_qty = 10
    p.expected_hold_typical = kw.get("expected_hold_typical", 60.0)
    p.expected_hold_max = kw.get("expected_hold_max", 120.0)
    return p


def test_expected_exit_times_recorded(repo):
    p = _play(symbol="TIMED", timeframe=Timeframe.SWING,
              expected_hold_typical=5.0, expected_hold_max=12.0)
    repo.record_play(p)
    tid = repo.open_trade(p, 100.0, 3, "paper")
    t = repo.get_trade(tid)
    assert t["expected_exit_at"] and t["overwatch_at"]
    assert t["overwatch_at"] > t["expected_exit_at"]
    assert t["time_status"] == "on_track"          # just opened
    assert t["held_label"]


def test_trade_lifecycle_and_pnl(repo):
    before = repo.pnl_summary()                      # other tests share this database: count what this one adds
    p = _play()
    repo.record_play(p)
    tid = repo.open_trade(p, fill_price=100.0, fill_qty=10, broker="paper")
    out = repo.close_trade(tid, exit_price=104.0, exit_reason="target")
    assert out["realized_pl"] == 40.0
    assert out["r_multiple"] == 2.0                 # (4 reward) / (2 risk)
    assert out["is_day_trade"] is True
    s = repo.pnl_summary()
    assert s["n_closed"] == before["n_closed"] + 1
    assert abs(s["realized_total"] - before["realized_total"] - 40.0) < 1e-6


def test_short_trade_pnl(repo):
    p = _play(symbol="SHRT", side=Side.SHORT, entry=50.0, stop=52.0, targets=[46.0])
    repo.record_play(p)
    tid = repo.open_trade(p, 50.0, 10, "paper")
    out = repo.close_trade(tid, 46.0, "target")
    assert out["realized_pl"] == 40.0              # short: profit when price falls


def test_day_trade_counter(repo):
    for i in range(2):
        p = _play(symbol=f"DT{i}")
        repo.record_play(p)
        tid = repo.open_trade(p, 100.0, 1, "paper")
        repo.close_trade(tid, 101.0, "target")
    assert repo.count_day_trades(5) >= 2
