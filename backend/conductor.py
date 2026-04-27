from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import anyio

from backend.orchestrator import (
    Orchestrator,
    RunState,
    _candidate_evidence_block,
    _earliest_start_date,
    _filter_application_fields,
)


class FallbackPolicy(StrEnum):
    RETRY = "retry"
    ESCALATE = "escalate"
    ABORT = "abort"


class StepStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PRECONDITION_FAILED = "precondition_failed"
    POSTCONDITION_FAILED = "postcondition_failed"
    TIMED_OUT = "timed_out"
    ACTION_FAILED = "action_failed"


Predicate = Callable[[Any], bool | Awaitable[bool]]
StepAction = Callable[[Any], Any | Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class StepContract:
    name: str
    pre: list[Predicate] = field(default_factory=list)
    action: StepAction | None = None
    post: list[Predicate] = field(default_factory=list)
    timeout_s: float = 10.0
    on_post_fail: FallbackPolicy = FallbackPolicy.ESCALATE
    audit_artifacts: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class StepExecution:
    name: str
    status: StepStatus
    fallback: FallbackPolicy | None = None
    result: Any = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is StepStatus.SUCCEEDED


class Conductor(Orchestrator):
    """Phase 1 orchestration boundary.

    The existing `Orchestrator` remains the implementation while stages move
    into specialist modules. New callers should depend on `Conductor`; old
    imports stay valid until the migration is complete.
    """

    async def run_step_contract(self, contract: StepContract, context: Any) -> StepExecution:
        """Execute one verified step without fire-and-forget semantics."""
        if contract.action is None:
            return StepExecution(
                name=contract.name,
                status=StepStatus.ACTION_FAILED,
                fallback=FallbackPolicy.ABORT,
                error="step contract has no action",
            )
        try:
            with anyio.fail_after(contract.timeout_s):
                if not await _all_predicates(contract.pre, context):
                    return StepExecution(
                        name=contract.name,
                        status=StepStatus.PRECONDITION_FAILED,
                        fallback=FallbackPolicy.ABORT,
                    )
                result = await _maybe_await(contract.action(context))
                if not await _all_predicates(contract.post, context):
                    return StepExecution(
                        name=contract.name,
                        status=StepStatus.POSTCONDITION_FAILED,
                        fallback=contract.on_post_fail,
                        result=result,
                    )
                return StepExecution(name=contract.name, status=StepStatus.SUCCEEDED, result=result)
        except TimeoutError:
            return StepExecution(
                name=contract.name,
                status=StepStatus.TIMED_OUT,
                fallback=FallbackPolicy.RETRY,
                error=f"step timed out after {contract.timeout_s:.2f}s",
            )
        except Exception as exc:
            return StepExecution(
                name=contract.name,
                status=StepStatus.ACTION_FAILED,
                fallback=FallbackPolicy.RETRY,
                error=f"{exc.__class__.__name__}: {exc}",
            )


async def _all_predicates(predicates: list[Predicate], context: Any) -> bool:
    for predicate in predicates:
        if not bool(await _maybe_await(predicate(context))):
            return False
    return True


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


__all__ = [
    "Conductor",
    "FallbackPolicy",
    "Orchestrator",
    "RunState",
    "StepContract",
    "StepExecution",
    "StepStatus",
    "_candidate_evidence_block",
    "_earliest_start_date",
    "_filter_application_fields",
]
