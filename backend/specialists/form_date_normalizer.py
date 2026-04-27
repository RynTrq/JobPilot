from __future__ import annotations

import re
import time
from datetime import date, datetime
from typing import Any

from backend.contracts import SpecialistName
from backend.specialists.base import SpecialistContext, SpecialistResult


LOCALE_FORMATS = {
    "jp": "%Y/%m/%d",
    "ja-jp": "%Y/%m/%d",
    "eu": "%d/%m/%Y",
    "uk": "%d/%m/%Y",
    "au": "%d/%m/%Y",
    "australia": "%d/%m/%Y",
    "us": "%m/%d/%Y",
    "na": "%m/%d/%Y",
    "iso": "%Y-%m-%d",
}


class FormDateNormalizer:
    name = SpecialistName.FORM_DATE_NORMALIZER

    async def normalize(
        self,
        value: str,
        context: SpecialistContext | None = None,
        *,
        locale: str = "iso",
    ) -> str:
        result = await self.run(
            {"value": value, "locale": locale},
            context or SpecialistContext(correlation_id="local"),
        )
        return str(result.payload.get("normalized") or "")

    async def run(self, payload: dict[str, Any], context: SpecialistContext) -> SpecialistResult:
        started = time.perf_counter()
        value = str(payload.get("value") or "")
        locale = str(payload.get("locale") or "iso")
        normalized = normalize_form_date(value, locale=locale)
        return SpecialistResult(
            specialist=self.name,
            payload={"input": value, "locale": locale, "normalized": normalized, "known": bool(normalized)},
            latency_ms=int((time.perf_counter() - started) * 1000),
            schema_valid=True,
            provenance={"correlation_id": context.correlation_id, "job_url": context.job_url},
        )


def normalize_form_date(value: str | date | datetime | None, *, locale: str = "iso") -> str:
    parsed = _coerce_date(value)
    if parsed is None:
        return ""
    fmt = LOCALE_FORMATS.get(locale.strip().lower(), LOCALE_FORMATS["iso"])
    return parsed.strftime(fmt)


def _coerce_date(value: str | date | datetime | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    cleaned = " ".join(str(value).strip().split())
    if not cleaned:
        return None
    if cleaned.lower() in {"present", "current", "now"}:
        return None
    cleaned = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", cleaned, flags=re.IGNORECASE)
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    for parser in (_from_iso_datetime, _from_known_formats):
        parsed = parser(cleaned)
        if parsed is not None:
            return parsed
    return None


def _from_iso_datetime(value: str) -> date | None:
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None


def _from_known_formats(value: str) -> date | None:
    formats = (
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%Y/%m/%d",
        "%d-%m-%Y",
        "%m-%d-%Y",
        "%d %b %Y",
        "%d %B %Y",
        "%b %d %Y",
        "%B %d %Y",
        "%Y-%m",
        "%b %Y",
        "%B %Y",
        "%Y",
    )
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None
