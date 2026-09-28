"""How headlines read - positive, negative or neutral - scored by FinBERT, a language
model trained on financial news, running on this computer (nothing is sent anywhere).

FinBERT is an optional install (the transformers and torch packages). Without it
headlines carry no sentiment and nothing else changes.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from importlib.util import find_spec
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

MODEL = "ProsusAI/finbert"


def _load_pipeline(model: str) -> Callable[..., Any]:
    from transformers import pipeline  # optional dependency, imported only when used
    return pipeline("text-classification", model=model, top_k=None)


class HeadlineSentiment:
    def __init__(self, model: str = MODEL, batch_size: int = 16,
                 loader: Optional[Callable[[str], Callable[..., Any]]] = None) -> None:
        self.model = model
        self.batch_size = batch_size
        self._loader = loader or _load_pipeline
        self._pipe: Optional[Callable[..., Any]] = None
        self._lock = threading.Lock()
        self.error = ""

    def installed(self) -> bool:
        return self._loader is not _load_pipeline or (find_spec("transformers") is not None
                                                      and find_spec("torch") is not None)

    def status(self) -> Dict[str, Any]:
        return {"installed": self.installed(), "loaded": self._pipe is not None, "model": self.model,
                "error": self.error}

    def score(self, headlines: Sequence[str]) -> List[Tuple[float, float]]:
        """(sentiment from -1 to 1, confidence) per headline: the positive minus the
        negative probability, and the most likely label's probability. Empty when
        FinBERT isn't available."""
        pipe = self._pipeline()
        if pipe is None or not headlines:
            return []
        out: List[Tuple[float, float]] = []
        for i in range(0, len(headlines), self.batch_size):
            for labels in pipe(list(headlines[i:i + self.batch_size]), truncation=True):
                probs = {row["label"].lower(): float(row["score"]) for row in labels}
                out.append((round(probs.get("positive", 0.0) - probs.get("negative", 0.0), 4),
                            round(max(probs.values(), default=0.0), 4)))
        return out

    def _pipeline(self) -> Optional[Callable[..., Any]]:
        if self._pipe is not None or self.error or not self.installed():
            return self._pipe
        with self._lock:
            if self._pipe is None and not self.error:
                try:
                    self._pipe = self._loader(self.model)
                    log.info("FinBERT loaded (%s)", self.model)
                except Exception as e:  # noqa: BLE001
                    self.error = f"{type(e).__name__}: {e}"
                    log.warning("FinBERT couldn't be loaded, headlines stay unscored: %s", self.error)
        return self._pipe


def symbol_sentiment(rows: Sequence[Mapping[str, Any]], now: dt.datetime,
                     half_life_hours: float = 24.0) -> Optional[Dict[str, Any]]:
    """One reading of a stock's news from its scored headlines: each headline's
    sentiment weighted by the model's confidence, and by half for every
    ``half_life_hours`` of age. None when no headline is scored."""
    total = weight = 0.0
    counted = 0
    for row in rows:
        if row.get("sentiment") is None:
            continue
        published = dt.datetime.fromisoformat(str(row["published_at"]))
        age_hours = max(0.0, (now - published).total_seconds() / 3600)
        w = float(row.get("sentiment_conf") or 0.5) * 0.5 ** (age_hours / half_life_hours)
        total += w * float(row["sentiment"])
        weight += w
        counted += 1
    if not counted or weight <= 0:
        return None
    return {"score": round(total / weight, 3), "headlines": counted, "weight": round(weight, 3)}
