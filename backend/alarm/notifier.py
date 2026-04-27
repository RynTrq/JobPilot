from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any
from uuid import uuid4

import structlog

log = structlog.get_logger()


@dataclass
class PendingAlarm:
    token: str
    question: str
    details: dict[str, Any]
    future: asyncio.Future[str]


class AlarmNotifier:
    voice_repeat_seconds = 12.0

    def __init__(self, stream=None, store=None):
        self.stream = stream
        self.store = store
        self.pending: PendingAlarm | None = None
        self._voice_task: asyncio.Task[None] | None = None
        # Active Playwright Page — set by the orchestrator on each run. Used in live mode so
        # the UI can tell the backend "the user filled the answer directly in Chrome; read it".
        self.active_page: Any = None

    async def trigger(
        self,
        question: str,
        field_type: str,
        options: list[str] | None = None,
        *,
        context: dict[str, Any] | None = None,
    ) -> str | None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        token = f"alarm:{question}"
        details = {
            "question": question,
            "field_type": field_type,
            "options": options,
            "live_mode": self._live_mode_safe(),
        }
        if context:
            details.update(
                {
                    "run_id": context.get("run_id"),
                    "job_url": context.get("job_url"),
                    "company": context.get("company"),
                    "title": context.get("title"),
                    "field_label": question,
                }
            )
        if self._deferred_questions_mode_enabled():
            self._store_deferred_question(question, field_type, options, context)
            return None
        self.pending = PendingAlarm(token, question, details, future)
        self._beep()
        self._start_voice_alarm(question)
        # In live mode, bring the Chromium window forward so the user can type directly into the form.
        try:
            from backend import config as _config

            if _config.live_mode_enabled() and self.active_page is not None:
                try:
                    await self.active_page.bring_to_front()
                except Exception:
                    pass
        except Exception:
            pass
        if self.store:
            self.store.upsert_pending_action(token, "alarm", details)
        if self.stream:
            await self.stream.publish(
                "alarm",
                {
                    "type": "alarm",
                    "event_type": "alarm",
                    "stage": "alarm",
                    "outcome": "pending",
                    "error_code": None,
                    "token": token,
                    "correlation_id": details.get("correlation_id"),
                    **details,
                },
            )
        try:
            return await asyncio.wait_for(future, timeout=600)
        except asyncio.TimeoutError as exc:
            log.error("alarm_response_timed_out", question=question, field_type=field_type)
            raise TimeoutError(f"Alarm response timed out for question: {question}") from exc
        finally:
            await self._stop_voice_alarm()
            if self.store:
                self.store.clear_pending_action(token)
            self.pending = None

    def fill(self, question: str, answer: str) -> bool:
        if not self.pending or self.pending.question != question:
            return False
        if not self.pending.future.done():
            self.pending.future.set_result(answer)
        if self.store:
            self.store.clear_pending_action(self.pending.token)
        return True

    async def read_from_browser(self) -> str | None:
        """Read the currently-focused input value from the live browser window.

        Returns the trimmed text typed by the user, or None if nothing is focused / readable.
        """
        if self.active_page is None:
            return None
        try:
            value = await self.active_page.evaluate(
                """() => {
                    const el = document.activeElement;
                    if (!el) return null;
                    if (typeof el.value === 'string') return el.value;
                    if (el.isContentEditable) return el.innerText || '';
                    return null;
                }"""
            )
        except Exception:
            return None
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _live_mode_safe() -> bool:
        try:
            from backend import config as _config

            return _config.live_mode_enabled()
        except Exception:
            return False

    @staticmethod
    def _deferred_questions_mode_enabled() -> bool:
        try:
            from backend import config as _config

            return bool(getattr(_config, "DEFERRED_QUESTIONS_MODE", False))
        except Exception:
            return False

    def _store_deferred_question(
        self,
        question: str,
        field_type: str,
        options: list[str] | None,
        context: dict[str, Any] | None,
    ) -> None:
        label = question or "Unknown field"
        job_context = context or {}
        job_id = str(job_context.get("job_url") or job_context.get("id") or "")
        if self.store:
            try:
                self.store.upsert_pending_question(
                    label_normalized=label.strip().lower(),
                    classification=field_type,
                    job_id=job_id,
                    job_title=str(job_context.get("title") or ""),
                    company=str(job_context.get("company") or ""),
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
            except Exception as exc:
                log.error("deferred_question_store_failed", question=label[:80], field_type=field_type, error=str(exc))
        entry = self._deferred_question_entry(label, field_type, options, job_context)
        self._append_deferred_question(entry, job_context)

    @staticmethod
    def _deferred_question_entry(
        question: str,
        field_type: str,
        options: list[str] | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        run_id = context.get("run_id")
        required = bool(context.get("required", False))
        option_list = list(options if options is not None else context.get("options") or [])
        options_en = context.get("options_en")
        if options_en is None:
            options_en = option_list
        return {
            "deferred_id": str(context.get("deferred_id") or uuid4()),
            "run_id": run_id,
            "fixture_url": context.get("fixture_url") or context.get("career_url") or context.get("job_url") or "",
            "company": str(context.get("company") or ""),
            "title": str(context.get("title") or ""),
            "page_index": context.get("page_index", 0),
            "field_id": context.get("field_id") or context.get("dom_id") or context.get("selector") or question.strip().lower(),
            "field_label": question,
            "field_label_en": context.get("field_label_en") or context.get("translated_label") or question,
            "field_type": field_type,
            "options": option_list,
            "options_en": list(options_en or []),
            "required": required,
            "char_limit": context.get("char_limit"),
            "page_screenshot_path": context.get("page_screenshot_path") or context.get("screenshot_path"),
            "page_html_path": context.get("page_html_path") or context.get("html_path"),
            "tier_attempts": list(context.get("tier_attempts") or []),
            "suggested_canonical_field": context.get("suggested_canonical_field"),
            "language": context.get("language") or context.get("detected_language") or "unknown",
            "translation_notes": context.get("translation_notes"),
            "blocking_required_field": bool(context.get("blocking_required_field", required)),
        }

    def _append_deferred_question(self, entry: dict[str, Any], context: dict[str, Any]) -> None:
        try:
            path = self._deferred_questions_path(entry, context)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")
        except Exception as exc:
            log.error(
                "deferred_question_artifact_failed",
                question=str(entry.get("field_label", ""))[:80],
                error=str(exc),
            )

    @staticmethod
    def _deferred_questions_path(entry: dict[str, Any], context: dict[str, Any]) -> Path:
        if context.get("artifact_dir"):
            return Path(str(context["artifact_dir"])) / "deferred_questions.jsonl"
        try:
            from backend import config as _config

            run_id = entry.get("run_id") or "unknown"
            slug = context.get("job_slug") or _safe_slug(
                str(context.get("job_url") or context.get("title") or entry.get("field_label") or "job")
            )
            return _config.OUTPUT_DIR / f"run_{run_id}" / str(slug) / "deferred_questions.jsonl"
        except Exception:
            return Path("deferred_questions.jsonl")

    def _beep(self) -> None:
        """Local CLI/backend fallback beep. The real escalating alarm lives in the menubar app."""
        try:
            import AppKit  # type: ignore

            for _ in range(3):
                AppKit.NSBeep()
        except Exception:
            # macOS is unavailable (dev / Linux). Try terminal bell.
            try:
                import sys

                sys.stdout.write("\a")
                sys.stdout.flush()
            except Exception:
                pass

    def _start_voice_alarm(self, question: str) -> None:
        if self._voice_task is not None and not self._voice_task.done():
            return
        prompt = self._voice_prompt(question)
        self._voice_task = asyncio.create_task(self._voice_alarm_loop(prompt))

    async def _stop_voice_alarm(self) -> None:
        if self._voice_task is None:
            return
        self._voice_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._voice_task
        self._voice_task = None

    async def _voice_alarm_loop(self, prompt: str) -> None:
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
    def _voice_prompt(question: str) -> str:
        cleaned = " ".join(question.split()).strip()
        if not cleaned:
            cleaned = "An issue needs your attention."
        return f"JobPilot alarm. {cleaned}. Please provide an answer in JobPilot."

    def snapshot(self) -> list[dict[str, Any]]:
        if self.pending is None:
            return []
        return [{"token": self.pending.token, **self.pending.details}]


def _safe_slug(value: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in value)[:80].strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "job"
