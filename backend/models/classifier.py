from __future__ import annotations

import pickle
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from backend.config import DATA_DIR
from backend.models.classifier_feedback import feedback_adjusted_score
from backend.storage.ground_truth import GroundTruthStore


@dataclass
class Classifier:
    clf: object | None
    profile_emb: np.ndarray | None
    cv_f1: float | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> "Classifier":
        classifier_path = path or DATA_DIR / "classifier.pkl"
        if classifier_path.exists():
            with classifier_path.open("rb") as handle:
                payload = pickle.load(handle)
            return cls(payload["clf"], payload["profile_emb"], payload.get("cv_f1"))
        return cls(None, None, None)

    def score(self, job_description: str, encoder) -> float:
        return self.score_details(job_description, encoder)["score"]

    def score_details(self, job_description: str, encoder) -> dict[str, float | str]:
        if self.clf is not None and self.profile_emb is not None:
            job_emb = encoder.encode(job_description)
            feat = np.concatenate(
                [job_emb, self.profile_emb, job_emb * self.profile_emb, np.abs(job_emb - self.profile_emb)]
            ).reshape(1, -1)
            base_score = float(self.clf.predict_proba(feat)[0, 1])
            mode = "model/full"
        else:
            base_score = self._heuristic_score(job_description)
            mode = "heuristic/fallback"
        adjusted_score = feedback_adjusted_score(base_score, job_description, encoder)
        confidence = abs(adjusted_score - 0.5) * 2.0
        return {"score": adjusted_score, "base_score": base_score, "mode": mode, "confidence": confidence}

    def score_text(self, job_description: str) -> float:
        return self.score(job_description, encoder=self._default_encoder())

    def _heuristic_score(self, job_description: str) -> float:
        text = job_description.lower()
        gt = GroundTruthStore().read_if_exists()
        # Be defensive — ground truth may be missing, partial, or have user-edited
        # fields of the wrong type. Coerce everything into safe lists/dicts.
        preferences = gt.get("preferences") or {}
        if not isinstance(preferences, dict):
            preferences = {}
        desired_roles = preferences.get("desired_roles") or []
        if not isinstance(desired_roles, list):
            desired_roles = []
        desired = " ".join(str(item) for item in desired_roles)
        skills = gt.get("skills") or {}
        if not isinstance(skills, dict):
            skills = {}
        def _lst(key: str) -> list[str]:
            value = skills.get(key) or []
            if not isinstance(value, list):
                return []
            return [str(item) for item in value]
        vocab = " ".join(
            desired.split()
            + _lst("languages")
            + _lst("frameworks")
            + _lst("tools")
            + _lst("ml")
        ).lower()
        vocab_terms = {term for term in vocab.replace("/", " ").replace("-", " ").split() if len(term) > 2}
        hits = sum(1 for term in vocab_terms if term in text)
        early = any(term in text for term in ["new grad", "new graduate", "fresher", "entry level", "intern"])
        senior = any(term in text for term in ["5+ years", "7+ years", "staff engineer", "principal engineer"])
        score = min(0.95, 0.25 + hits * 0.045 + (0.2 if early else 0.0) - (0.35 if senior else 0.0))
        return max(0.05, score)

    @staticmethod
    def _default_encoder():
        from backend.models.encoder import Encoder

        return Encoder()
