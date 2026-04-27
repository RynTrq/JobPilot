from __future__ import annotations

import json
import time
from datetime import date, datetime, timezone
from typing import Any

from bs4 import BeautifulSoup

from backend.contracts import SpecialistName
from backend.scraping.adapters.platform_catalog import DEFAULT_INACTIVE_MARKERS
from backend.specialists.base import SpecialistContext, SpecialistResult


ACTIVE_MARKERS = (
    "apply now",
    "quick apply",
    "submit application",
    "upload resume",
    "attach resume",
    "attach your resume",
    "attach cv",
    "attach your cv",
    "apply for this job",
    "apply here",
    "apply to this job",
    "start application",
)

HTTP_INACTIVE_MARKERS = ("404", "410")


class LivenessDetector:
    name = SpecialistName.LIVENESS_DETECTOR

    async def detect(self, text: str, context: SpecialistContext | None = None, *, html: str = "") -> dict[str, Any]:
        result = await self.run(
            {"text": text, "html": html},
            context or SpecialistContext(correlation_id="local"),
        )
        return result.payload

    async def run(self, payload: dict[str, Any], context: SpecialistContext) -> SpecialistResult:
        started = time.perf_counter()
        detection = classify_liveness_text(
            text=str(payload.get("text") or ""),
            html=str(payload.get("html") or ""),
        )
        return SpecialistResult(
            specialist=self.name,
            payload=detection,
            latency_ms=int((time.perf_counter() - started) * 1000),
            schema_valid=True,
            provenance={"correlation_id": context.correlation_id, "job_url": context.job_url},
        )


def classify_liveness_text(
    text: str,
    *,
    html: str = "",
    today: date | None = None,
) -> dict[str, Any]:
    today = today or datetime.now(timezone.utc).date()
    reasons: list[str] = []
    valid_through = _extract_valid_through(html)
    if valid_through is not None:
        if valid_through < today:
            return {"state": "expired", "reasons": [f"validThrough={valid_through.isoformat()}"]}
        reasons.append(f"validThrough={valid_through.isoformat()}")

    normalized = " ".join(f"{text}\n{_html_visible_text(html)}".lower().split())
    expired_reasons = [marker for marker in (*HTTP_INACTIVE_MARKERS, *DEFAULT_INACTIVE_MARKERS) if marker in normalized]
    if expired_reasons:
        return {"state": "expired", "reasons": expired_reasons[:3]}

    active_reasons = [marker for marker in ACTIVE_MARKERS if marker in normalized]
    if active_reasons or reasons:
        return {"state": "active", "reasons": reasons + active_reasons[:3]}
    return {"state": "uncertain", "reasons": ["missing_apply_signal"]}


def _html_visible_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "meta", "link"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def _extract_valid_through(html: str) -> date | None:
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        for item in _json_ld_items(raw):
            value = _find_key(item, "validThrough")
            if isinstance(value, str):
                parsed = _parse_date(value)
                if parsed is not None:
                    return parsed
    return None


def _json_ld_items(raw: str) -> list[Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return parsed
    return [parsed]


def _find_key(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for nested in value.values():
            found = _find_key(nested, key)
            if found is not None:
                return found
    if isinstance(value, list):
        for nested in value:
            found = _find_key(nested, key)
            if found is not None:
                return found
    return None


def _parse_date(value: str) -> date | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    try:
        return datetime.fromisoformat(cleaned).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(cleaned[:10])
    except ValueError:
        return None
