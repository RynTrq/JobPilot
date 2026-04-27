from __future__ import annotations

import time
from typing import Any

from jinja2 import Template
from pydantic import ValidationError

from backend.config import ROOT_DIR
from backend.contracts import JdMeta, SpecialistName
from backend.specialists.base import SpecialistContext, SpecialistResult


PROMPT_PATH = ROOT_DIR / "backend" / "models" / "prompts" / "extract_job_meta.txt"
REQUIRED_KEYS = {"company", "role_title", "top_requirements", "why_company_fact", "jd_domain_tags", "keywords_exact"}


class JDExtractor:
    name = SpecialistName.JD_EXTRACTOR

    def __init__(self, generator: Any):
        self.generator = generator

    async def extract(self, job_description: str, context: SpecialistContext | None = None) -> JdMeta:
        result = await self.run({"job_description": job_description}, context or SpecialistContext(correlation_id="local"))
        return JdMeta.model_validate(result.payload)

    async def run(self, payload: dict[str, Any], context: SpecialistContext) -> SpecialistResult:
        started = time.perf_counter()
        job_description = str(payload.get("job_description") or "")
        prompt = Template(PROMPT_PATH.read_text(encoding="utf-8")).render(job_description=job_description)
        parsed = await self.generator.complete_json(
            "Extract job metadata as strict JSON.",
            prompt,
            default_key="job_meta",
            required_keys=REQUIRED_KEYS,
            max_tokens=380,
            temperature=0.0,
            specialist=SpecialistName.JD_EXTRACTOR,
        )
        schema_valid = True
        try:
            meta = JdMeta.model_validate(parsed)
        except ValidationError:
            schema_valid = False
            meta = JdMeta()
        latency_ms = int((time.perf_counter() - started) * 1000)
        return SpecialistResult(
            specialist=self.name,
            payload=meta.model_dump(mode="json"),
            latency_ms=latency_ms,
            schema_valid=schema_valid,
            provenance={
                "prompt_id": "extract_job_meta@2.0.0",
                "correlation_id": context.correlation_id,
                "job_url": context.job_url,
            },
        )
