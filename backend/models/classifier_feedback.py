from __future__ import annotations

import json
import hashlib
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from backend.config import DATA_DIR, GROUND_TRUTH_DIR


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
        self.path = path or _classifier_feedback_write_path()
        self._explicit_path = path is not None

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
        review_label: str | None = None,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        label = (
            review_label
            if review_label in {"pass", "fail"}
            else "pass" if agent_decision in {"pass", "dry_run_complete", "fit"} else "fail"
        )
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
            "review_label": review_label,
            "regions_matched": regions_matched or [],
            "jd_min_years": jd_min_years,
        }
        line = json.dumps(row, sort_keys=True) + "\n"
        with _APPEND_LOCK:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def read(self) -> list[dict[str, Any]]:
        rows = []
        seen: set[str] = set()
        for path in self._read_paths():
            for row in _read_feedback_path(path):
                key = _feedback_row_key(row)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
        return rows

    def _read_paths(self) -> list[Path]:
        if self._explicit_path:
            return [self.path]
        return _classifier_feedback_read_paths()


def feedback_adjusted_score(
    base_score: float,
    job_description: str,
    encoder,
    *,
    max_examples: int = 200,
    min_reviewed_examples: int = 3,
    min_similar_examples: int = 2,
) -> float:
    rows = ClassifierFeedbackStore().read()[-max_examples:]
    if not rows:
        return base_score
    reviewed_rows = [
        row
        for row in rows
        if row.get("review_label") in {"pass", "fail"} and row.get("description_text")
    ]
    reviewed: list[dict[str, Any]] = []
    seen_reviewed: set[tuple[str, str, str]] = set()
    for row in reviewed_rows:
        key = (
            str(row.get("job_url") or ""),
            str(row.get("review_label") or ""),
            str(row.get("jd_text_hash") or _sha256_text(str(row.get("description_text") or ""))),
        )
        if key in seen_reviewed:
            continue
        seen_reviewed.add(key)
        reviewed.append(row)
    if len(reviewed) < min_reviewed_examples:
        return base_score

    query = encoder.encode(job_description)
    descriptions = [row["description_text"] for row in reviewed]
    embeddings = encoder.encode_batch(descriptions)
    sims = embeddings @ query
    useful = [(float(sim), row) for sim, row in zip(sims, reviewed) if sim >= 0.42]
    if len(useful) < min_similar_examples:
        return base_score

    useful.sort(key=lambda item: item[0], reverse=True)
    top = useful[:12]
    weights = np.asarray([sim for sim, _ in top], dtype=np.float32)
    labels = np.asarray([1.0 if row["review_label"] == "pass" else 0.0 for _, row in top], dtype=np.float32)
    label_score = float((weights @ labels) / max(float(weights.sum()), 1e-6))
    strength = min(0.45, 0.12 + 0.06 * len(top))
    return max(0.01, min(0.99, (1.0 - strength) * base_score + strength * label_score))


def _classifier_feedback_write_path() -> Path:
    return GROUND_TRUTH_DIR / "classifier_feedback.jsonl"


def _classifier_feedback_read_paths() -> list[Path]:
    canonical = _classifier_feedback_write_path()
    legacy = DATA_DIR / "classifier_feedback.jsonl"
    paths: list[Path] = []
    if legacy.exists() and legacy != canonical:
        paths.append(legacy)
    paths.append(canonical)
    return paths


def _read_feedback_path(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
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


def _feedback_row_key(row: dict[str, Any]) -> str:
    digest = row.get("jd_text_hash") or _sha256_text(str(row.get("description_text") or ""))
    return "|".join(
        [
            str(row.get("job_url") or ""),
            str(row.get("label") or ""),
            str(digest),
            str(row.get("created_at") or ""),
        ]
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _agreement(*, agent_decision: str, sut_decision: str) -> bool:
    agent_pass = agent_decision in {"pass", "dry_run_complete", "fit"}
    sut_pass = sut_decision == "pass"
    return agent_pass == sut_pass
