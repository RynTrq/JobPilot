from __future__ import annotations

import hashlib
import os
from functools import cached_property
from typing import Iterable

import numpy as np


class Encoder:
    """Thin wrapper around BGE-small with a deterministic local fallback for tests."""

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5", device: str | None = None):
        self.model_name = model_name
        self.device = device or os.getenv("JOBPILOT_ENCODER_DEVICE", "cpu")

    @cached_property
    def _model(self):
        if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("JOBPILOT_USE_REAL_ENCODER") != "1":
            return None
        try:
            from sentence_transformers import SentenceTransformer

            return SentenceTransformer(self.model_name, device=self.device)
        except Exception:
            return None

    def encode(self, text: str) -> np.ndarray:
        if self._model is not None:
            vector = self._model.encode([text], normalize_embeddings=True)[0]
            return np.asarray(vector, dtype=np.float32).reshape(384)
        return self._fallback_encode(text)

    def encode_batch(self, texts: Iterable[str]) -> np.ndarray:
        items = list(texts)
        if self._model is not None:
            return np.asarray(self._model.encode(items, normalize_embeddings=True), dtype=np.float32)
        return np.vstack([self._fallback_encode(item) for item in items])

    def _fallback_encode(self, text: str) -> np.ndarray:
        vector = np.zeros(384, dtype=np.float32)
        tokens = [tok.strip(".,;:()[]{}").lower() for tok in text.split()]
        for token in tokens:
            if not token:
                continue
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(digest[:4], "little") % vector.size
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[idx] += sign
        norm = np.linalg.norm(vector)
        if norm == 0:
            return vector
        return vector / norm
