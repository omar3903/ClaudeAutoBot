"""How headlines read - positive, negative or neutral - scored by FinBERT, a language
model trained on financial news, running on this computer (nothing is sent anywhere).

FinBERT is an optional install (the transformers and torch packages). Without it
headlines carry no sentiment and nothing else changes.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
from importlib.util import find_spec
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

MODEL = "ProsusAI/finbert"
# The exact upload of the model the app runs, so a later change on its Hugging Face page never
# reaches this computer by itself.
REVISION = "4556d13015211d73dccd3fdd39d39232506f3e43"
# The files that upload is made of: with all of them in the local cache FinBERT loads from there
# without asking huggingface.co anything.
FILES = ("config.json", "pytorch_model.bin", "special_tokens_map.json", "tokenizer_config.json", "vocab.txt")


def _cached(model: str, revision: str) -> Optional[str]:
    """The folder holding ``revision`` of ``model`` in the local Hugging Face cache, or None
    while any of its files is missing (the first load downloads them)."""
    from huggingface_hub import try_to_load_from_cache  # installed with transformers
    paths = [try_to_load_from_cache(model, name, revision=revision) for name in FILES]
    return os.path.dirname(paths[0]) if all(isinstance(p, str) for p in paths) else None


def _load_pipeline(model: str) -> Callable[..., Any]:
    # Without this, a load by name has transformers ask Hugging Face's converter in the background
    # for a safetensors copy of the weights (they are a pickle file) - one more call out.
    os.environ.setdefault("DISABLE_SAFETENSORS_CONVERSION", "1")
    from transformers import pipeline  # optional dependency, imported only when used
    revision = REVISION if model == MODEL else None
    # A cached model loads from its folder: loaded by name, huggingface_hub still looks something
    # up on huggingface.co once a day, even with local_files_only.
    folder = _cached(model, revision) if revision else None
    return pipeline("text-classification", model=folder or model, top_k=None, revision=revision,
                    local_files_only=folder is not None)


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
