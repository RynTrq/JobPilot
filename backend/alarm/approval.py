from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import shutil
import subprocess
from typing import Any

import structlog

from backend.config import APPROVAL_TIMEOUT_SECONDS, MANUAL_TAKEOVER_TIMEOUT_SECONDS

log = structlog.get_logger()


@dataclass
class PendingApproval:
    token: str
    details: dict[str, Any]
    future: asyncio.Future[dict[str, Any]]


class _BaseGate:
    action_type = "approval"
    timeout_seconds = APPROVAL_TIMEOUT_SECONDS
    response_key = "approved"
    timeout_code = "expired"

    def __init__(self, stream=None, store=None):
        self.stream = stream
        self.store = store
        self.pending: dict[str, PendingApproval] = {}

    async def _start_attention(self, details: dict[str, Any]) -> None:
        return None

    async def _stop_attention(self) -> None:
        return None

    def _event_payload(self, token: str, details: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": f"{self.action_type}_required",
            "event_type": f"{self.action_type}_required",
            "stage": self.action_type,
            "outcome": "pending",
            "error_code": None,
            "token": token,
            "correlation_id": details.get("correlation_id"),
            **details,
        }

    async def request(self, token: str, details: dict[str, Any]) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self.pending[token] = PendingApproval(token=token, details=details, future=future)
        await self._start_attention(details)
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=self.timeout_seconds)).isoformat()
        if self.store:
            self.store.upsert_pending_action(token, self.action_type, details, correlation_id=details.get("correlation_id"), expires_at=expires_at)
        if self.stream:
            await self.stream.publish(f"{self.action_type}_required", self._event_payload(token, details))
        try:
            return await asyncio.wait_for(future, timeout=self.timeout_seconds)
        except asyncio.TimeoutError as exc:
            log.error("gate_request_timed_out", action_type=self.action_type, token=token)
            if self.store:
                self.store.resolve_pending_action(token, status=self.timeout_code)
            raise TimeoutError(f"{self.action_type} response timed out for token {token}") from exc
        finally:
            self.pending.pop(token, None)
            await self._stop_attention()
            if self.store:
                self.store.clear_pending_action(token)

    def _respond(self, token: str, payload: dict[str, Any]) -> bool:
        pending = self.pending.get(token)
        if pending is None:
            return False
        if pending.future.done():
            return True
        pending.future.set_result(payload)
        if self.store:
            self.store.resolve_pending_action(token, status="resolved")
            self.store.clear_pending_action(token)
        return True

    def cancel_all(self, *, reason: str) -> int:
        cancelled = 0
        for token, pending in list(self.pending.items()):
            if not pending.future.done():
                pending.future.set_exception(asyncio.CancelledError(reason))
                cancelled += 1
            if self.store:
                self.store.resolve_pending_action(token, status="cancelled")
                self.store.clear_pending_action(token)
            self.pending.pop(token, None)
        return cancelled

    def snapshot(self) -> list[dict[str, Any]]:
        return [{"token": token, **pending.details} for token, pending in self.pending.items()]


class ApprovalGate(_BaseGate):
    action_type = "approval"
    timeout_seconds = APPROVAL_TIMEOUT_SECONDS

    def respond(
        self,
        token: str,
        approved: bool,
        field_answers: list[dict[str, Any]] | None = None,
        *,
        reason: str | None = None,
        checks: dict[str, Any] | None = None,
    ) -> bool:
        payload: dict[str, Any] = {"approved": approved, "field_answers": field_answers or []}
        if reason:
            payload["reason"] = reason
        if checks:
            payload["checks"] = checks
        return self._respond(token, payload)


class ClassifierReviewGate(_BaseGate):
    action_type = "classifier_review"
    timeout_seconds = APPROVAL_TIMEOUT_SECONDS
    voice_repeat_seconds = 12.0

    def __init__(self, stream=None, store=None):
        super().__init__(stream=stream, store=store)
        self._attention_task: asyncio.Task[None] | None = None

    async def _start_attention(self, details: dict[str, Any]) -> None:
        if self._attention_task is not None and not self._attention_task.done():
            return
        prompt = self._voice_prompt(details)
        self._attention_task = asyncio.create_task(self._attention_loop(prompt))

    async def _stop_attention(self) -> None:
        if self._attention_task is None:
            return
        self._attention_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._attention_task
        self._attention_task = None

    async def _attention_loop(self, prompt: str) -> None:
        while True:
            await self._speak_once(prompt)
            await asyncio.sleep(self.voice_repeat_seconds)

    async def _speak_once(self, prompt: str) -> None:
        if self._say_command_available():
            try:
                await asyncio.to_thread(self._run_beep_cmd, ["say", prompt])
            except Exception as exc:
                log.debug("classifier_review_voice_alert_failed", error=str(exc))
                try:
                    await asyncio.to_thread(self._run_beep_cmd, ["beep"])
                except Exception:
                    pass
        else:
            try:
                await asyncio.to_thread(self._run_beep_cmd, ["beep"])
            except Exception as exc:
                log.debug("classifier_review_beep_failed", error=str(exc))

    def _say_command_available(self) -> bool:
        try:
            return shutil.which("say") is not None
        except Exception:
            return False

    def _run_beep_cmd(self, cmd: list[str]) -> None:
        subprocess.run(cmd, capture_output=True, timeout=10, check=False)

    def _voice_prompt(self, details: dict[str, Any]) -> str:
        reason = details.get("reason", "Classifier review needed").strip()
        return f"JobPilot alert. {reason}. Please continue in the browser."

    def respond(self, token: str, passed: bool, decision_payload: dict[str, Any] | None = None) -> bool:
        payload: dict[str, Any] = {"passed": passed}
        if decision_payload:
            payload["decision_payload"] = decision_payload
        return self._respond(token, payload)


class ManualTakeoverGate(_BaseGate):
    action_type = "manual_takeover"
    timeout_seconds = MANUAL_TAKEOVER_TIMEOUT_SECONDS
    voice_repeat_seconds = 12.0

    def __init__(self, stream=None, store=None):
        super().__init__(stream=stream, store=store)
        self._attention_task: asyncio.Task[None] | None = None

    async def _start_attention(self, details: dict[str, Any]) -> None:
        if self._attention_task is not None and not self._attention_task.done():
            return
        prompt = self._voice_prompt(details)
        self._attention_task = asyncio.create_task(self._attention_loop(prompt))

    async def _stop_attention(self) -> None:
        if self._attention_task is None:
            return
        self._attention_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._attention_task
        self._attention_task = None

    async def _attention_loop(self, prompt: str) -> None:
        while True:
            await self._speak_once(prompt)
            await asyncio.sleep(self.voice_repeat_seconds)

    async def _speak_once(self, prompt: str) -> None:
        if self._say_command_available():
            try:
                await asyncio.to_thread(self._run_say_command, prompt)
                return
            except Exception:
                pass
        self._beep()

    @staticmethod
    def _say_command_available() -> bool:
        return shutil.which("say") is not None

    @staticmethod
    def _run_say_command(prompt: str) -> None:
        subprocess.run(
            ["say", prompt],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    @staticmethod
    def _beep() -> None:
        try:
            import AppKit  # type: ignore

            for _ in range(3):
                AppKit.NSBeep()
        except Exception:
            try:
                import sys

                sys.stdout.write("\a")
                sys.stdout.flush()
            except Exception:
                pass

    @staticmethod
    def _voice_prompt(details: dict[str, Any]) -> str:
        reason = " ".join(str(details.get("reason") or "Manual browser takeover required").split()).strip()
        return f"JobPilot alert. {reason}. Please continue in the browser."

    def respond(self, token: str, action: str, registered_button: dict | None = None) -> bool:
        payload: dict = {"action": action}
        if registered_button:
            payload["registered_button"] = registered_button
        return self._respond(token, payload)
