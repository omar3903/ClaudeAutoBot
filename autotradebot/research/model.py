"""The meta-label model: the odds that a play pays, learned from what happened to plays like it.

The setups still choose the side, the entry, the stop and the target. The model only answers "act on
this one, and how big?" - López de Prado's meta-labelling (*Advances in Financial Machine Learning*,
ch. 3). From the same book: rows that overlap in time on one stock share information, so each is
weighted by how unique it is, and older rows count for less (ch. 4); the model is judged walking
forward with purged, embargoed folds (ch. 7, research/validate.py); a feature matters by how much
worse the out-of-sample score gets when it is shuffled (MDA, ch. 8); and the size of a bet follows
from the predicted probability (ch. 10). From Jansen's *Machine Learning for Algorithmic Trading*:
gradient boosting for tabular features with gaps in them (ch. 12), calibrated probabilities, and
the information coefficient of each feature (ch. 4).

A model is *usable* only when it beats the odds the app already states, stays calibrated, pays more
in its top decile, and beats itself on shuffled labels (validate.verdicts). Until then it runs in
shadow: its probability is logged with every play and never acted on.

scikit-learn is needed to train and to score; without it the app runs as before, without a model.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import numpy as np

from . import validate
from .features import FEATURE_SCHEMA
from .validate import CATEGORICAL, NUMERIC, Design

log = logging.getLogger(__name__)

CARD = "current.json"
# The only model names train() writes. Loading a model file can run code, so a card naming anything
# else (a path, another folder) is never followed.
MODEL_ID = re.compile(r"gbm_\d{14}", re.ASCII)
DECAY = 0.5                        # the oldest row counts this much of the newest (AFML ch. 4.10)
MIN_ROWS = 500
TOP_FEATURES = 15
SIZE_FLOOR = 0.25                  # the least of the usual risk a bet the model likes a little still gets


def available() -> bool:
    try:
        import sklearn  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- weights
def sample_weights(entered: np.ndarray, exited: np.ndarray, symbols: Optional[Sequence[str]] = None,
                   decay: float = DECAY) -> np.ndarray:
    """AFML ch. 4: a row's weight is its uniqueness - one over the number of rows alive on the same
    stock while it was (every setup firing on one move is one piece of information, not five) -
    times a linear time decay from ``decay`` for the oldest row to 1 for the newest. Scaled to
    average 1. Without symbols, concurrency is counted across all rows."""
    n = len(entered)
    if n == 0:
        return np.zeros(0)
    weights = np.ones(n)
    groups: Dict[str, List[int]] = {}
    for i in range(n):
        groups.setdefault(symbols[i] if symbols is not None and len(symbols) == n else "", []).append(i)
    for idx in groups.values():
        idx = np.asarray(idx)
        start, end = entered[idx], np.maximum(exited[idx], entered[idx])
        began_before = np.searchsorted(np.sort(start), end, side="right")     # rows that began by my end
        ended_before = np.searchsorted(np.sort(end), start, side="left")      # ...less those over before my start
        weights[idx] = 1.0 / np.maximum(1, began_before - ended_before)
    order = np.argsort(np.argsort(entered))
    age = order / max(1, n - 1)
    weights *= decay + (1.0 - decay) * age
    return weights * n / weights.sum()


# ---------------------------------------------------------------- the model, in the harness's shape
class Boosted:
    """Gradient-boosted trees on the Design's columns; NaN is a value of its own, so an unknown
    reading needs no filling in. Shallow trees and a slow rate: the rows are few and noisy."""

    PARAMS = dict(max_depth=3, learning_rate=0.05, max_iter=200, min_samples_leaf=40, l2_regularization=1.0,
                  early_stopping=False, random_state=7)

    def __init__(self) -> None:
        self.model = None

    def fit(self, design: Design, idx: np.ndarray) -> "Boosted":
        from sklearn.ensemble import HistGradientBoostingClassifier

        symbols = [design.symbol[i] for i in idx] if getattr(design, "symbol", None) else None
        weights = sample_weights(design.entered[idx], design.exited[idx], symbols)
        self.model = HistGradientBoostingClassifier(**self.PARAMS)
        self.model.fit(design.X[idx], design.y[idx].astype(int), sample_weight=weights)
        return self

    def predict(self, design: Design, idx: np.ndarray) -> np.ndarray:
        return self.predict_matrix(design.X[idx])

    def predict_matrix(self, X: np.ndarray) -> np.ndarray:
        return np.clip(self.model.predict_proba(X)[:, 1], 0.01, 0.99)


def encode(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> np.ndarray:
    """Rows (or a live play's features) as the matrix a trained model expects: the columns it was
    trained on, in its order; a category or flag it never saw is simply absent."""
    X = np.full((len(rows), len(columns)), np.nan)
    for i, row in enumerate(rows):
        flags = set(validate._flags(row.get("noise")))
        for j, col in enumerate(columns):
            if col.startswith("noise:"):
                X[i, j] = 1.0 if col[6:] in flags else 0.0
            elif "=" in col:
                key, value = col.split("=", 1)
                X[i, j] = 1.0 if str(row.get(key)) == value else 0.0
            else:
                v = validate._num(row.get(col))
                X[i, j] = np.nan if v is None else v
    return X


# ---------------------------------------------------------------- training
def _out_of_fold(design: Design, folds) -> tuple:
    ps, idxs = [], []
    for fold in folds:
        ps.append(Boosted().fit(design, fold.train).predict(design, fold.test))
        idxs.append(fold.test)
    return (np.concatenate(ps), np.concatenate(idxs)) if ps else (np.zeros(0), np.zeros(0, dtype=int))


def importance(design: Design, folds, repeats: int = 5, seed: int = 7) -> List[Dict[str, Any]]:
    """AFML's MDA on the latest fold: how much worse the out-of-sample log loss gets when a column
    is shuffled. A feature that only helps in sample scores nothing here."""
    if not folds:
        return []
    fold = folds[-1]
    model = Boosted().fit(design, fold.train)
    X, y = design.X[fold.test].copy(), design.y[fold.test].astype(float)
    rng = np.random.default_rng(seed)

    def loss(matrix: np.ndarray) -> float:
        p = model.predict_matrix(matrix)
        return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())

    base, out = loss(X), []
    for j, name in enumerate(design.columns):
        keep, worse = X[:, j].copy(), []
        for _ in range(repeats):
            X[:, j] = rng.permutation(keep)
            worse.append(loss(X) - base)
        X[:, j] = keep
        out.append({"feature": name, "loss_increase": round(float(np.mean(worse)), 5)})
    return sorted(out, key=lambda r: -r["loss_increase"])[:TOP_FEATURES]


def information_coefficients(design: Design) -> List[Dict[str, Any]]:
    """Jansen's first look at a feature: the rank correlation between it and the outcome in R."""
    import pandas as pd

    r = pd.Series(design.r).rank()
    out = []
    for j, name in enumerate(design.columns[:len(NUMERIC)]):
        col = pd.Series(design.X[:, j])
        if col.notna().sum() < 50 or col.nunique() < 5:
            continue
        ic = col.rank().corr(r)
        if ic == ic:
            out.append({"feature": name, "ic": round(float(ic), 4)})
    return sorted(out, key=lambda row: -abs(row["ic"]))[:TOP_FEATURES]


def train(rows: Sequence[Mapping[str, Any]], directory: Path, *, folds: int = 5, embargo_days: float = 1.0,
          shuffles: int = 10, sources: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Judge the model walking forward, fit it on every row, calibrate it on its own out-of-fold
    predictions, and keep it with its card. Returns the card; the model is saved whether or not it
    is usable - an unusable one still runs in shadow, which is how it earns (or fails) trust."""
    import joblib
    from sklearn.isotonic import IsotonicRegression

    design = validate.prepare(rows, sources)
    if len(design) < MIN_ROWS:
        return {"saved": False, "why": f"{len(design)} rows with an outcome - a model needs {MIN_ROWS}"}
    report = validate.run(rows, sources=sources, folds=folds, embargo_days=embargo_days, shuffles=shuffles,
                          models=("stated", "confidence", "logistic", "boosted"),
                          extra={"boosted": Boosted}, judged="boosted")
    splits = validate.walk_forward(design, folds, embargo_days)
    p_oof, idx = _out_of_fold(design, splits)
    calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.02, y_max=0.98)
    calibrator.fit(p_oof, design.y[idx].astype(float))
    final = Boosted().fit(design, np.arange(len(design)))
    stamp = dt.datetime.now(dt.timezone.utc)
    model_id = "gbm_" + stamp.strftime("%Y%m%d%H%M%S")
    card = {
        "id": model_id, "trained_at": stamp.isoformat(timespec="seconds"), "schema": FEATURE_SCHEMA,
        "rows": len(design), "by_source": report.get("by_source"), "span": report.get("span"),
        "features": len(design.columns), "usable": bool(report["verdict"].get("usable")),
        "verdict": report["verdict"],
        "models": {k: {m: v.get(m) for m in ("rows", "log_loss", "brier", "auc", "calibration_gap", "base_rate")}
                   | {"top_decile": v.get("top_decile")} for k, v in report["models"].items()},
        "shuffled": report.get("shuffled"), "importance": importance(design, splits),
        "information_coefficients": information_coefficients(design),
    }
    directory.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": final.model, "calibrator": calibrator, "columns": list(design.columns), "card": card},
                directory / f"{model_id}.joblib")
    (directory / CARD).write_text(json.dumps(card, indent=1), encoding="utf-8")
    return {"saved": True, "card": card, "report": report}


# ---------------------------------------------------------------- scoring live plays
class Scorer:
    """The latest trained model, scoring plays as they are found. It reloads when a new model is
    trained while the app runs. Every failure is a quiet "no score" - a model is never in the way."""

    CHECK_S = 60.0

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._lock = threading.Lock()
        self._bundle: Optional[Dict[str, Any]] = None
        self._loaded_id: Optional[str] = None
        self._checked_at = 0.0

    @property
    def card(self) -> Optional[Dict[str, Any]]:
        self._refresh()
        return dict(self._bundle["card"]) if self._bundle else None

    def _refresh(self) -> None:
        now = time.monotonic()
        if now - self._checked_at < self.CHECK_S and self._checked_at:
            return
        with self._lock:
            self._checked_at = now
            try:
                card = json.loads((self.directory / CARD).read_text(encoding="utf-8"))
                if card.get("id") == self._loaded_id:
                    return
                if not isinstance(card.get("id"), str) or not MODEL_ID.fullmatch(card["id"]):
                    log.warning("the model card names %r, not a model this app trained - not loaded", card.get("id"))
                    self._bundle, self._loaded_id = None, card.get("id")
                    return
                import joblib

                bundle = joblib.load(self.directory / f"{card['id']}.joblib")
                if int(bundle["card"].get("schema", 0)) != FEATURE_SCHEMA:
                    log.warning("the trained model is for feature schema %s, the app makes %s - not used",
                                bundle["card"].get("schema"), FEATURE_SCHEMA)
                    self._bundle, self._loaded_id = None, card.get("id")
                    return
                self._bundle, self._loaded_id = bundle, card["id"]
                log.info("meta-label model %s loaded (%s rows, usable=%s)", card["id"], card.get("rows"),
                         card.get("usable"))
            except FileNotFoundError:
                self._bundle = None
            except Exception:  # noqa: BLE001
                log.warning("could not load the trained model", exc_info=True)
                self._bundle = None

    def score(self, features: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        self._refresh()
        bundle = self._bundle
        if bundle is None:
            return None
        try:
            raw = float(np.clip(bundle["model"].predict_proba(encode([features], bundle["columns"]))[:, 1], 0.01, 0.99)[0])
            p = float(bundle["calibrator"].predict([raw])[0])
        except Exception:  # noqa: BLE001
            log.debug("the model could not score a play", exc_info=True)
            return None
        return {"p": round(p, 3), "id": bundle["card"]["id"], "usable": bool(bundle["card"].get("usable"))}


def bet_size(p: float) -> float:
    """AFML ch. 10: the size of a bet from the probability it pays - the test statistic of
    "p is more than a coin flip", through the normal curve: 2*Phi(z) - 1, z = (p - 1/2) / sqrt(p(1 - p)).
    0 at a coin flip, 1 at certainty."""
    p = min(0.999, max(0.001, float(p)))
    z = (p - 0.5) / math.sqrt(p * (1.0 - p))
    return max(0.0, math.erf(z / math.sqrt(2.0)))


def risk_factor(p: float) -> float:
    """The share of the usual risk a play gets when the model sizes it: twice the bet size, so a
    play at 78% or more gets all of it, floored at a quarter for one the model barely likes."""
    return round(min(1.0, max(SIZE_FLOOR, 2.0 * bet_size(p))), 3)
