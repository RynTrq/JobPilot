from __future__ import annotations

import json
import hashlib
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from backend.config import DATA_DIR


@dataclass
class ClassifierFeedback:
    job_url: str
    label: str
    score: float
    title: str | None
    company: str | None
    description_text: str
    created_at: str


# Single process-wide lock for JSONL append. Keeping concurrent asyncio tasks from
# interleaving partial writes prevents corruption of classifier_feedback.jsonl.
_APPEND_LOCK = threading.Lock()


class ClassifierFeedbackStore:
    def __init__(self, path: Path | None = None):
        self.path = path or DATA_DIR / "classifier_feedback.jsonl"

    def append(
        self,
        *,
        job_url: str,
        label: str,
        score: float,
        description_text: str,
        title: str | None = None,
        company: str | None = None,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = ClassifierFeedback(
            job_url=job_url,
            label=label,
            score=score,
            title=title,
            company=company,
            description_text=(description_text or "")[:20000],
            created_at=datetime.now().isoformat(),
        )
        line = json.dumps(asdict(row), sort_keys=True) + "\n"
        with _APPEND_LOCK:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def append_agent_signal(
        self,
        *,
        job_url: str,
        jd_text: str,
        candidate_facts: dict[str, Any],
        agent_decision: str,
        agent_reasoning: dict[str, Any],
        sut_score: float,
        sut_decision: str,
        title: str | None = None,
        company: str | None = None,
        regions_matched: list[str] | None = None,
        jd_min_years: int | float | None = None,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        label = "pass" if agent_decision in {"pass", "dry_run_complete", "fit"} else "fail"
        row = {
            "job_url": job_url,
            "label": label,
            "score": sut_score,
            "title": title,
            "company": company,
            "description_text": (jd_text or "")[:20000],
            "created_at": datetime.now().isoformat(),
            "jd_text_hash": _sha256_text(jd_text or ""),
            "candidate_facts_hash": _sha256_text(json.dumps(candidate_facts, sort_keys=True, default=str)),
            "agent_decision": agent_decision,
            "agent_reasoning": agent_reasoning,
            "sut_score": sut_score,
            "sut_decision": sut_decision,
            "agreement": _agreement(agent_decision=agent_decision, sut_decision=sut_decision),
            "regions_matched": regions_matched or [],
            "jd_min_years": jd_min_years,
        }
        line = json.dumps(row, sort_keys=True) + "\n"
        with _APPEND_LOCK:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = []
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows


def feedback_adjusted_score(base_score: float, job_description: str, encoder, *, max_examples: int = 200) -> float:
    rows = ClassifierFeedbackStore().read()[-max_examples:]
    if not rows:
        return base_score
    labeled = [row for row in rows if row.get("label") in {"pass", "fail"} and row.get("description_text")]
    if not labeled:
        return base_score

    query = encoder.encode(job_description)
    descriptions = [row["description_text"] for row in labeled]
    embeddings = encoder.encode_batch(descriptions)
    sims = embeddings @ query
    useful = [(float(sim), row) for sim, row in zip(sims, labeled) if sim >= 0.42]
    if not useful:
        return base_score

    useful.sort(key=lambda item: item[0], reverse=True)
    top = useful[:12]
    weights = np.asarray([sim for sim, _ in top], dtype=np.float32)
    labels = np.asarray([1.0 if row["label"] == "pass" else 0.0 for _, row in top], dtype=np.float32)
    label_score = float((weights @ labels) / max(float(weights.sum()), 1e-6))
    strength = min(0.45, 0.12 + 0.06 * len(top))
    return max(0.01, min(0.99, (1.0 - strength) * base_score + strength * label_score))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _agreement(*, agent_decision: str, sut_decision: str) -> bool:
    agent_pass = agent_decision in {"pass", "dry_run_complete", "fit"}
    sut_pass = sut_decision == "pass"
    return agent_pass == sut_pass
