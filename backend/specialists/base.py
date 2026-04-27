from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from backend.contracts import SpecialistName


@dataclass(frozen=True, slots=True)
class SpecialistContext:
    correlation_id: str
    run_id: int | None = None
    job_url: str | None = None
    budget_ms: int = 1000


@dataclass(frozen=True, slots=True)
class SpecialistResult:
    specialist: SpecialistName
    payload: dict[str, Any]
    latency_ms: int
    schema_valid: bool | None = None
    route_reasons: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)


class Specialist(Protocol):
    name: SpecialistName

    async def run(self, payload: dict[str, Any], context: SpecialistContext) -> SpecialistResult:
        """Run one narrow specialist task behind a typed boundary."""
