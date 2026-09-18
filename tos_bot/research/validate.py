"""Judging a model of the plays honestly.

A model here answers one question for a play: how likely is it to reach its target before its
stop? This module says whether a model's answer is worth anything, the way López de Prado's
*Advances in Financial Machine Learning* (ch. 7, 11-14) and Aronson's *Evidence-Based Technical
Analysis* insist it be judged:

* **purged walk-forward folds** - every fold trains on rows that finished before the test window
  starts (less an embargo), and is judged on the rows that started inside it. Never a random
  split: rows from one session are not independent, and a random split leaks the future;
* **baselines** - the confidence the setup states and the calibrated probability the app shows,
  judged on the same rows; a model that can't beat them is not used;
* **a shuffled-label baseline** - the same model fitted on random labels, many times, to see how
  good "nothing" looks; the real model must beat the best of those;
* **the metric table** - log loss and Brier score (are the probabilities honest?), calibration
  (does 0.6 mean 60%?), the win rate and expectancy in R of the top decile (if we only took the
  plays the model likes most), and the same above a floor.

Everything is NumPy: a regularised logistic regression is the first model (transparent, hard to
overfit on a few thousand rows), fitted by Newton's method on standardised features. The rows
come from research/dataset.py - the trades taken, the plays not taken and the replay.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import warnings
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np

log = logging.getLogger(__name__)

#: the numeric features a model may use - all known at the decision (research/features.py)
NUMERIC = ("confidence", "probability", "reward_risk", "score", "expected_r", "evidence_weight", "stop_pct",
           "target_pct", "n_targets", "n_noise", "confirmations", "has_catalyst", "minutes_since_open", "weekday",
           "p_turbulent", "rvol", "gap_pct", "atr_pct", "change_pct", "range_atr", "heat", "dollar_volume",
           "move_atr", "extreme", "vol", "recent_vol", "vol_ratio", "hurst", "variance_ratio_z", "half_life_bars",
           "market_z", "market_beta", "market_move_pct", "news_since_move", "sessions_to_earnings", "signal_nudge")
#: the categorical features, one column per value seen
CATEGORICAL = ("strategy", "timeframe", "side", "time_of_day", "regime", "price_character", "vol_model")
#: rows with fewer trades than this in a fold's training or test set are skipped
MIN_TRAIN, MIN_TEST = 200, 30
#: the floor a play must clear to count as "taken" in the above-floor metrics
FLOOR = 0.55
TOP_FRACTION = 0.10


# ---------------------------------------------------------------- the prepared table
@dataclass
class Design:
    """The rows as a model sees them: a feature matrix with NaN where a reading was unknown, the
    labels, and what the baselines need."""

    X: np.ndarray                          # (n, d) floats, NaN = unknown
    columns: List[str]
    y: np.ndarray                          # (n,) bool - did the trade pay?
    r: np.ndarray                          # (n,) float - the outcome in R
    entered: np.ndarray                    # (n,) float - seconds since the epoch, UTC
    exited: np.ndarray                     # (n,) float
    stated: np.ndarray                     # (n,) the probability the app showed (its calibrated odds)
    confidence: np.ndarray                 # (n,) the setup's own confidence
    source: List[str]
    strategy: List[str]
    symbol: List[str] = field(default_factory=list)   # for the sample weights: rows on one stock overlap

    def __len__(self) -> int:
        return int(self.y.shape[0])

    def subset(self, idx: np.ndarray) -> "Design":
        return Design(self.X[idx], self.columns, self.y[idx], self.r[idx], self.entered[idx], self.exited[idx],
                      self.stated[idx], self.confidence[idx], [self.source[i] for i in idx],
                      [self.strategy[i] for i in idx], [self.symbol[i] for i in idx] if self.symbol else [])


def prepare(rows: Sequence[Mapping[str, Any]], sources: Optional[Iterable[str]] = None) -> Design:
    """Rows from research/dataset.py (dicts, or the CSV read back) -> a Design, oldest first. Rows
    with no outcome or no times are left out; ``sources`` keeps only those populations."""
    wanted = set(sources) if sources else None
    kept = []
    for row in rows:
        if wanted and row.get("source") not in wanted:
            continue
        r = _num(row.get("r"))
        entered, exited = _stamp(row.get("entered_at")), _stamp(row.get("exited_at"))
        if r is None or math.isnan(r) or entered is None:
            continue
        kept.append((row, r, entered, exited if exited is not None else entered))
    kept.sort(key=lambda t: t[2])
    flags = sorted({f for row, *_ in kept for f in _flags(row.get("noise"))})
    categories = {c: sorted({str(row.get(c)) for row, *_ in kept if row.get(c) not in (None, "")}) for c in CATEGORICAL}
    columns = list(NUMERIC) + [f"noise:{f}" for f in flags] + [f"{c}={v}" for c in CATEGORICAL for v in categories[c]]
    n = len(kept)
    X = np.full((n, len(columns)), np.nan)
    y, r_out, ent, ext, stated, conf = (np.zeros(n, dtype=bool), np.zeros(n), np.zeros(n), np.zeros(n),
                                        np.zeros(n), np.zeros(n))
    source, strategy, symbol = [], [], []
    for i, (row, r, entered, exited) in enumerate(kept):
        for j, k in enumerate(NUMERIC):
            v = _num(row.get(k))
            X[i, j] = np.nan if v is None else v
        row_flags = set(_flags(row.get("noise")))
        base = len(NUMERIC)
        for j, f in enumerate(flags):
            X[i, base + j] = 1.0 if f in row_flags else 0.0
        base += len(flags)
        for c in CATEGORICAL:
            value = str(row.get(c)) if row.get(c) not in (None, "") else None
            for v in categories[c]:
                X[i, base] = 1.0 if value == v else 0.0
                base += 1
        y[i], r_out[i], ent[i], ext[i] = r > 0, r, entered, exited
        c_ = _num(row.get("confidence"))
        p_ = _num(row.get("probability"))
        conf[i] = 0.5 if c_ is None else c_
        stated[i] = conf[i] if p_ is None else p_
        source.append(str(row.get("source") or ""))
        strategy.append(str(row.get("strategy") or ""))
        symbol.append(str(row.get("symbol") or ""))
    return Design(X, columns, y, r_out, ent, ext, np.clip(stated, 0.01, 0.99), np.clip(conf, 0.01, 0.99),
                  source, strategy, symbol)


# ---------------------------------------------------------------- the folds
@dataclass
class Fold:
    train: np.ndarray                      # row indices
    test: np.ndarray
    start: float                           # the test window, seconds since the epoch
    end: float

    def as_dict(self) -> Dict[str, Any]:
        return {"train": int(len(self.train)), "test": int(len(self.test)),
                "from": _iso(self.start), "to": _iso(self.end)}


def walk_forward(design: Design, folds: int = 5, embargo_days: float = 1.0) -> List[Fold]:
    """Purged walk-forward folds over time: the test windows are consecutive slices of the rows'
    entry times (equal counts), and each fold trains only on rows that had *exited* before the
    window starts, less ``embargo_days``. A fold too small on either side is left out."""
    n = len(design)
    if n < MIN_TRAIN + MIN_TEST:
        return []
    order = np.argsort(design.entered, kind="stable")
    entered = design.entered[order]
    edges = [entered[int(n * k / (folds + 1))] for k in range(1, folds + 1)] + [math.inf]
    embargo = embargo_days * 86400.0
    out: List[Fold] = []
    for k in range(folds):
        start, end = edges[k], edges[k + 1]
        test = np.where((design.entered >= start) & (design.entered < end))[0]
        train = np.where(design.exited <= start - embargo)[0]
        if len(train) >= MIN_TRAIN and len(test) >= MIN_TEST:
            out.append(Fold(train, test, start, end))
    return out


# ---------------------------------------------------------------- the models
class Stated:
    """The probability the app showed for the play - the calibrated odds. The baseline to beat."""
    name = "stated"

    def fit(self, design: Design, idx: np.ndarray) -> "Stated":
        return self

    def predict(self, design: Design, idx: np.ndarray) -> np.ndarray:
        return design.stated[idx]


class Confidence(Stated):
    """The setup's own confidence, as if it were a probability."""
    name = "confidence"

    def predict(self, design: Design, idx: np.ndarray) -> np.ndarray:
        return design.confidence[idx]


class Logistic:
    """L2-regularised logistic regression on the standardised features, fitted by Newton's method.
    Unknown readings are filled with the training rows' medians. Transparent: every feature has a
    sign and a size you can argue with."""
    name = "logistic"

    #: the default shrinkage - on ten thousand rows of weak features a small value overfits; the
    #: harness's --l2 tries others, and a chosen value is a result to report, not a free lunch
    L2 = 100.0

    def __init__(self, l2: float = L2, iterations: int = 30) -> None:
        self.l2, self.iterations = l2, iterations
        self.median: Optional[np.ndarray] = None
        self.mean: Optional[np.ndarray] = None
        self.std: Optional[np.ndarray] = None
        self.w: Optional[np.ndarray] = None
        self.columns: List[str] = []

    def fit(self, design: Design, idx: np.ndarray) -> "Logistic":
        X = design.X[idx]
        self.columns = list(design.columns)
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)         # an all-unknown column has no median
            self.median = np.nanmedian(X, axis=0)
        self.median = np.where(np.isnan(self.median), 0.0, self.median)
        Z = self._fill(X)
        self.mean, self.std = Z.mean(axis=0), Z.std(axis=0)
        self.std = np.where(self.std < 1e-9, 1.0, self.std)
        A = self._design(Z)
        y = design.y[idx].astype(float)
        w = np.zeros(A.shape[1])
        reg = np.full(A.shape[1], self.l2)
        reg[0] = 0.0                                          # the intercept isn't shrunk
        for _ in range(self.iterations):
            p = _sigmoid(A @ w)
            grad = A.T @ (p - y) + reg * w
            H = (A * (p * (1 - p))[:, None]).T @ A + np.diag(reg)
            step = np.linalg.solve(H, grad)
            w = w - step
            if np.max(np.abs(step)) < 1e-6:
                break
        self.w = w
        return self

    def predict(self, design: Design, idx: np.ndarray) -> np.ndarray:
        assert self.w is not None, "fit first"
        return _sigmoid(self._design(self._fill(design.X[idx])) @ self.w)

    def coefficients(self, top: int = 12) -> List[Tuple[str, float]]:
        """The largest weights, on the standardised features (comparable to each other)."""
        if self.w is None:
            return []
        pairs = sorted(zip(self.columns, self.w[1:]), key=lambda kv: -abs(kv[1]))
        return [(c, round(float(v), 4)) for c, v in pairs[:top]]

    def _fill(self, X: np.ndarray) -> np.ndarray:
        return np.where(np.isnan(X), self.median, X)

    def _design(self, Z: np.ndarray) -> np.ndarray:
        S = (Z - self.mean) / self.std
        return np.hstack([np.ones((S.shape[0], 1)), S])


MODELS = {"stated": Stated, "confidence": Confidence, "logistic": Logistic}


# ---------------------------------------------------------------- the metrics
def metrics(p: np.ndarray, y: np.ndarray, r: np.ndarray, floor: float = FLOOR) -> Dict[str, Any]:
    """What a set of probabilities is worth on the rows they were made for."""
    n = len(y)
    if n == 0:
        return {"rows": 0}
    pc = np.clip(p, 1e-6, 1 - 1e-6)
    yf = y.astype(float)
    log_loss = float(-np.mean(yf * np.log(pc) + (1 - yf) * np.log(1 - pc)))
    brier = float(np.mean((pc - yf) ** 2))
    k = max(1, int(round(n * TOP_FRACTION)))
    top = np.argsort(-p, kind="stable")[:k]
    above = p >= floor
    bins = []
    for lo in np.arange(0.0, 1.0, 0.2):
        m = (p >= lo) & (p < lo + 0.2) if lo < 0.8 else (p >= lo)
        if m.any():
            bins.append({"from": round(float(lo), 1), "rows": int(m.sum()), "predicted": round(float(p[m].mean()), 3),
                         "actual": round(float(yf[m].mean()), 3)})
    gaps = [abs(b["predicted"] - b["actual"]) for b in bins if b["rows"] >= 20]
    return {
        "rows": int(n), "base_rate": round(float(yf.mean()), 4), "base_expectancy_r": round(float(r.mean()), 4),
        "log_loss": round(log_loss, 4), "brier": round(brier, 4),
        "top_decile": {"rows": int(k), "win_rate": round(float(yf[top].mean()), 4),
                       "expectancy_r": round(float(r[top].mean()), 4)},
        "above_floor": {"floor": floor, "rows": int(above.sum()),
                        "win_rate": round(float(yf[above].mean()), 4) if above.any() else None,
                        "expectancy_r": round(float(r[above].mean()), 4) if above.any() else None},
        "calibration": bins, "calibration_gap": round(max(gaps), 4) if gaps else None,
    }


# ---------------------------------------------------------------- the run
def evaluate(design: Design, folds: Sequence[Fold], model_factory) -> Dict[str, Any]:
    """Fit a fresh model per fold, predict its test rows, and judge the pooled out-of-fold
    predictions (plus each fold's log loss)."""
    pooled_p, pooled_idx, per_fold = [], [], []
    last = None
    for fold in folds:
        model = model_factory().fit(design, fold.train)
        p = model.predict(design, fold.test)
        pooled_p.append(p)
        pooled_idx.append(fold.test)
        m = metrics(p, design.y[fold.test], design.r[fold.test])
        per_fold.append({"from": _iso(fold.start), "to": _iso(fold.end), "rows": m["rows"], "log_loss": m["log_loss"],
                         "top_decile_win_rate": m["top_decile"]["win_rate"]})
        last = model
    if not pooled_p:
        return {"rows": 0, "folds": []}
    p, idx = np.concatenate(pooled_p), np.concatenate(pooled_idx)
    out = metrics(p, design.y[idx], design.r[idx])
    out["folds"] = per_fold
    if isinstance(last, Logistic):
        out["coefficients"] = last.coefficients()
    return out


def shuffled_baseline(design: Design, folds: Sequence[Fold], model_factory, shuffles: int = 20,
                      seed: int = 7) -> Dict[str, Any]:
    """The same model on random labels, ``shuffles`` times: the best it manages by luck is what
    the real model has to beat (Aronson's data-mining bias, made visible)."""
    rng = np.random.default_rng(seed)
    wins, losses = [], []
    for _ in range(shuffles):
        fake = Design(design.X, design.columns, rng.permutation(design.y), design.r, design.entered, design.exited,
                      design.stated, design.confidence, design.source, design.strategy, design.symbol)
        m = evaluate(fake, folds, model_factory)
        if m.get("rows"):
            wins.append(m["top_decile"]["win_rate"])
            losses.append(m["log_loss"])
    if not wins:
        return {"shuffles": 0}
    return {"shuffles": len(wins), "top_decile_win_rate_p95": round(float(np.percentile(wins, 95)), 4),
            "top_decile_win_rate_mean": round(float(np.mean(wins)), 4),
            "log_loss_p05": round(float(np.percentile(losses, 5)), 4)}


def verdicts(report: Dict[str, Any], model: str = "logistic") -> Dict[str, Any]:
    """The pass marks of the learning guide, applied to a model against the baselines."""
    m, stated, shuffled = report["models"].get(model), report["models"].get("stated"), report.get("shuffled") or {}
    if not m or not m.get("rows") or not stated or not stated.get("rows"):
        return {"usable": False, "why": "not enough rows for a walk-forward judgement"}
    checks = {
        "log_loss_beats_stated": m["log_loss"] < stated["log_loss"],
        "calibrated_within_5_points": m["calibration_gap"] is not None and m["calibration_gap"] <= 0.05,
        "top_decile_beats_base_rate": m["top_decile"]["win_rate"] > m["base_rate"] + 0.02,
        "top_decile_pays_more_than_stated": m["top_decile"]["expectancy_r"] > stated["top_decile"]["expectancy_r"],
        "beats_shuffled_labels": (shuffled.get("top_decile_win_rate_p95") is not None
                                  and m["top_decile"]["win_rate"] > shuffled["top_decile_win_rate_p95"]),
    }
    return {"usable": all(checks.values()), "checks": checks}


def run(rows: Sequence[Mapping[str, Any]], *, sources: Optional[Iterable[str]] = None, folds: int = 5,
        embargo_days: float = 1.0, shuffles: int = 20, models: Iterable[str] = ("stated", "confidence", "logistic"),
        seed: int = 7, l2: float = Logistic.L2, extra: Optional[Mapping[str, Any]] = None,
        judged: str = "logistic") -> Dict[str, Any]:
    """The whole judgement on a set of rows: the folds, every model's pooled out-of-fold metrics,
    the shuffled-label baseline for the learned model, and the verdicts. ``extra``: more model
    factories by name (research/model.py brings the boosted trees); ``judged``: the learned model
    the shuffled baseline and the verdict are for."""
    design = prepare(rows, sources)
    splits = walk_forward(design, folds, embargo_days)
    by_source: Dict[str, int] = {}
    for s in design.source:
        by_source[s] = by_source.get(s, 0) + 1
    report: Dict[str, Any] = {
        "made_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "rows": len(design), "by_source": by_source, "features": len(design.columns),
        "base_rate": round(float(design.y.mean()), 4) if len(design) else None,
        "span": [_iso(float(design.entered.min())), _iso(float(design.entered.max()))] if len(design) else None,
        "folds": [f.as_dict() for f in splits], "embargo_days": embargo_days, "l2": l2, "models": {},
    }
    factories = {"stated": Stated, "confidence": Confidence, "logistic": lambda: Logistic(l2=l2), **dict(extra or {})}
    for name in models:
        report["models"][name] = evaluate(design, splits, factories[name])
    if judged in report["models"] and splits:
        report["shuffled"] = shuffled_baseline(design, splits, factories[judged], shuffles, seed)
    report["judged"] = judged
    report["verdict"] = verdicts(report, judged)
    return report


def table(report: Mapping[str, Any]) -> str:
    """The report as text, for the terminal."""
    lines = [f"rows {report['rows']}  by source {report['by_source']}  features {report['features']}  "
             f"base rate {report['base_rate']}  span {report.get('span')}",
             f"folds: " + "; ".join(f"{f['from'][:10]}..{f['to'][:10]} train {f['train']} test {f['test']}"
                                    for f in report["folds"])]
    head = f"{'model':12s} {'log loss':>9s} {'brier':>7s} {'cal gap':>8s} {'top10% win':>11s} {'top10% R':>9s} {'>floor win':>11s} {'>floor R':>9s}"
    lines.append(head)
    for name, m in report["models"].items():
        if not m.get("rows"):
            lines.append(f"{name:12s} (no folds)")
            continue
        af = m["above_floor"]
        lines.append(f"{name:12s} {m['log_loss']:9.4f} {m['brier']:7.4f} {str(m['calibration_gap']):>8s} "
                     f"{m['top_decile']['win_rate']:11.3f} {m['top_decile']['expectancy_r']:9.3f} "
                     f"{str(af['win_rate']):>11s} {str(af['expectancy_r']):>9s}")
    sh = report.get("shuffled") or {}
    if sh.get("shuffles"):
        lines.append(f"shuffled labels x{sh['shuffles']}: top-decile win rate p95 {sh['top_decile_win_rate_p95']} "
                     f"(mean {sh['top_decile_win_rate_mean']}), best log loss {sh['log_loss_p05']}")
    v = report.get("verdict") or {}
    if "checks" in v:
        lines.append("checks: " + ", ".join(f"{k} {'yes' if ok else 'NO'}" for k, ok in v["checks"].items()))
        lines.append("usable: " + ("YES" if v["usable"] else "no - stays in shadow"))
    else:
        lines.append(f"verdict: {v.get('why')}")
    coefs = (report["models"].get("logistic") or {}).get("coefficients")
    if coefs:
        lines.append("largest weights: " + ", ".join(f"{c} {w:+.2f}" for c, w in coefs))
    return "\n".join(lines)


# ---------------------------------------------------------------- helpers
def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -35, 35)))


def _num(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, str):
        if v.strip().lower() in ("true", "false"):
            return 1.0 if v.strip().lower() == "true" else 0.0
        try:
            v = float(v)
        except ValueError:
            return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _flags(v: Any) -> List[str]:
    if not v:
        return []
    if isinstance(v, str):
        return [f for f in v.split("|") if f]
    return [str(f) for f in v]


def _stamp(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        d = dt.datetime.fromisoformat(str(v))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.timestamp()


def _iso(seconds: float) -> str:
    if seconds is None or math.isinf(seconds):
        return "end"
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).isoformat(timespec="minutes")
