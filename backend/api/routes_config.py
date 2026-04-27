from __future__ import annotations

from pydantic import BaseModel
from fastapi import APIRouter, Request

from backend.storage.ground_truth import GroundTruthStore

router = APIRouter(prefix="/config")


class SiteLimitRequest(BaseModel):
    domain: str
    daily_limit: int | None = None


@router.get("/site_limits")
async def list_site_limits(request: Request) -> list[dict]:
    return request.app.state.sqlite.list_site_limits()


@router.put("/site_limits")
async def upsert_site_limit(body: SiteLimitRequest, request: Request) -> dict[str, bool]:
    request.app.state.sqlite.upsert_site_limit(body.domain, body.daily_limit)
    return {"ok": True, "correlation_id": getattr(request.state, "correlation_id", None)}


@router.get("/ground_truth")
async def get_ground_truth(request: Request) -> dict:
    return GroundTruthStore().read() | {"correlation_id": getattr(request.state, "correlation_id", None)}


@router.put("/ground_truth")
async def put_ground_truth(body: dict, request: Request) -> dict[str, bool]:
    GroundTruthStore().write(body)
    return {"ok": True, "correlation_id": getattr(request.state, "correlation_id", None)}
