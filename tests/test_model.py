"""The meta-label model (research/model.py): weights, training, the card, scoring, and what Autopilot does with it."""

from __future__ import annotations

import datetime as dt
import json
import logging
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("sklearn")

from autotradebot.research import model as meta  # noqa: E402
from autotradebot.research import validate  # noqa: E402

UTC = dt.timezone.utc


def _rows(n=1600, seed=3):
    """Plays whose outcome really depends on two readings together - something a line can't see:
    they pay when the reward:risk is high *and* the stated confidence is modest."""
    rng = np.random.default_rng(seed)
    start = dt.datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    rows = []
    for i in range(n):
        rr, conf = float(rng.uniform(1.5, 4.0)), float(rng.uniform(0.45, 0.85))
        good = rr > 2.6 and conf < 0.65
        win = rng.random() < (0.72 if good else 0.33)
        entered = start + dt.timedelta(hours=2.5 * i)
        rows.append({"source": "replay", "id": i, "symbol": f"S{i % 40:02d}", "strategy": "vwap_reclaim",
                     "timeframe": "INTRADAY", "side": "LONG" if i % 2 else "SHORT",
                     "entered_at": entered.isoformat(), "exited_at": (entered + dt.timedelta(hours=1)).isoformat(),
                     "r": float(rr if win else -1.0), "confidence": conf, "probability": 0.5, "reward_risk": rr,
                     "noise": ["conflict"] if i % 7 == 0 else [], "time_of_day": "OPEN" if i % 3 else "MIDDAY"})
    return rows


def test_rows_that_overlap_on_one_stock_share_their_weight_and_old_rows_count_for_less():
    entered = np.array([0.0, 10.0, 10.0, 5000.0])
    exited = np.array([100.0, 110.0, 110.0, 5100.0])
    w = meta.sample_weights(entered, exited, ["AAA", "AAA", "BBB", "AAA"], decay=1.0)
    assert abs(w.mean() - 1.0) < 1e-9
    assert abs(w[0] - w[1]) < 1e-9 and abs(w[2] / w[0] - 2.0) < 1e-9        # the two AAA rows overlap: half each
    assert abs(w[3] - w[2]) < 1e-9                                            # alone on its stock at the time
    aged = meta.sample_weights(entered, exited, ["A", "B", "C", "D"], decay=0.5)
    assert aged[0] < aged[1] <= aged[2] < aged[3] and abs(aged[0] / aged[3] - 0.5) < 1e-9
    assert len(meta.sample_weights(np.zeros(0), np.zeros(0))) == 0


def test_a_model_is_trained_judged_kept_and_scores_plays(tmp_path):
    rows = _rows()
    out = meta.train(rows, tmp_path, folds=4, shuffles=3)
    card = out["card"]
    assert out["saved"] and card["rows"] == len(rows) and card["id"].startswith("gbm_")
    models = card["models"]
    assert models["boosted"]["log_loss"] < models["stated"]["log_loss"] - 0.02     # it found the interaction
    assert models["boosted"]["top_decile"]["win_rate"] > 0.6 > card["shuffled"]["top_decile_win_rate_p95"]
    assert {r["feature"] for r in card["importance"][:3]} >= {"reward_risk", "confidence"}
    assert (tmp_path / meta.CARD).exists() and (tmp_path / f"{card['id']}.joblib").exists()

    scorer = meta.Scorer(tmp_path)
    good = scorer.score({"reward_risk": 3.5, "confidence": 0.5, "strategy": "vwap_reclaim", "timeframe": "INTRADAY",
                         "side": "LONG", "noise": [], "time_of_day": "OPEN", "probability": 0.5})
    poor = scorer.score({"reward_risk": 1.8, "confidence": 0.8, "strategy": "vwap_reclaim", "timeframe": "INTRADAY",
                         "side": "LONG", "noise": [], "time_of_day": "OPEN", "probability": 0.5})
    assert good["p"] > 0.6 > 0.45 > poor["p"] and good["id"] == card["id"] and good["usable"] == card["usable"]
    never_seen = scorer.score({"strategy": "brand_new_setup", "noise": ["a_new_flag"]})      # gaps are fine
    assert 0.0 < never_seen["p"] < 1.0
    assert meta.Scorer(tmp_path / "empty").score({"reward_risk": 3.0}) is None              # no model: no score


def test_a_card_naming_anything_but_a_model_the_app_trained_is_never_opened(tmp_path, monkeypatch, caplog):
    import joblib

    opened = []
    bundle = {"model": None, "calibrator": None, "columns": [],
              "card": {"id": "gbm_20260105143000", "schema": meta.FEATURE_SCHEMA, "usable": False}}
    monkeypatch.setattr(joblib, "load", lambda path: opened.append(path) or bundle)
    with caplog.at_level(logging.WARNING, logger=meta.log.name):
        for named in ["../elsewhere/gbm_20260105143000", str(tmp_path.parent / "gbm_20260105143000"),
                      r"\\server\share\gbm_20260105143000", "gbm_2026", "gbm_20260105143000.joblib", 7]:
            (tmp_path / meta.CARD).write_text(json.dumps({"id": named}), encoding="utf-8")
            assert meta.Scorer(tmp_path).card is None
    assert opened == [] and "not a model this app trained" in caplog.text

    (tmp_path / meta.CARD).write_text(json.dumps({"id": "gbm_20260105143000"}), encoding="utf-8")
    assert meta.Scorer(tmp_path).card["id"] == "gbm_20260105143000"           # the names train() writes load
    assert opened == [tmp_path / "gbm_20260105143000.joblib"]


def test_too_few_rows_train_nothing(tmp_path):
    out = meta.train(_rows(n=120), tmp_path)
    assert not out["saved"] and "needs" in out["why"] and not (tmp_path / meta.CARD).exists()


def test_the_columns_a_model_was_trained_on_are_rebuilt_for_a_single_play():
    rows = _rows(n=60)
    design = validate.prepare(rows)
    again = meta.encode(rows[:5], design.columns)
    order = np.argsort([r["entered_at"] for r in rows])[:5]                   # prepare() sorts oldest first
    assert np.allclose(np.nan_to_num(again), np.nan_to_num(design.X[[list(order).index(i) for i in range(5)]]))


def test_the_bet_grows_with_the_odds():
    assert meta.bet_size(0.5) == 0.0 and 0.2 < meta.bet_size(0.65) < 0.3 < meta.bet_size(0.75) < 0.5
    assert meta.risk_factor(0.52) == meta.SIZE_FLOOR and meta.risk_factor(0.9) == 1.0
    assert meta.risk_factor(0.65) < meta.risk_factor(0.75)


def test_autopilot_listens_to_the_model_only_when_asked_and_only_while_it_is_usable():
    from test_autopilot import SILENT, FakeEngine, _cfg, _run, mkplay
    from autotradebot.execution.autopilot import AutoPilot

    eng = FakeEngine()
    ap = AutoPilot(eng, _cfg(model_mode="shadow", model_min_p=0.55), bus=SILENT)
    doubted = mkplay(sym="LOW")
    doubted.evidence["model"] = {"p": 0.41, "id": "gbm_x", "usable": True}
    assert ap.model_refusal(doubted) is None                                   # shadow: logged, never acted on
    ap.configure(model_mode="gate")
    assert "learned model" in ap.model_refusal(doubted) and ap.status()["model_mode"] == "gate"
    doubted.evidence["model"]["usable"] = False
    assert ap.model_refusal(doubted) is None                                   # an unproven model has no say
    doubted.evidence["model"]["usable"] = True
    liked = mkplay(sym="HIGH")
    liked.evidence["model"] = {"p": 0.66, "id": "gbm_x", "usable": True}
    _run(ap, doubted, liked)
    assert eng.approved_ids() == [liked.id]
    ap.configure(model_mode="nonsense")
    assert ap.model_mode == "shadow"


def test_in_size_mode_the_risk_follows_the_odds(monkeypatch):
    from autotradebot.engine.engine import TradingEngine

    fake = SimpleNamespace(strategy_risk_pct=lambda key: 0.8, autopilot=SimpleNamespace(model_mode="size"),
                           settings=SimpleNamespace(config=SimpleNamespace(risk=SimpleNamespace(max_risk_per_trade_pct=1.0))))
    play = SimpleNamespace(strategy="vwap_reclaim", evidence={"model": {"p": 0.65, "usable": True}})
    assert TradingEngine._play_risk_pct(fake, play) == round(0.8 * meta.risk_factor(0.65), 4)
    play.evidence["model"]["usable"] = False
    assert TradingEngine._play_risk_pct(fake, play) == 0.8
    fake.autopilot.model_mode = "gate"
    play.evidence["model"]["usable"] = True
    assert TradingEngine._play_risk_pct(fake, play) == 0.8
