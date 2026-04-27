from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from backend.config import ROOT_DIR
from backend.contracts import LlmRequest, PrivacyLevel, SpecialistName
from backend.llm.router import ModelRouter
from backend.models.prompt_registry import PromptRegistryError, discover_prompt_metadata

router = APIRouter()


class RoutePreviewRequest(BaseModel):
    specialist: SpecialistName
    system: str = ""
    user: str = ""
    privacy_level: PrivacyLevel = PrivacyLevel.PUBLIC
    requires_json: bool = False
    latency_budget_ms: int = 1000
    max_tokens: int = 256
    temperature: float = 0.2
    quality_tier: str = "fast"
    batch_size: int = 1
    schema_failures: int = 0


@router.get("/specialists")
async def specialists(request: Request) -> dict[str, Any]:
    prompt_dir = ROOT_DIR / "backend" / "models" / "prompts"
    prompts = _prompt_catalog(prompt_dir)
    return {
        "items": [
            {
                "name": specialist.value,
                "prompt_ids": sorted(prompt["prompt_id"] for prompt in prompts if prompt.get("specialist") == specialist.value),
            }
            for specialist in SpecialistName
        ],
        "prompt_count": len(prompts),
        "correlation_id": getattr(request.state, "correlation_id", None),
    }


@router.get("/router/cost")
async def router_cost(request: Request) -> dict[str, Any]:
    model_router = getattr(request.app.state, "router", None)
    ledger = model_router.cost_ledger() if model_router is not None else {}
    quotas = model_router.quota_snapshot() if model_router is not None else {}
    return {
        "ledger": ledger,
        "quotas": quotas,
        "total_calls": sum(int(value) for value in ledger.values()),
        "quota_source": "configured-daily-caps+local-ledger",
        "correlation_id": getattr(request.state, "correlation_id", None),
    }


@router.post("/router/preview")
async def route_preview(body: RoutePreviewRequest, request: Request) -> dict[str, Any]:
    model_router = getattr(request.app.state, "router", None) or ModelRouter()
    llm_request = LlmRequest(
        specialist=body.specialist,
        system=body.system,
        user=body.user,
        privacy_level=body.privacy_level,
        requires_json=body.requires_json,
        latency_budget_ms=body.latency_budget_ms,
        max_tokens=body.max_tokens,
        temperature=body.temperature,
        quality_tier=body.quality_tier,
    )
    decision = model_router.choose(llm_request, batch_size=body.batch_size, schema_failures=body.schema_failures)
    return {
        "provider": decision.provider,
        "model": decision.model,
        "reasons": [reason.value for reason in decision.reasons],
        "local_only": decision.local_only,
        "correlation_id": getattr(request.state, "correlation_id", None),
    }


@router.get("/evals")
async def evals(request: Request) -> dict[str, Any]:
    eval_dir = ROOT_DIR / "tests" / "evals"
    runner = ROOT_DIR / "scripts" / "run_prompt_evals.py"
    configs = sorted(str(path.relative_to(ROOT_DIR)) for path in eval_dir.glob("*/config.yaml")) if eval_dir.exists() else []
    return {
        "runner_exists": runner.exists(),
        "eval_dir_exists": eval_dir.exists(),
        "configs": configs,
        "correlation_id": getattr(request.state, "correlation_id", None),
    }


def _prompt_catalog(prompt_dir: Path) -> list[dict[str, Any]]:
    try:
        return [item.model_dump(mode="json") for item in discover_prompt_metadata(prompt_dir)]
    except (PromptRegistryError, ValueError):
        return []
