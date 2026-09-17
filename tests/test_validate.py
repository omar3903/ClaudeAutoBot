"""The validation harness: purged walk-forward folds, the baselines, the shuffled-label baseline and
the metric table - on rows whose outcome a hidden feature drives, so a model that learns it must
beat the stated confidence, and one fitted on random labels must not."""
from __future__ import annotations

import datetime as dt

import numpy as np

from tos_bot.research import validate
from tos_bot.research.dataset import COLUMNS, load_csv, write_csv
from tos_bot.research.features import FEATURE_SCHEMA

START = dt.datetime(2026, 1, 5, 15, 0, tzinfo=dt.timezone.utc)


def _rows(n: int = 1500, seed: int = 3):
    """Plays over 150 sessions: the odds of paying rise with relative volume and fall with the
    number of noise flags; the stated confidence knows nothing of it."""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        day = i * 150 // n
        rvol = float(rng.lognormal(0.3, 0.5))
        n_noise = int(rng.integers(0, 3))
        p = 1 / (1 + np.exp(-(1.6 * (rvol - 1.4) - 1.2 * n_noise + 0.2)))
        win = rng.random() < p
        entered = START + dt.timedelta(days=day, minutes=int(rng.integers(0, 300)))
        rows.append({"source": "replay" if i % 5 else "shadow", "id": f"row{i}", "symbol": f"S{i % 40}",
                     "strategy": ["vwap_reclaim", "bollinger_fade"][i % 2], "timeframe": "INTRADAY", "side": "LONG",
                     "entered_at": entered.isoformat(), "exited_at": (entered + dt.timedelta(minutes=90)).isoformat(),
                     "exit_reason": "target" if win else "stop", "held_out": day > 100,
                     "r": float(1.8 if win else -1.0), "win": bool(win), "mfe_r": 1.8 if win else 0.2,
                     "confidence": 0.58, "probability": 0.55, "reward_risk": 1.8, "score": 0.6,
                     "expected_r": 0.1, "stop_pct": 1.0, "target_pct": 1.8, "n_targets": 1,
                     "noise": ["against_trend", "conflict"][:n_noise], "n_noise": n_noise, "confirmations": 2,
                     "tags": [], "has_catalyst": False, "minutes_since_open": float(rng.integers(5, 300)),
                     "time_of_day": ["OPEN", "MIDDAY"][i % 2], "weekday": day % 5, "rvol": rvol, "regime": "calm",
                     "p_turbulent": 0.1, "gap_pct": None, "atr_pct": 2.5, "schema": FEATURE_SCHEMA})
    return rows


def test_the_folds_train_on_the_past_only_with_a_purge_gap():
    design = validate.prepare(_rows())
    folds = validate.walk_forward(design, folds=4, embargo_days=1.0)
    assert len(folds) == 4
    for k, f in enumerate(folds):
        assert design.exited[f.train].max() <= f.start - 86400                     # finished before the window
        assert design.entered[f.test].min() >= f.start and design.entered[f.test].max() < f.end
        if k:
            assert f.start > folds[k - 1].start and len(f.train) > len(folds[k - 1].train)   # walking forward
    assert folds[-1].end == float("inf") and folds[0].as_dict()["train"] >= validate.MIN_TRAIN
    assert validate.walk_forward(design.subset(np.arange(50)), folds=3) == []      # too few rows: no judgement


def test_a_model_that_learns_the_driver_beats_the_stated_odds_and_random_labels():
    report = validate.run(_rows(), folds=4, shuffles=8)
    stated, logistic = report["models"]["stated"], report["models"]["logistic"]
    assert report["rows"] == 1500 and report["by_source"] == {"replay": 1200, "shadow": 300}
    assert logistic["log_loss"] < stated["log_loss"] and logistic["brier"] < stated["brier"]
    assert logistic["top_decile"]["win_rate"] > logistic["base_rate"] + 0.1
    assert logistic["top_decile"]["expectancy_r"] > stated["top_decile"]["expectancy_r"]
    assert report["shuffled"]["shuffles"] == 8
    assert logistic["top_decile"]["win_rate"] > report["shuffled"]["top_decile_win_rate_p95"]
    assert report["verdict"]["checks"]["beats_shuffled_labels"] and report["verdict"]["checks"]["log_loss_beats_stated"]
    names = [c for c, _ in logistic["coefficients"]]
    assert "rvol" in names[:3] and "n_noise" in names[:4]                          # it found the drivers
    assert stated["calibration_gap"] is not None and len(logistic["folds"]) == 4
    text = validate.table(report)
    assert "logistic" in text and "shuffled labels x8" in text and "checks:" in text


def test_the_stated_odds_are_judged_as_they_are_and_prepare_reads_strings_too():
    rows = _rows(400)
    design = validate.prepare(rows, sources=["replay"])
    assert len(design) == 320 and set(design.source) == {"replay"}
    assert np.all(design.stated == 0.55) and np.all(design.confidence == 0.58)
    assert "noise:against_trend" in design.columns and "strategy=bollinger_fade" in design.columns
    m = validate.metrics(design.stated, design.y, design.r)
    assert m["rows"] == 320 and abs(m["top_decile"]["win_rate"] - m["base_rate"]) < 0.3
    # the CSV the export writes reads back into the same design
    path = write_csv(rows, __import__("pathlib").Path(__import__("tempfile").mkdtemp()) / "set.csv")
    back = load_csv(path)
    assert len(back) == 400 and list(back[0]) == list(COLUMNS) and back[0]["noise"] == rows[0]["noise"]
    again = validate.prepare(back, sources=["replay"])
    assert again.columns == design.columns and np.allclose(np.nan_to_num(again.X), np.nan_to_num(design.X))
    assert np.array_equal(again.y, design.y)
