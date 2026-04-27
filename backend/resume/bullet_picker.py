from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np


@dataclass
class ProjectSelection:
    projects_top3: list[dict[str, Any]]
    projects_other3: list[dict[str, Any]]


class BulletPicker:
    def __init__(self, encoder):
        self.encoder = encoder
        self._emb_cache: dict[str, np.ndarray] = {}

    def pick(self, bullet_library: dict[str, list[dict[str, Any]]], job_description: str, limit_per_item: int = 4) -> dict[str, list[dict]]:
        job_emb = self.encoder.encode(job_description)
        selected: dict[str, list[dict]] = {}
        for item_id, bullets in bullet_library.items():
            if not bullets:
                continue
            texts = [" ".join(bullet.get("tags", [])) + " " + bullet.get("text", "") for bullet in bullets]
            embs = self.encoder.encode_batch(texts)
            sims = embs @ job_emb
            order = np.argsort(sims)[::-1][:limit_per_item]
            selected[item_id] = [bullets[int(i)] for i in order]
        return selected

    def select_projects(self, projects_library: dict[str, Any], job_description: str) -> ProjectSelection:
        jd_emb = self.encoder.encode(job_description)
        scored: list[tuple[float, datetime, dict[str, Any]]] = []
        for project in projects_library.get("projects", []):
            text = " ".join(
                [
                    project.get("one_line_summary", ""),
                    " ".join(project.get("tech_stack", [])),
                    " ".join(project.get("domain_tags", [])),
                ]
            )
            proj_emb = self._embed_cached(f"project:{project.get('id')}:{text}", text)
            scored.append((float(proj_emb @ jd_emb), _project_recency(project), project))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        top = [self._render_project(project, jd_emb, main=True) for _, _, project in scored[:3]]
        other = [self._render_project(project, jd_emb, main=False) for _, _, project in scored[3:6]]
        return ProjectSelection(projects_top3=top, projects_other3=other)

    def _render_project(self, project: dict[str, Any], jd_emb: np.ndarray, *, main: bool) -> dict[str, Any]:
        tech_stack = self._ordered_tech_stack(project.get("tech_stack", []), jd_emb, limit=6 if main else 3)
        rendered = {
            "id": project.get("id"),
            "name": project.get("name", ""),
            "github_url": project.get("github_url", ""),
            "live_url": project.get("live_url"),
            "date_range": _date_range(project),
            "tech_stack": tech_stack,
            "tech_stack_short": tech_stack[:3],
            "one_line_summary": project.get("one_line_summary", ""),
        }
        if main:
            rendered["bullets"] = self._select_project_bullets(project, jd_emb, limit=3)
        return rendered

    def _select_project_bullets(self, project: dict[str, Any], jd_emb: np.ndarray, limit: int) -> list[str]:
        variants = project.get("bullet_variants", [])
        if not variants:
            return [project.get("one_line_summary", "")][:limit]
        texts = [variant.get("text", "") for variant in variants]
        embs = np.vstack([self._embed_cached(f"bullet:{project.get('id')}:{variant.get('id')}", text) for variant, text in zip(variants, texts)])
        sims = embs @ jd_emb
        order = np.argsort(sims)[::-1][:limit]
        return [texts[int(i)] for i in order if texts[int(i)]]

    def _ordered_tech_stack(self, tech_stack: list[str], jd_emb: np.ndarray, limit: int) -> list[str]:
        if not tech_stack:
            return []
        embs = np.vstack([self._embed_cached(f"tech:{tech}", tech) for tech in tech_stack])
        sims = embs @ jd_emb
        order = np.argsort(sims)[::-1]
        return [tech_stack[int(i)] for i in order[:limit]]

    def _embed_cached(self, key: str, text: str) -> np.ndarray:
        if key not in self._emb_cache:
            self._emb_cache[key] = self.encoder.encode(text)
        return self._emb_cache[key]


def _project_recency(project: dict[str, Any]) -> datetime:
    raw = project.get("end_month_year") or project.get("start_month_year") or ""
    if str(raw).lower() == "present":
        return datetime.max
    for fmt in ("%b %Y", "%B %Y", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(str(raw), fmt)
        except ValueError:
            continue
    return datetime.min


def _date_range(project: dict[str, Any]) -> str:
    start = project.get("start_month_year") or ""
    end = project.get("end_month_year") or ""
    if start and end and start != end:
        return f"{start} -- {end}"
    return start or end
