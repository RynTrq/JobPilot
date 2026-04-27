from __future__ import annotations


class ResumeTailor:
    def __init__(self, generator):
        self.generator = generator

    async def summary(self, job_description: str, profile_brief: str) -> str:
        result = await self.generator.grounded_answer(
            question="Write a concise truthful resume summary.",
            source_blocks={"job_description_text": job_description[:3000], "candidate_profile_brief": profile_brief},
            min_confidence=0.88,
            max_tokens=160,
        )
        return str(result.get("answer_text") or "UNKNOWN")
