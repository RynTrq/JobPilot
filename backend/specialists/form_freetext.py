from __future__ import annotations

import time
from typing import Any

from backend.contracts import FormFreeTextAnswer, SpecialistName
from backend.specialists.base import SpecialistContext, SpecialistResult
from backend.specialists.translator import Translator


class FormFreeTextSpecialist:
    name = SpecialistName.FORM_FREETEXT

    def __init__(self, generator: Any):
        self.generator = generator

    async def answer(
        self,
        *,
        question: str,
        source_blocks: dict[str, Any],
        min_confidence: float,
        context: SpecialistContext | None = None,
    ) -> FormFreeTextAnswer:
        result = await self.run(
            {"question": question, "source_blocks": source_blocks, "min_confidence": min_confidence},
            context or SpecialistContext(correlation_id="local"),
        )
        return FormFreeTextAnswer.model_validate(result.payload)

    async def run(self, payload: dict[str, Any], context: SpecialistContext) -> SpecialistResult:
        started = time.perf_counter()
        question = str(payload.get("question") or "")
        source_blocks = payload.get("source_blocks") if isinstance(payload.get("source_blocks"), dict) else {}
        min_confidence = float(payload.get("min_confidence") or 0.9)
        result = await self.generator.grounded_answer(
            question=question,
            source_blocks=source_blocks,
            min_confidence=min_confidence,
            max_tokens=220,
            specialist=SpecialistName.FORM_FREETEXT,
        )
        body = str(result.get("answer_text") or "").strip()
        if result.get("unknown_flag") or body.upper() in {"NULL", "NULL.", "N/A", "NONE", "UNKNOWN", "NOT APPLICABLE"}:
            body = ""
        fallback_reason = result.get("fallback_reason", "")
        source_language = str(payload.get("source_language") or "en").lower()
        if body and source_language not in ("en", "und"):
            translator = Translator()
            round_trip_bleu = translator.back_translate_bleu(body, source_language)
            if round_trip_bleu < 0.6:
                body = ""
                fallback_reason = f"translation_round_trip_bleu_below_floor:{round_trip_bleu:.2f}"
            else:
                body = translator.translate(body, "en", source_language)
        answer = FormFreeTextAnswer(
            length_words=len(body.split()) if body else 0,
            body=body,
            citations=[str(item) for item in result.get("source_keys_used", [])],
        )
        return SpecialistResult(
            specialist=self.name,
            payload=answer.model_dump(mode="json"),
            latency_ms=int((time.perf_counter() - started) * 1000),
            schema_valid=True,
            provenance={
                "prompt_id": "form_freetext@2.0.0",
                "correlation_id": context.correlation_id,
                "job_url": context.job_url,
                "confidence": result.get("confidence_0_to_1", 0.0),
                "fallback_reason": fallback_reason,
                "source_language": source_language,
            },
        )
