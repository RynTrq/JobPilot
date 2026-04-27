from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Any

from backend.config import DATA_DIR, GENERATOR_DISABLED, MODEL_REQUEST_TIMEOUT_SECONDS, MODEL_WORKERS
from backend.contracts import LlmRequest, PrivacyLevel, SpecialistName
from backend.llm.router import ModelRouter

BANNED_WORDS = {
    "passionate",
    "hardworking",
    "quick learner",
    "motivated",
    "driven",
    "team player",
    "go-getter",
    "detail-oriented",
    "results-oriented",
    "synergy",
    "utilize",
}
ACTION_VERBS = {"Built", "Shipped", "Engineered", "Designed", "Reduced", "Optimized", "Integrated"}


@dataclass
class _GenerationRequest:
    system: str
    user: str
    max_tokens: int
    temperature: float
    future: asyncio.Future[str]


class Generator:
    _instance: "Generator | None" = None

    @classmethod
    async def create(cls, router: ModelRouter | None = None) -> "Generator":
        if cls._instance is not None:
            if cls._instance._loop is asyncio.get_running_loop():
                cls._instance.router = router
                await cls._instance._start_workers()
                return cls._instance
            cls._instance = None
        if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("JOBPILOT_USE_REAL_MLX") != "1":
            cls._instance = cls(None, None, router=router)
            await cls._instance._start_workers()
            return cls._instance
        if GENERATOR_DISABLED:
            cls._instance = cls(None, None, router=router)
            await cls._instance._start_workers()
            return cls._instance
        try:
            from mlx_lm import load

            model, tokenizer = await asyncio.to_thread(load, "mlx-community/Qwen2.5-14B-Instruct-4bit")
        except Exception:
            model, tokenizer = None, None
        cls._instance = cls(model, tokenizer, router=router)
        await cls._instance._start_workers()
        return cls._instance

    def __init__(self, model, tokenizer, *, router: ModelRouter | None = None):
        self.model = model
        self.tokenizer = tokenizer
        self.router = router
        self._loop = asyncio.get_running_loop()
        self._queue_maxsize = max(2, MODEL_WORKERS * 4)
        self._queue: asyncio.Queue[_GenerationRequest | None] = asyncio.Queue(maxsize=self._queue_maxsize)
        self._workers: list[asyncio.Task] = []

    async def _start_workers(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop = loop
            self._queue = asyncio.Queue(maxsize=self._queue_maxsize)
            self._workers.clear()
        self._workers = [worker for worker in self._workers if not worker.done()]
        if self._workers:
            return
        for idx in range(MODEL_WORKERS):
            self._workers.append(asyncio.create_task(self._worker(idx)))

    async def _worker(self, worker_id: int) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                self._queue.task_done()
                break
            try:
                result = await self._execute(item.system, item.user, item.max_tokens, item.temperature)
                if not item.future.done():
                    item.future.set_result(result)
            except Exception as exc:
                if not item.future.done():
                    item.future.set_exception(exc)
            finally:
                self._queue.task_done()

    async def _execute(self, system: str, user: str, max_tokens: int, temperature: float) -> str:
        if self.model is None or self.tokenizer is None:
            return self._fallback_complete(user)
        from mlx_lm import generate
        from mlx_lm.sample_utils import make_sampler

        prompt = self.tokenizer.apply_chat_template(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            tokenize=False,
            add_generation_prompt=True,
        )
        sampler = make_sampler(temp=temperature)
        return await asyncio.to_thread(
            generate,
            self.model,
            self.tokenizer,
            prompt=prompt,
            max_tokens=max_tokens,
            sampler=sampler,
            verbose=False,
        )

    async def _complete_local(self, system: str, user: str, max_tokens: int, temperature: float) -> str:
        await self._start_workers()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        await self._queue.put(_GenerationRequest(system, user, max_tokens, temperature, future))
        return await asyncio.wait_for(future, timeout=MODEL_REQUEST_TIMEOUT_SECONDS)

    async def _complete_routed(
        self,
        system: str,
        user: str,
        *,
        specialist: SpecialistName,
        requires_json: bool,
        max_tokens: int,
        temperature: float,
        latency_budget_ms: int = 1000,
        privacy_level: PrivacyLevel = PrivacyLevel.PUBLIC,
        quality_tier: str = "fast",
        batch_size: int = 1,
        schema_failures: int = 0,
    ) -> str:
        if self.router is None:
            return await self._complete_local(system, user, max_tokens, temperature)
        request = LlmRequest(
            specialist=specialist,
            system=system,
            user=user,
            privacy_level=privacy_level,
            requires_json=requires_json,
            latency_budget_ms=latency_budget_ms,
            max_tokens=max_tokens,
            temperature=temperature,
            quality_tier=quality_tier,
        )
        response = await self.router.complete(
            request,
            generator=self,
            batch_size=batch_size,
            schema_failures=schema_failures,
        )
        return response.text

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
        temperature: float = 0.3,
        *,
        specialist: SpecialistName = SpecialistName.FORM_FREETEXT,
        privacy_level: PrivacyLevel = PrivacyLevel.PUBLIC,
    ) -> str:
        return await self._complete_routed(
            system,
            user,
            specialist=specialist,
            requires_json=False,
            max_tokens=max_tokens,
            temperature=temperature,
            privacy_level=privacy_level,
        )

    async def shutdown(self) -> None:
        if self._loop is not asyncio.get_running_loop():
            self._workers.clear()
            if Generator._instance is self:
                Generator._instance = None
            return
        for _ in self._workers:
            await self._queue.put(None)
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        if Generator._instance is self:
            Generator._instance = None

    def unload(self) -> None:
        self.model = None
        self.tokenizer = None

    async def complete_json(
        self,
        system: str,
        user: str,
        *,
        default_key: str,
        required_keys: set[str],
        max_tokens: int = 512,
        temperature: float = 0.1,
        specialist: SpecialistName = SpecialistName.JD_EXTRACTOR,
        privacy_level: PrivacyLevel = PrivacyLevel.PUBLIC,
    ) -> dict[str, Any]:
        for schema_failures, temp in enumerate((temperature, 0.0, 0.0)):
            text = await self._complete_routed(
                system,
                user,
                specialist=specialist,
                requires_json=True,
                max_tokens=max_tokens,
                temperature=temp,
                privacy_level=privacy_level,
                schema_failures=schema_failures,
            )
            try:
                parsed = json.loads(_strip_code_fence(text))
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and required_keys.issubset(parsed.keys()):
                return parsed
        return load_defaults().get(default_key, {})

    async def grounded_answer(
        self,
        *,
        question: str,
        source_blocks: dict[str, Any],
        min_confidence: float,
        max_tokens: int = 400,
        specialist: SpecialistName = SpecialistName.FORM_FREETEXT,
    ) -> dict[str, Any]:
        system = (
            "Answer only from provided source blocks. "
            "If the answer is not present in sources, return UNKNOWN. "
            "Return valid JSON with keys: answer_text, source_keys_used, confidence_0_to_1, unknown_flag, fallback_reason."
        )
        user = json.dumps(
            {
                "question": question,
                "source_blocks": source_blocks,
                "instruction": "If answer not present in sources, return UNKNOWN and unknown_flag=true.",
            },
            sort_keys=True,
        )
        result = await self.complete_json(
            system,
            user,
            default_key="grounded_answer",
            required_keys={"answer_text", "source_keys_used", "confidence_0_to_1", "unknown_flag", "fallback_reason"},
            max_tokens=max_tokens,
            temperature=0.0,
            specialist=specialist,
        )
        result.setdefault("answer_text", "UNKNOWN")
        result.setdefault("source_keys_used", [])
        result.setdefault("confidence_0_to_1", 0.0)
        result.setdefault("unknown_flag", True)
        result.setdefault("fallback_reason", "unknown")
        if float(result["confidence_0_to_1"]) < min_confidence:
            result["unknown_flag"] = True
            result["fallback_reason"] = "low_confidence"
        return result

    async def grounded_json(
        self,
        *,
        task: str,
        source_blocks: dict[str, Any],
        required_keys: set[str],
        defaults: dict[str, Any],
        min_confidence: float,
        max_tokens: int = 500,
        specialist: SpecialistName = SpecialistName.JD_EXTRACTOR,
    ) -> dict[str, Any]:
        system = (
            "Use only the provided source blocks. "
            "If a field is not supported by the sources, use UNKNOWN for strings and [] for lists. "
            "Return valid JSON only."
        )
        user = json.dumps(
            {
                "task": task,
                "source_blocks": source_blocks,
                "required_keys": sorted(required_keys),
                "instruction": "Ground every field in the source blocks. Do not invent facts.",
            },
            sort_keys=True,
        )
        parsed = await self.complete_json(
            system,
            user,
            default_key="grounded_json",
            required_keys=required_keys,
            max_tokens=max_tokens,
            temperature=0.0,
            specialist=specialist,
        )
        if not isinstance(parsed, dict):
            return dict(defaults)
        result = dict(defaults)
        result.update({key: parsed.get(key, defaults.get(key)) for key in required_keys})
        confidence = float(parsed.get("confidence_0_to_1", 1.0))
        if confidence < min_confidence:
            return dict(defaults)
        return result

    async def complete_text_validated(
        self,
        system: str,
        user: str,
        *,
        default_key: str,
        max_tokens: int,
        temperature: float,
        min_words: int | None = None,
        max_words: int | None = None,
        require_tagline: bool = False,
        banned_filter: bool = True,
        banned_phrases: list[str] | None = None,
        project_tech_stack: list[str] | None = None,
        require_action_verb: bool = False,
        specialist: SpecialistName = SpecialistName.RESUME_BULLET_REWRITER,
        privacy_level: PrivacyLevel = PrivacyLevel.PUBLIC,
    ) -> str:
        last_text = ""
        for temp in (temperature, 0.0):
            text = (
                await self._complete_routed(
                    system,
                    user,
                    specialist=specialist,
                    requires_json=False,
                    max_tokens=max_tokens,
                    temperature=temp,
                    privacy_level=privacy_level,
                )
            ).strip()
            last_text = text
            if not validate_text(
                text,
                min_words=min_words,
                max_words=max_words,
                require_tagline=require_tagline,
                banned_filter=banned_filter,
                banned_phrases=banned_phrases,
                project_tech_stack=project_tech_stack,
                require_action_verb=require_action_verb,
            ):
                continue
            return text
        fallback = str(load_defaults().get(default_key, last_text))
        return truncate_to_words(fallback, max_words) if max_words else fallback

    def _fallback_complete(self, user: str) -> str:
        first_line = next((line.strip() for line in user.splitlines() if line.strip()), "")
        return first_line[:600] or "Not enough local context is available to generate a grounded answer."


def load_defaults() -> dict[str, Any]:
    path = DATA_DIR / "defaults.json"
    if not path.exists():
        return {
            "grounded_answer": {"answer_text": "UNKNOWN", "source_keys_used": [], "confidence_0_to_1": 0.0, "unknown_flag": True, "fallback_reason": "defaults_missing"},
            "grounded_json": {},
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _strip_code_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def contains_banned_word(text: str) -> bool:
    lower = text.lower()
    return any(word in lower for word in BANNED_WORDS)


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w&/+.-]+\b", text))


def sentence_count(text: str) -> int:
    return len([part for part in re.split(r"[.!?]+", text.strip()) if part.strip()])


def validate_text(
    text: str,
    *,
    min_words: int | None = None,
    max_words: int | None = None,
    require_tagline: bool = False,
    banned_filter: bool = True,
    banned_phrases: list[str] | None = None,
    project_tech_stack: list[str] | None = None,
    require_action_verb: bool = False,
) -> bool:
    if not text:
        return False
    if require_tagline and text.count(" | ") != 2:
        return False
    if banned_filter and contains_banned_word(text):
        return False
    if banned_phrases:
        lower = text.lower()
        for phrase in banned_phrases:
            pattern = r"\b" + re.escape(phrase.lower()) + r"\b"
            if re.search(pattern, lower):
                return False
    words = word_count(text)
    if min_words is not None and words < int(min_words * 0.75):
        return False
    if max_words is not None and words > int(max_words * 1.25):
        return False
    if require_action_verb:
        first_word = next(iter(text.split()), "").strip(",.")
        if first_word not in ACTION_VERBS:
            return False
    if project_tech_stack:
        lower = text.lower()
        if not any(tech.lower() in lower for tech in project_tech_stack):
            return False
    return True


def truncate_to_words(text: str, max_words: int | None) -> str:
    if max_words is None:
        return text
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]).rstrip(",;:") + "."
