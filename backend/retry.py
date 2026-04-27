from __future__ import annotations

from typing import Awaitable, Callable, TypeVar

from tenacity import AsyncRetrying, RetryCallState, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter


T = TypeVar("T")


class JobPilotError(RuntimeError):
    code = "jobpilot_error"
    retryable = False
    permanent = False

    def __init__(self, message: str, *, code: str | None = None, context: dict | None = None):
        super().__init__(message)
        if code:
            self.code = code
        self.context = context or {}


class TransientJobError(JobPilotError):
    retryable = True
    code = "transient_error"


class PermanentJobError(JobPilotError):
    permanent = True
    code = "permanent_error"


def retry_state_payload(state: RetryCallState) -> dict:
    return {
        "attempt_number": state.attempt_number,
        "idle_for": state.idle_for,
        "outcome_failed": bool(state.outcome and state.outcome.failed),
    }


async def run_with_retry(
    func: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    min_seconds: float = 0.2,
    max_seconds: float = 3.0,
    retryable_types: tuple[type[BaseException], ...] = (TransientJobError, TimeoutError),
) -> T:
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential_jitter(initial=min_seconds, max=max_seconds),
        retry=retry_if_exception_type(retryable_types),
        reraise=True,
    ):
        with attempt:
            return await func()
    raise RuntimeError("retry loop terminated unexpectedly")
