from __future__ import annotations

import re
import time
from typing import Any

from bs4 import BeautifulSoup

from backend.contracts import SpecialistName
from backend.scraping.job_page import minimal_safe_clean
from backend.specialists.base import SpecialistContext, SpecialistResult


BOILERPLATE_MARKERS = (
    "accept all cookies",
    "accept cookies",
    "cookie policy",
    "privacy policy",
    "terms of use",
    "sign in",
    "create job alert",
    "share this job",
    "similar jobs",
    "recommended jobs",
    "all rights reserved",
)


class JDCleaner:
    name = SpecialistName.JD_CLEANER

    async def clean(self, raw: str, context: SpecialistContext | None = None, *, is_html: bool = False) -> str:
        result = await self.run(
            {"raw": raw, "is_html": is_html},
            context or SpecialistContext(correlation_id="local"),
        )
        return str(result.payload.get("cleaned_text") or "")

    async def run(self, payload: dict[str, Any], context: SpecialistContext) -> SpecialistResult:
        started = time.perf_counter()
        raw = str(payload.get("raw") or "")
        text = _html_to_text(raw) if payload.get("is_html") else raw
        cleaned, removed = clean_job_description_text(text)
        return SpecialistResult(
            specialist=self.name,
            payload={
                "cleaned_text": cleaned,
                "char_count": len(cleaned),
                "removed_line_count": removed,
            },
            latency_ms=int((time.perf_counter() - started) * 1000),
            schema_valid=True,
            provenance={"correlation_id": context.correlation_id, "job_url": context.job_url},
        )


def clean_job_description_text(text: str) -> tuple[str, int]:
    normalized = minimal_safe_clean(text)
    kept: list[str] = []
    removed = 0
    seen: set[str] = set()
    for line in normalized.splitlines():
        candidate = " ".join(line.split())
        if not candidate:
            if kept and kept[-1]:
                kept.append("")
            continue
        lowered = candidate.lower()
        if _is_boilerplate(lowered):
            removed += 1
            continue
        dedupe_key = re.sub(r"\W+", "", lowered)
        if dedupe_key and dedupe_key in seen and len(candidate) < 120:
            removed += 1
            continue
        if dedupe_key:
            seen.add(dedupe_key)
        kept.append(candidate)
    return minimal_safe_clean("\n".join(kept)), removed


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "meta", "link", "svg", "canvas", "nav", "footer"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def _is_boilerplate(lowered_line: str) -> bool:
    if lowered_line in {"apply", "apply now"}:
        return False
    if len(lowered_line) <= 3:
        return True
    if any(marker in lowered_line for marker in BOILERPLATE_MARKERS):
        return True
    return bool(re.fullmatch(r"(home|jobs|careers|about|contact|login|menu)", lowered_line))
