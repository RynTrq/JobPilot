from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from backend.contracts import LlmRequest, LlmResponse, PrivacyLevel
from backend.llm.providers import CloudProvider, LlmProviderError
from backend.security.redactor import contains_sensitive


class RouteReason(StrEnum):
    PRIVACY = "PRIVACY"
    LATENCY = "LATENCY"
    SCHEMA = "SCHEMA"
    THROUGHPUT = "THROUGHPUT"
    QUALITY_FALLBACK = "QUALITY_FALLBACK"
    COST = "COST"
    LOCAL_DISABLED = "LOCAL_DISABLED"
    CLIENT_UNAVAILABLE = "CLIENT_UNAVAILABLE"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"


@dataclass(frozen=True, slots=True)
class RouteDecision:
    provider: str
    model: str
    reasons: tuple[RouteReason, ...]
    local_only: bool = False


class ModelRouter:
    """Deterministic privacy-first model routing.

    This is the Phase 1 decision layer. Provider clients and constrained
    decoders can be attached behind this contract without changing callers.
    """

    LOCAL_TINY = "mlx-community/Qwen2.5-1.5B-Instruct-4bit"
    LOCAL_JSON = "mlx-community/Qwen2.5-3B-Instruct-4bit"
    LOCAL_REASONING = "mlx-community/Qwen2.5-7B-Instruct-4bit"
    GROQ_FAST = "llama-3.1-8b-instant"
    GROQ_STRONG = "llama-3.3-70b-versatile"
    GEMINI_FLASH = "gemini-2.5-flash"

    def __init__(
        self,
        *,
        allow_paid: bool = False,
        local_enabled: bool = True,
        provider_clients: dict[str, CloudProvider] | None = None,
        daily_caps: dict[str, int] | None = None,
    ):
        self.allow_paid = allow_paid
        self.local_enabled = local_enabled
        self.provider_clients = provider_clients or {}
        self.daily_caps = {provider: max(0, int(cap)) for provider, cap in (daily_caps or {}).items()}
        self._ledger: dict[str, int] = {}

    def choose(self, request: LlmRequest, *, batch_size: int = 1, schema_failures: int = 0) -> RouteDecision:
        reasons: list[RouteReason] = []
        if request.privacy_level is PrivacyLevel.SENSITIVE or contains_sensitive(request.system, request.user):
            reasons.append(RouteReason.PRIVACY)
            return RouteDecision("local", self._local_model_for(request), tuple(reasons), local_only=True)

        if schema_failures >= 2:
            reasons.append(RouteReason.QUALITY_FALLBACK)
            if request.requires_json:
                reasons.append(RouteReason.SCHEMA)
                return self._cloud_or_local("gemini", self.GEMINI_FLASH, reasons, request)
            return self._cloud_or_local("groq", self.GROQ_STRONG, reasons, request)

        if request.latency_budget_ms < 500:
            reasons.append(RouteReason.LATENCY)
            return self._cloud_or_local("groq", self.GROQ_FAST, reasons, request)

        if batch_size >= 4:
            reasons.append(RouteReason.THROUGHPUT)
            return self._cloud_or_local("groq", self.GROQ_FAST, reasons, request)

        if request.requires_json:
            reasons.append(RouteReason.SCHEMA)
            if self.local_enabled:
                reasons.append(RouteReason.COST)
                return RouteDecision("local", self.LOCAL_JSON, tuple(reasons))
            reasons.append(RouteReason.LOCAL_DISABLED)
            return self._cloud_or_local("gemini", self.GEMINI_FLASH, reasons, request)

        reasons.append(RouteReason.COST)
        if self.local_enabled:
            return RouteDecision("local", self._local_model_for(request), tuple(reasons))
        reasons.append(RouteReason.LOCAL_DISABLED)
        return self._cloud_or_local("groq", self.GROQ_FAST, reasons, request)

    async def complete(
        self,
        request: LlmRequest,
        *,
        generator: Any | None = None,
        batch_size: int = 1,
        schema_failures: int = 0,
    ) -> LlmResponse:
        decision = self.choose(request, batch_size=batch_size, schema_failures=schema_failures)
        self.record_decision(decision)
        provider = decision.provider
        model = decision.model
        reasons = [reason.value for reason in decision.reasons]
        text = ""
        if decision.provider != "local":
            client = self.provider_clients.get(decision.provider)
            if client is not None:
                try:
                    text = await client.complete(request, model=decision.model)
                    return LlmResponse(text=text, provider=provider, model=model, route_reasons=reasons, schema_valid=None)
                except LlmProviderError:
                    reasons.append(RouteReason.PROVIDER_ERROR.value)
            else:
                reasons.append(RouteReason.CLIENT_UNAVAILABLE.value)
        if generator is not None:
            provider = "local"
            model = self._local_model_for(request)
            text = await generator._complete_local(
                request.system,
                request.user,
                request.max_tokens,
                request.temperature,
            )
        return LlmResponse(
            text=text,
            provider=provider,
            model=model,
            route_reasons=reasons,
            schema_valid=None,
        )

    def cost_ledger(self) -> dict[str, int]:
        return dict(self._ledger)

    def quota_snapshot(self) -> dict[str, dict[str, Any]]:
        providers = set(self.daily_caps) | {key.split(":", 1)[0] for key in self._ledger}
        snapshot: dict[str, dict[str, Any]] = {}
        for provider in sorted(providers):
            used = self._provider_call_count(provider)
            cap = self.daily_caps.get(provider)
            remaining = None if cap is None else max(0, cap - used)
            snapshot[provider] = {
                "used": used,
                "cap": cap,
                "remaining": remaining,
                "exhausted": bool(cap is not None and used >= cap),
                "percent_used": None if cap in (None, 0) else round((used / cap) * 100, 2),
            }
        return snapshot

    def record_decision(self, decision: RouteDecision) -> None:
        ledger_key = f"{decision.provider}:{decision.model}"
        self._ledger[ledger_key] = self._ledger.get(ledger_key, 0) + 1

    def _cloud_or_local(
        self,
        provider: str,
        model: str,
        reasons: list[RouteReason],
        request: LlmRequest,
    ) -> RouteDecision:
        if self._provider_exhausted(provider):
            reasons.append(RouteReason.QUOTA_EXHAUSTED)
            return RouteDecision("local", self._local_model_for(request), tuple(reasons), local_only=True)
        return RouteDecision(provider, model, tuple(reasons))

    def _provider_exhausted(self, provider: str) -> bool:
        cap = self.daily_caps.get(provider)
        return bool(cap is not None and self._provider_call_count(provider) >= cap)

    def _provider_call_count(self, provider: str) -> int:
        prefix = f"{provider}:"
        return sum(count for key, count in self._ledger.items() if key.startswith(prefix))

    def _local_model_for(self, request: LlmRequest) -> str:
        if request.requires_json:
            return self.LOCAL_JSON
        if request.max_tokens <= 120:
            return self.LOCAL_TINY
        return self.LOCAL_REASONING if request.quality_tier == "reasoning" else self.LOCAL_JSON
