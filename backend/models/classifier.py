from __future__ import annotations

import pickle
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from backend.config import DATA_DIR, GROUND_TRUTH_DIR
from backend.models.classifier_feedback import feedback_adjusted_score
from backend.storage.ground_truth import GroundTruthStore


@dataclass
class Classifier:
    clf: object | None
    profile_emb: np.ndarray | None
    cv_f1: float | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> "Classifier":
        classifier_path = path or _classifier_model_path()
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
        import re as _re
        text = job_description.lower()
        gt = GroundTruthStore().read_if_exists()
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
        # Explicit fresher / zero-experience markers → strong positive signal
        early_terms = [
            "new grad", "new graduate", "fresher", "entry level", "entry-level",
            "junior", "0+ years", "0 years", "no experience required",
            "recent graduate", "early career", "early-career", "internship",
            "trainee", "associate engineer", "graduate engineer", "campus hire",
            "campus recruit", "0-1 year", "0-2 year", "0 to 1 year", "0 to 2 year",
        ]
        early = any(term in text for term in early_terms)
        # Zero-to-N year ranges count as fresher-friendly
        zero_range = bool(_re.search(r"\b0\s*[-–]\s*[1-3]\s*years?\b", text))
        # Senior / over-experienced markers → strong negative signal
        senior_terms = [
            "5+ years", "6+ years", "7+ years", "8+ years", "10+ years",
            "staff engineer", "principal engineer", "senior staff",
            "director of", "vp of", "head of", "founding engineer",
        ]
        senior = any(term in text for term in senior_terms)
        # Explicit mid-level exclusions (2+ or 3+ years required)
        mid_required = bool(_re.search(r"\b[23]\+?\s*years?\s*(of\s+)?(professional\s+)?experience\b", text))
        score = min(0.95,
            0.25
            + hits * 0.045
            + (0.25 if early else 0.0)
            + (0.15 if zero_range else 0.0)
            - (0.35 if senior else 0.0)
            - (0.15 if mid_required else 0.0)
        )
        return max(0.05, score)

    @staticmethod
    def _default_encoder():
        from backend.models.encoder import Encoder

        return Encoder()


def _classifier_model_path() -> Path:
    canonical = GROUND_TRUTH_DIR / "classifier.pkl"
    legacy = DATA_DIR / "classifier.pkl"
    if canonical.exists() or not legacy.exists():
        return canonical
    return legacy
