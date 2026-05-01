from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from backend import config
from backend.config import TEMPLATE_DIR
from backend.artifacts import create_fallback_artifact
from backend.form.field_answerer import build_candidate_data, find_best_option_match, get_answer_for_field_with_metadata
from backend.form.navigator import (
    NavigationError,
    advance_to_next_page,
    check_for_validation_errors,
    click_apply_and_get_form_page,
    detect_apply_button,
    detect_page_type,
    find_next_button,
    find_submit_button,
    wait_for_post_submit_confirmation,
)
from backend.cover_letter.assembler import CoverLetterAssembler
from backend.cover_letter.compiler import compile_latex as compile_cover_latex
from backend.cover_letter.writer import CoverLetterWriter
from backend.resume.assembler import ResumeAssembler
from backend.resume.builder import ResumeContextBuilder
from backend.resume.compiler import compile_latex
from backend.storage.candidate_profile import CandidateProfileStore
from backend.storage.ground_truth import GroundTruthStore

log = structlog.get_logger()


def _latex_retryable(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(token in text for token in ("temporar", "timeout", "busy", "locked", "resource unavailable"))

NOISE_FIELD_KEYWORDS = (
    "cookie",
    "consent",
    "browsing experience",
    "accept cookies",
    "decline",
    "gdpr",
    "privacy notice",
    "privacy policy",
    "legal disclaimer",
    "terms of use",
    "navigation",
    "skip to content",
    "menu",
    "robot",
    "robots only",
    "do not enter if you're human",
    "honeypot",
    "autofill",
    "import resume",
    "import from",
)


@dataclass
class FillOutcome:
    page: Any
    field_answers: list[dict[str, Any]] = field(default_factory=list)
    submitted: bool = False
    submit_error: str | None = None
    confirmation_detected: bool = False
    tier1_count: int = 0
    tier2_count: int = 0
    tier3_count: int = 0
    unanswered_count: int = 0
    pre_submit_audit: dict[str, Any] | None = None
    debug_snapshot: dict[str, Any] | None = None


class FormFiller:
    def __init__(self, app_state: Any):
        self.app_state = app_state
        self.candidate_data = getattr(app_state, "candidate_data", None) or self._candidate_data()
        # Per-job CAPTCHA retry tracking. If a CAPTCHA is seen for the first time on a
        # job we wait briefly and re-check before alarming the user. Only on the second
        # confirmed detection do we escalate to manual takeover.
        self._captcha_retried: set[str] = set()

    def _event_envelope(self, event_type: str, *, message: str | None = None, outcome: str | None = None, error_code: str | None = None, job_url: str | None = None, stage: str | None = None, **extra: Any) -> dict[str, Any]:
        orch = getattr(self.app_state, "orch", None)
        return {
            "type": event_type,
            "event_type": event_type,
            "run_id": getattr(getattr(orch, "state", None), "run_id", None),
            "correlation_id": getattr(getattr(orch, "state", None), "correlation_id", None),
            "stage": stage,
            "message": message,
            "outcome": outcome,
            "error_code": error_code,
            "job_url": job_url,
            **extra,
        }

    async def _emit_progress(
        self,
        stage: str,
        message: str,
        *,
        outcome: str = "in_progress",
        job_url: str | None = None,
        **extra: Any,
    ) -> None:
        """Publish a fine-grained ``stage_progress`` event to the live UI.

        The JobPilot menubar app subscribes to this stream and surfaces
        ``stage`` / ``message`` so the user sees exactly where we are in the
        fill→audit→submit sequence (e.g. "Filling field 4/12: Email" or
        "Waiting for Cloudflare Turnstile to verify").  Use this from the
        filler whenever a step takes more than ~50 ms — the UI should never
        be silent for long enough that the user wonders if we're stuck.
        """
        # Mirror orchestrator._stage so the stage updates the orchestrator's
        # ``current_stage`` / ``current_stage_message`` text on the dashboard.
        orch = getattr(self.app_state, "orch", None)
        orch_state = getattr(orch, "state", None) if orch is not None else None
        if orch_state is not None:
            orch_state.current_stage = stage
            orch_state.current_stage_message = message
        log.info("filler_stage_progress", stage=stage, message=message, **extra)
        try:
            await self.app_state.stream.publish(
                "progress",
                self._event_envelope(
                    "stage_progress",
                    stage=stage,
                    message=message,
                    outcome=outcome,
                    job_url=job_url,
                    **extra,
                ),
            )
        except Exception as exc:
            log.debug("filler_stage_progress_publish_failed", stage=stage, error=str(exc))

    async def _compile_document(self, compile_fn, tex_path: Path, out_dir: Path, *, stage: str) -> str:
        attempts = max(2, config.MAX_RETRY_BUDGET)
        for attempt in range(1, attempts + 1):
            try:
                return str(compile_fn(tex_path, out_dir))
            except Exception as exc:
                if attempt >= attempts or not _latex_retryable(exc):
                    raise
                log.warning("latex_compile_retry", stage=stage, attempt=attempt, max_attempts=attempts, error=str(exc))
                await asyncio.sleep(min(0.2 * attempt, 1.0))

    async def run(
        self,
        *,
        page,
        adapter,
        job_context: dict[str, Any],
        document_paths: dict[str, str | None],
        approval_details: dict[str, Any],
    ) -> FillOutcome:
        outcome = FillOutcome(page=page)
        form_page = await self._open_form_page(page, adapter, job_context["job_url"])
        outcome.page = form_page
        page_number = 1
        max_pages = 20
        no_nav_retries = 0
        max_no_nav_retries = 3
        all_filled_fields: list[dict[str, Any]] = []

        while True:
            log.info("processing_form_page", page_number=page_number, url=getattr(form_page, "url", ""))
            await self._guard_for_manual_takeover(form_page, job_context)
            page_type = await detect_page_type(form_page)
            if page_type == "confirmation_page":
                log.info("application_confirmed", url=getattr(form_page, "url", ""))
                outcome.confirmation_detected = True
                outcome.submitted = True and not config.DRY_RUN
                break
            if page_type == "review_page":
                submit_result = await self._handle_submit(
                    form_page=form_page,
                    adapter=adapter,
                    approval_details=approval_details | {"field_answers": all_filled_fields},
                )
                outcome.submitted = submit_result["submitted"]
                outcome.submit_error = submit_result["submit_error"]
                outcome.confirmation_detected = submit_result["confirmation_detected"]
                outcome.pre_submit_audit = submit_result.get("pre_submit_audit")
                break

            fields = await adapter.enumerate_fields(form_page)
            fields, skipped_noise = _filter_application_fields(fields)
            log.info("page_fields_filtered", page_number=page_number, real_fields=len(fields), skipped_noise=skipped_noise)

            page_answers, unanswered = await self.fill_all_fields_on_page(
                page=form_page,
                adapter=adapter,
                fields=fields,
                job_context=job_context,
                candidate_data=self.candidate_data,
                document_paths=document_paths,
                outcome=outcome,
            )
            all_filled_fields.extend(page_answers)
            outcome.field_answers = all_filled_fields

            if unanswered:
                for entry in unanswered:
                    answer = await self._resolve_unanswered_field(entry["field"], job_context | {"page_index": page_number})
                    if answer is None:
                        outcome.unanswered_count += 1
                        continue
                    try:
                        await self._fill_single_field(form_page, adapter, entry["field"], answer, document_paths, page_answers, job_context)
                    except Exception as exc:
                        log.exception("unanswered_field_fill_failed", field=entry["field"].label_text, error=str(exc))
                        outcome.unanswered_count += 1

            await self._ensure_required_fields(form_page, adapter, fields, job_context, document_paths, page_answers, outcome)
            all_filled_fields = _merge_field_answers(all_filled_fields, page_answers)
            outcome.field_answers = all_filled_fields

            # Workable/Turnstile: CAPTCHA widget renders lazily at bottom of form.
            # Guard runs again after field filling so it catches CAPTCHA that
            # was invisible at the top of the loop.
            await self._guard_for_manual_takeover(form_page, job_context)

            next_button = await find_next_button(form_page)
            if next_button is not None:
                validation_retry = 0
                while True:
                    await advance_to_next_page(form_page)
                    errors = await check_for_validation_errors(form_page)
                    if not errors:
                        fields = await adapter.enumerate_fields(form_page)
                        break
                    validation_retry += 1
                    log.warning("validation_errors_after_next", page_number=page_number, errors=errors, retry=validation_retry)
                    if validation_retry >= 3:
                        await self._manual_takeover(job_context, form_page, "Validation errors blocked page advance")
                        raise NavigationError("Validation errors persisted after 3 retries")
                    await self._ensure_required_fields(form_page, adapter, fields, job_context, document_paths, page_answers, outcome)
                page_number += 1
                if page_number > max_pages:
                    raise NavigationError("Exceeded max pages safety limit")
                continue

            submit_button = await find_submit_button(form_page)
            if submit_button is not None:
                submit_result = await self._handle_submit(
                    form_page=form_page,
                    adapter=adapter,
                    approval_details=approval_details | {"field_answers": all_filled_fields},
                    fields=fields,
                    document_paths=document_paths,
                    page_answers=page_answers,
                    job_context=job_context,
                )
                outcome.submitted = submit_result["submitted"]
                outcome.submit_error = submit_result["submit_error"]
                outcome.confirmation_detected = submit_result["confirmation_detected"]
                outcome.pre_submit_audit = submit_result.get("pre_submit_audit")
                break

            # Check for a disabled submit button — usually means CAPTCHA is unsolved.
            disabled_submit = await _find_disabled_submit_button(form_page)
            if disabled_submit is not None:
                log.warning("submit_button_disabled_captcha_likely", url=getattr(form_page, "url", ""))
                no_nav_retries += 1
                if no_nav_retries > max_no_nav_retries:
                    raise NavigationError("Submit button remained disabled after repeated retries")
                await self._manual_takeover(
                    job_context, form_page,
                    "Submit button is present but disabled — if you see a CAPTCHA (Cloudflare Turnstile) please solve it in the browser window, then press Continue.",
                )
                # After user intervenes, loop back and try again
                continue

            log.error("no_next_or_submit_button_found", url=getattr(form_page, "url", ""))
            no_nav_retries += 1
            if no_nav_retries > max_no_nav_retries:
                raise NavigationError("Neither next nor submit button found after repeated retries")
            await self._manual_takeover(job_context, form_page, "Neither next nor submit button found")
            continue  # button memory updated; retry to find the registered button

        total_fields = len(outcome.field_answers)
        log.info(
            "field_answer_tier_distribution",
            tier1_count=outcome.tier1_count,
            tier2_count=outcome.tier2_count,
            tier3_count=outcome.tier3_count,
            unanswered_count=outcome.unanswered_count,
            total_fields=total_fields,
        )
        return outcome

    async def fill_all_fields_on_page(
        self,
        *,
        page,
        adapter,
        fields: list[Any],
        job_context: dict[str, Any],
        candidate_data: dict[str, Any],
        document_paths: dict[str, str | None],
        outcome: FillOutcome,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        page_answers: list[dict[str, Any]] = []
        unanswered: list[dict[str, Any]] = []
        previous_text_values: dict[str, str] = {}
        ordered_fields = _order_fields_for_dependencies(fields)

        # Log the full field enumeration for debugging (Phase 7: Observability)
        field_summary = [
            {
                "label": (f.label_text or f.name or "?")[:60],
                "type": _normalized_field_type(f),
                "required": getattr(f, "required", False),
                "has_options": bool(getattr(f, "options", None)),
            }
            for f in ordered_fields
        ]
        log.info(
            "form_field_enumeration",
            total_fields=len(ordered_fields),
            required_count=sum(1 for f in field_summary if f["required"]),
            field_types={ft: sum(1 for f in field_summary if f["type"] == ft) for ft in set(f["type"] for f in field_summary)},
            fields=field_summary[:20],  # Cap at 20 to avoid log explosion
        )
        page_fill_start = time.monotonic()

        total_visible = sum(1 for f in ordered_fields if _normalized_field_type(f) != "hidden")
        await self._emit_progress(
            "filling_form",
            f"Filling {total_visible} form field(s) one by one",
            job_url=job_context.get("job_url"),
            total_fields=total_visible,
        )

        idx = 0
        filled_so_far = 0
        while idx < len(ordered_fields):
            form_field = ordered_fields[idx]
            idx += 1
            field_start = time.monotonic()
            try:
                field_type = _normalized_field_type(form_field)
                if field_type == "hidden":
                    continue
                filled_so_far += 1
                progress_label = (form_field.label_text or form_field.name or "field")[:60]
                await self._emit_progress(
                    "filling_field",
                    f"Filling field {filled_so_far}/{total_visible}: {progress_label}",
                    job_url=job_context.get("job_url"),
                    field_label=progress_label,
                    field_index=filled_so_far,
                    total_fields=total_visible,
                    field_type=field_type,
                )
                answer = await self._get_answer(form_field, job_context, candidate_data, outcome)
                if answer is None and not _is_file_like_field(form_field):
                    unanswered.append({"field": form_field})
                    page_answers.append(_field_answer_record(form_field, ""))
                    continue
                if _is_file_like_field(form_field) or field_type == "file":
                    await self._emit_progress(
                        "attaching_document",
                        f"Attaching document to field: {progress_label}",
                        job_url=job_context.get("job_url"),
                        field_label=progress_label,
                    )
                final_value = await self._fill_single_field(page, adapter, form_field, answer, document_paths, page_answers, job_context)
                answer_text = "" if final_value is None else str(final_value)
                page_answers.append(_field_answer_record(form_field, answer_text))

                # After a successful file upload, React/Vue may remount the form,
                # invalidating data-jobpilot-field selectors on remaining fields.
                # Wait for the DOM to settle then re-enumerate so subsequent fills
                # get fresh, valid selectors.
                if (field_type == "file" or _is_file_like_field(form_field)) and final_value:
                    try:
                        await page.wait_for_load_state("networkidle", timeout=4000)
                    except Exception:
                        await asyncio.sleep(1.5)
                    try:
                        fresh_all = await adapter.enumerate_fields(page)
                        fresh_all, _ = _filter_application_fields(fresh_all)
                        already_filled = {_field_key(f) for f in ordered_fields[:idx]}
                        fresh_remaining = [
                            f for f in _order_fields_for_dependencies(fresh_all)
                            if _field_key(f) not in already_filled
                        ]
                        ordered_fields = ordered_fields[:idx] + fresh_remaining
                        log.debug("fields_reenumerated_after_file_upload", fresh_remaining=len(fresh_remaining))
                    except Exception as exc:
                        log.debug("post_file_upload_field_reenumerate_failed", error=str(exc))

                if field_type in {"text", "email", "tel", "number", "textarea", "url", "date"}:
                    previous_text_values[_field_key(form_field)] = answer_text
                if "linkedin" in (form_field.label_text or "").lower():
                    await asyncio.sleep(2)
                    await self._restore_overwritten_fields(page, ordered_fields, previous_text_values)
                field_elapsed = time.monotonic() - field_start
                log.debug("field_filled", label=form_field.label_text, type=field_type, value_preview=answer_text[:50], elapsed_ms=round(field_elapsed * 1000))
                await _post_field_pause()
            except Exception as exc:
                field_elapsed = time.monotonic() - field_start
                log.exception("field_fill_failed", field=form_field.label_text or form_field.name, error=str(exc), elapsed_ms=round(field_elapsed * 1000))
                unanswered.append({"field": form_field})
                page_answers.append(_field_answer_record(form_field, ""))

        page_fill_elapsed = time.monotonic() - page_fill_start
        log.info(
            "page_fill_completed",
            total_fields=len(ordered_fields),
            filled=len(page_answers),
            unanswered=len(unanswered),
            elapsed_seconds=round(page_fill_elapsed, 2),
        )
        return page_answers, unanswered

    async def _open_form_page(self, page, adapter, job_url: str):
        if _adapter_uses_browser_navigation(adapter):
            apply_button_info = await detect_apply_button(page)
            if apply_button_info is None:
                return page
            return await click_apply_and_get_form_page(page, page.context, apply_button_info)
        await adapter.open_application(page, job_url)
        return page

    async def _get_answer(self, field, job_context: dict[str, Any], candidate_data: dict[str, Any], outcome: FillOutcome):
        override = _deterministic_field_answer_override(field, candidate_data)
        if override is not None:
            outcome.tier1_count += 1
            return override
        try:
            result = await asyncio.wait_for(
                get_answer_for_field_with_metadata(
                    field_label=field.label_text or field.name or "",
                    field_type=_normalized_field_type(field),
                    field_options=list(getattr(field, "options", None) or []),
                    job_context={**job_context, "enable_learned_answers": True},
                    candidate_data=candidate_data,
                    encoder=self.app_state.encoder,
                    corpus_embeddings=getattr(self.app_state, "field_answer_corpus_embeddings", None),
                    corpus=getattr(self.app_state, "field_answer_corpus", []),
                    generator=self.app_state.generator,
                ),
                timeout=30.0,
            )
        except Exception as exc:
            log.exception("field_answer_failed", field=field.label_text or field.name, error=str(exc))
            return None
        if result.tier == 1:
            outcome.tier1_count += 1
        elif result.tier == 2:
            outcome.tier2_count += 1
        elif result.tier == 3:
            outcome.tier3_count += 1
        answer = result.answer
        if answer is None or (isinstance(answer, str) and not answer.strip()):
            return None
        return answer

    async def _resolve_unanswered_field(self, field, job_context: dict[str, Any] | None = None):
        alarm_context = dict(job_context or {})
        alarm_context.update(
            {
                "field_id": getattr(field, "element_id", None)
                or getattr(field, "name", None)
                or getattr(field, "selector", None)
                or getattr(field, "label_text", None),
                "selector": getattr(field, "selector", None),
                "required": bool(getattr(field, "required", False)),
                "char_limit": getattr(field, "char_limit", None),
                "options": list(getattr(field, "options", None) or []),
            }
        )
        try:
            return await asyncio.wait_for(
                self.app_state.alarm.trigger(
                    field.label_text or field.name or "Unknown field",
                    field.field_type,
                    field.options,
                    context=alarm_context,
                ),
                timeout=300.0,
            )
        except Exception as exc:
            log.warning("unanswered_field_alarm_failed", field=field.label_text or field.name, error=str(exc))
            return None

    async def _fill_single_field(self, page, adapter, field, answer, document_paths, page_answers, job_context: dict[str, Any]) -> str | None:
        field_type = _normalized_field_type(field)
        if field_type == "file" or _is_file_like_field(field):
            return await self._handle_file_upload(page, field, document_paths, job_context)
        last_error: Exception | None = None
        for attempt in range(1, config.MAX_RETRY_BUDGET + 1):
            try:
                await self._fill_field_by_type(page, field, answer)
                verified = await verify_field_filled(page, field, answer, field_type)
                if verified:
                    return None if answer is None else str(answer)
                log.warning("field_fill_verification_failed", field=field.label_text or field.name, field_type=field_type, attempt=attempt)
            except Exception as exc:
                last_error = exc
                log.warning("field_fill_retry", field=field.label_text or field.name, field_type=field_type, attempt=attempt, error=str(exc))
            await asyncio.sleep(min(0.3 * attempt, 1.0))
        if last_error is not None:
            raise last_error
        return None if answer is None else str(answer)

    async def _handle_file_upload(self, page, field, document_paths: dict[str, str | None], job_context: dict[str, Any]) -> str | None:
        label = _field_display_label(field).lower()
        if any(token in label for token in ("resume", "cv", "curriculum vitae", "upload your resume", "attach resume")):
            already_uploaded = bool(document_paths.get("resume_path"))
            resume_path = await self._ensure_resume_document(document_paths, job_context)
            try:
                await self._set_file_input(page, field, resume_path)
            except Exception as exc:
                if already_uploaded:
                    log.debug("resume_display_field_skip", field=field.label_text or field.name, error=str(exc))
                    return resume_path
                raise
            return resume_path
        if any(token in label for token in ("cover letter", "covering letter", "motivation letter", "letter of interest")):
            already_uploaded = bool(document_paths.get("cover_letter_path"))
            cover_path = await self._ensure_cover_letter_document(document_paths, job_context)
            try:
                await self._set_file_input(page, field, cover_path)
            except Exception as exc:
                if already_uploaded:
                    log.debug("cover_letter_display_field_skip", field=field.label_text or field.name, error=str(exc))
                    return cover_path
                raise
            return cover_path

        # Most single-upload flows are resume uploads but labels vary wildly.
        # Prefer a generated resume before escalating to a manual alarm.
        try:
            resume_path = await self._ensure_resume_document(document_paths, job_context)
            await self._set_file_input(page, field, resume_path)
            return resume_path
        except Exception as exc:
            log.warning("default_resume_upload_failed", field=field.label_text or field.name, error=str(exc))

        manual_path = await self._resolve_unanswered_field(field, job_context)
        if manual_path and Path(manual_path).exists():
            await self._set_file_input(page, field, manual_path)
            return manual_path
        return None

    async def _ensure_required_fields(self, page, adapter, fields, job_context, document_paths, page_answers, outcome) -> None:
        empty_required = [form_field for form_field in fields if form_field.required and not _field_answer_value(page_answers, form_field)]
        for form_field in empty_required:
            if _is_file_like_field(form_field):
                try:
                    final_value = await self._fill_single_field(page, adapter, form_field, None, document_paths, page_answers, job_context)
                    if final_value:
                        _upsert_field_answer(page_answers, form_field, final_value)
                        continue
                except Exception as exc:
                    log.warning("required_file_like_upload_failed", field=form_field.label_text or form_field.name, error=str(exc))
            answer = await self._get_answer(form_field, job_context, self.candidate_data, outcome)
            if answer is None:
                answer = await self._resolve_unanswered_field(form_field, job_context)
            if answer is None:
                outcome.unanswered_count += 1
                continue
            final_value = await self._fill_single_field(page, adapter, form_field, answer, document_paths, page_answers, job_context)
            _upsert_field_answer(page_answers, form_field, final_value if final_value is not None else answer)

    async def _restore_overwritten_fields(self, page, fields, previous_text_values: dict[str, str]) -> None:
        for form_field in fields:
            key = _field_key(form_field)
            expected = previous_text_values.get(key)
            if not expected or not form_field.selector:
                continue
            locator = page.locator(form_field.selector).first
            try:
                actual = await locator.input_value()
            except Exception as exc:
                log.debug("restore_overwritten_field_read_failed", field=form_field.label_text or form_field.name, error=str(exc))
                continue
            if actual != expected:
                log.info("restoring_overwritten_field", field=form_field.label_text or form_field.name)
                if _is_custom_combobox_field(form_field):
                    await self._fill_field_by_type(page, form_field, expected)
                else:
                    # Reuse the hardened fill path so any React-controlled input
                    # is force-cleared before re-typing the expected value.
                    # Using fill("") + type() directly here (the previous
                    # implementation) appends to React-managed inputs whose
                    # value the framework restores between events, producing
                    # concatenated URLs in the rendered field.
                    await _close_open_combobox_menus(page)
                    await self._fill_text_like(locator, expected, delay_range=(20, 60))

    async def _guard_for_manual_takeover(self, page, job_context: dict[str, Any]) -> None:
        if await _captcha_detected(page):
            job_url = str(job_context.get("job_url") or "")
            # Some pages flash a fake CAPTCHA frame for a moment. Try once silently
            # before escalating: wait for the page to settle and re-check. Only fire
            # the alarm if the CAPTCHA is still present on the second check.
            if job_url not in self._captcha_retried:
                self._captcha_retried.add(job_url)
                log.info("captcha_first_detection_retry_once", job_url=job_url)
                clicked_continue = await _try_continue_past_transient_captcha(page)
                if clicked_continue:
                    log.info("captcha_continue_clicked_once", job_url=job_url)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                await asyncio.sleep(3.0)
                if not await _captcha_detected(page):
                    log.info("captcha_cleared_after_retry", job_url=job_url)
                else:
                    log.warning("captcha_persists_after_retry_alarming", job_url=job_url)
                    await self._manual_takeover(job_context, page, "CAPTCHA detected — Human intervention required.")
                    try:
                        await page.wait_for_load_state("networkidle")
                    except Exception:
                        pass
            else:
                # Already retried once for this job in a previous guard call —
                # any further sighting is a confirmed CAPTCHA, alarm immediately.
                await self._manual_takeover(job_context, page, "CAPTCHA detected — Human intervention required.")
                try:
                    await page.wait_for_load_state("networkidle")
                except Exception:
                    pass
        page_url = (getattr(page, "url", "") or "").lower()
        if any(token in page_url for token in ("/login", "/signin", "/auth")):
            await self._manual_takeover(job_context, page, "Session expired — please log in again in the browser window.")
            try:
                await page.wait_for_load_state("networkidle")
            except Exception:
                pass

    async def _manual_takeover(self, job_context: dict[str, Any], page, reason: str) -> None:
        from urllib.parse import urlparse
        domain = urlparse(job_context["job_url"]).netloc or "unknown"
        token = f"manual:{job_context.get('run_id', 'run')}:{_safe_slug(job_context['job_url'])}"
        takeover = await self.app_state.manual_takeover.request(
            token,
            {
                "job_url": job_context["job_url"],
                "company": job_context.get("company"),
                "title": job_context.get("title"),
                "reason": reason,
                "current_url": getattr(page, "url", job_context["job_url"]),
                "domain": domain,
                "allow_button_name_registration": "Neither next nor submit button found" in reason,
            },
        )
        if takeover.get("action") != "continue":
            raise RuntimeError(reason)
        # If user registered a button name, update the module-level singleton used by
        # find_next_button / find_submit_button so the retry on this same run sees it.
        if takeover.get("registered_button"):
            from backend.form.navigator import _button_memory
            _button_memory.register_button_name(
                takeover["registered_button"]["type"],
                takeover["registered_button"]["name"],
                domain,
            )
            log.info(
                "button_name_registered_from_user",
                button_type=takeover["registered_button"]["type"],
                name=takeover["registered_button"]["name"],
                domain=domain,
            )

    async def _request_page_approval_and_apply_edits(self, form_page, adapter, approval_details, fields, document_paths, page_answers, job_context) -> bool:
        if config.auto_submit_without_approval_enabled():
            return True
            
        token = approval_details.get("approval_token") or f"approval:{_safe_slug(approval_details.get('job_url', 'job'))}"
        # We append a page id if we need to differentiate, but token can be re-used if it's cleared.
        response = await self.app_state.approval.request(token, approval_details)
        approved = bool(response.get("approved"))
        if not approved:
            if _qa_dry_run_decline_can_continue(approval_details):
                log.info(
                    "dry_run_approval_declined_continue",
                    job_url=approval_details.get("job_url"),
                    token=token,
                )
                return True
            return False
            
        updated_answers = response.get("field_answers") or []
        updated_by_key = {a["key"]: a["value"] for a in updated_answers}
        for current_ans in page_answers:
            key = current_ans["key"]
            if key in updated_by_key and updated_by_key[key] != current_ans["value"]:
                new_value = updated_by_key[key]
                form_field = next((f for f in fields if _field_key(f) == key), None)
                if form_field:
                    log.info("applying_user_edit", field=form_field.label_text or form_field.name, new_value=new_value[:30])
                    try:
                        await self._fill_single_field(form_page, adapter, form_field, new_value, document_paths, page_answers, job_context)
                        current_ans["value"] = new_value
                    except Exception as exc:
                        log.warning("user_edit_apply_failed", field=form_field.label_text or form_field.name, error=str(exc))
        return True

    async def _handle_submit(
        self,
        *,
        form_page,
        adapter,
        approval_details: dict[str, Any],
        fields: list[Any] | None = None,
        document_paths: dict[str, str | None] | None = None,
        page_answers: list[dict[str, Any]] | None = None,
        job_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current_fields = fields
        if current_fields is None:
            current_fields = await adapter.enumerate_fields(form_page)
            current_fields, _ = _filter_application_fields(current_fields)
        audit = _build_pre_submit_audit(current_fields, approval_details.get("field_answers", []))
        await _augment_pre_submit_audit_from_dom(form_page, current_fields, audit)
        if config.FORM_DEBUG_SNAPSHOTS:
            audit["debug_snapshot"] = _build_debug_snapshot(current_fields, approval_details.get("field_answers", []))
        if audit["blocked"]:
            # Try to resolve missing required fields by alarming/asking the user
            # before giving up. The user explicitly asked that DRY RUN must not
            # mark a job as done while any asterisked field is empty, and that
            # REAL SUBMIT must fill or ask for any blocker before marking done.
            current_fields, audit = await self._resolve_audit_blockers(
                form_page=form_page,
                adapter=adapter,
                fields=current_fields,
                page_answers=page_answers or approval_details.get("field_answers", []),
                document_paths=document_paths or {},
                job_context=job_context or {},
                audit=audit,
                approval_details=approval_details,
            )
            if audit["blocked"]:
                await self.app_state.stream.publish(
                    "progress",
                    self._event_envelope("pre_submit_blocked", stage="pre_submit", message="Pre-submit audit blocked submission", outcome="blocked", error_code="pre_submit_blocked", job_url=approval_details.get("job_url"), audit=audit),
                )
                raise RuntimeError(json.dumps(audit, sort_keys=True))

        if not config.auto_submit_without_approval_enabled():
            approval_payload = approval_details | {"pre_submit_audit": audit}
            approved = await self._request_page_approval_and_apply_edits(
                form_page,
                adapter,
                approval_payload,
                current_fields,
                document_paths or {},
                page_answers or approval_details.get("field_answers", []),
                job_context or {},
            )
            if not approved:
                raise RuntimeError("approval not granted by user")
            current_fields = await adapter.enumerate_fields(form_page)
            current_fields, _ = _filter_application_fields(current_fields)
            audit = _build_pre_submit_audit(current_fields, approval_details.get("field_answers", []))
            await _augment_pre_submit_audit_from_dom(form_page, current_fields, audit)
            if audit["blocked"]:
                current_fields, audit = await self._resolve_audit_blockers(
                    form_page=form_page,
                    adapter=adapter,
                    fields=current_fields,
                    page_answers=page_answers or approval_details.get("field_answers", []),
                    document_paths=document_paths or {},
                    job_context=job_context or {},
                    audit=audit,
                    approval_details=approval_details,
                )
            if audit["blocked"]:
                await self.app_state.stream.publish(
                    "progress",
                    self._event_envelope("pre_submit_blocked", stage="pre_submit", message="Pre-submit audit blocked submission", outcome="blocked", error_code="pre_submit_blocked", job_url=approval_details.get("job_url"), audit=audit),
                )
                raise RuntimeError(json.dumps(audit, sort_keys=True))

        page_type = await detect_page_type(form_page)
        if page_type == "review_page":
            await self.app_state.stream.publish("progress", self._event_envelope("review_page_detected", stage="review_page", message="Review page detected", outcome="ready", job_url=approval_details.get("job_url"), **approval_details, pre_submit_audit=audit))

        if config.runtime_settings().dry_run:
            # Final guard for DRY RUN: re-enumerate and verify no required field
            # (asterisked) is empty before declaring the dry run done.
            final_fields = await adapter.enumerate_fields(form_page)
            final_fields, _ = _filter_application_fields(final_fields)
            final_audit = _build_pre_submit_audit(final_fields, approval_details.get("field_answers", []))
            await _augment_pre_submit_audit_from_dom(form_page, final_fields, final_audit)
            if final_audit["blocked"]:
                final_fields, final_audit = await self._resolve_audit_blockers(
                    form_page=form_page,
                    adapter=adapter,
                    fields=final_fields,
                    page_answers=page_answers or approval_details.get("field_answers", []),
                    document_paths=document_paths or {},
                    job_context=job_context or {},
                    audit=final_audit,
                    approval_details=approval_details,
                )
            if final_audit["blocked"]:
                await self.app_state.stream.publish(
                    "progress",
                    self._event_envelope(
                        "dry_run_blocked_required",
                        stage="dry_run_finalize",
                        message="Dry run blocked: required fields still empty",
                        outcome="blocked",
                        error_code="dry_run_required_fields_missing",
                        job_url=approval_details.get("job_url"),
                        audit=final_audit,
                    ),
                )
                return {
                    "submitted": False,
                    "submit_error": "dry_run_required_fields_missing: " + ", ".join(final_audit.get("required_fields_missing", [])[:5]),
                    "confirmation_detected": False,
                    "pre_submit_audit": final_audit,
                }
            log.info("dry_run_submit_skipped", url=getattr(form_page, "url", ""))
            return {"submitted": False, "submit_error": None, "confirmation_detected": False, "pre_submit_audit": final_audit}

        # Pre-submit gate.  This is the LAST guard before we click submit and
        # the only place that enforces the user-facing contract:
        #
        #   "Submit only after the form is fully filled AND the resume is
        #    attached AND any anti-bot challenge has been solved."
        #
        # The pre-submit audit above has already covered required-field DOM
        # values; here we additionally:
        #   (a) await any background document task that the orchestrator
        #       launched in parallel with the field fill, so we never click
        #       submit before the resume PDF is on disk;
        #   (b) verify the file-input(s) actually carry attached files,
        #       not just that we *intended* to attach them;
        #   (c) wait for an anti-bot challenge (Cloudflare Turnstile) to
        #       resolve, escalating to manual takeover when it doesn't.
        _captcha_jctx = job_context or {"job_url": approval_details.get("job_url", "")}
        await self._await_pending_document_tasks(document_paths or {}, _captcha_jctx.get("job_url"))
        await self._verify_documents_attached(form_page, adapter, document_paths or {}, _captcha_jctx.get("job_url"))
        await self._emit_progress(
            "pre_submit_audit",
            "Verifying every required field is filled and the resume is attached",
            job_url=_captcha_jctx.get("job_url"),
        )
        # Pre-submit CAPTCHA check: Workable (and similar SPAs) show a Cloudflare
        # Turnstile on the final page.  The widget must be solved before clicking
        # Submit, otherwise the button goes to "Submitting…" indefinitely.
        if await _captcha_detected(form_page):
            log.warning("captcha_before_submit_manual_takeover", url=getattr(form_page, "url", ""))
            await self._emit_progress(
                "waiting_for_captcha",
                "Cloudflare Turnstile detected — waiting for human verification before submitting",
                outcome="waiting_for_user",
                job_url=_captcha_jctx.get("job_url"),
            )
            await self._manual_takeover(
                _captcha_jctx,
                form_page,
                "CAPTCHA (Cloudflare Turnstile) must be solved before submitting — please tick the 'Verify you are human' checkbox in the browser, then press Continue.",
            )
        submit_button = await find_submit_button(form_page)
        if submit_button is None:
            raise NavigationError("Submit button not found on final page")
        await submit_button.scroll_into_view_if_needed()
        await asyncio.sleep(random.uniform(config.BROWSER_HUMAN_DELAY_MIN_SECONDS, config.BROWSER_HUMAN_DELAY_MAX_SECONDS))
        await self._emit_progress(
            "submitting",
            "Clicking Submit — the form is filled and the resume is attached",
            job_url=_captcha_jctx.get("job_url"),
        )
        await submit_button.click()
        # For React/SPA forms (e.g. Workable), the submit is async — the button
        # shows "Submitting" and the page content changes client-side without a
        # full navigation.  Poll for confirmation text first, fall back to
        # networkidle if that fails.
        confirmed = await wait_for_post_submit_confirmation(form_page, timeout_ms=12000)
        if not confirmed:
            try:
                await form_page.wait_for_load_state("networkidle", timeout=10000)
            except Exception as exc:
                log.warning("submit_wait_networkidle_failed", url=getattr(form_page, "url", ""), error=str(exc))
        page_type = await detect_page_type(form_page)
        if page_type == "confirmation_page" or confirmed:
            return {"submitted": True, "submit_error": None, "confirmation_detected": True, "pre_submit_audit": audit}
        errors = await check_for_validation_errors(form_page)
        if errors:
            # The browser is telling us a real submit failed because of missing
            # or invalid fields. Try once more to fill them by re-enumerating
            # and asking the user for anything we can't answer ourselves; only
            # then re-attempt the submit. This matches the user's requirement:
            # never mark a real submit as done while the browser still has
            # outstanding field complaints.
            log.warning("submit_validation_errors_detected_retrying", url=getattr(form_page, "url", ""), errors=errors)
            audit["post_submit_validation_errors"] = errors
            retry_fields = await adapter.enumerate_fields(form_page)
            retry_fields, _ = _filter_application_fields(retry_fields)
            retry_audit = _build_pre_submit_audit(retry_fields, approval_details.get("field_answers", []))
            await _augment_pre_submit_audit_from_dom(form_page, retry_fields, retry_audit)
            retry_fields, retry_audit = await self._resolve_audit_blockers(
                form_page=form_page,
                adapter=adapter,
                fields=retry_fields,
                page_answers=page_answers or approval_details.get("field_answers", []),
                document_paths=document_paths or {},
                job_context=job_context or {},
                audit=retry_audit,
                approval_details=approval_details,
            )
            if not retry_audit["blocked"]:
                submit_button = await find_submit_button(form_page)
                if submit_button is not None:
                    await submit_button.scroll_into_view_if_needed()
                    await asyncio.sleep(random.uniform(config.BROWSER_HUMAN_DELAY_MIN_SECONDS, config.BROWSER_HUMAN_DELAY_MAX_SECONDS))
                    await submit_button.click()
                    confirmed = await wait_for_post_submit_confirmation(form_page, timeout_ms=12000)
                    if not confirmed:
                        try:
                            await form_page.wait_for_load_state("networkidle", timeout=10000)
                        except Exception as exc:
                            log.warning("submit_wait_networkidle_failed", url=getattr(form_page, "url", ""), error=str(exc))
                    if await detect_page_type(form_page) == "confirmation_page" or confirmed:
                        return {"submitted": True, "submit_error": None, "confirmation_detected": True, "pre_submit_audit": retry_audit}
                    errors = await check_for_validation_errors(form_page)
            if errors:
                retry_audit["post_submit_validation_errors"] = errors
                retry_audit["blocked"] = True
                message = "Submit blocked by validation errors: " + "; ".join(errors[:5])
                log.warning("submit_validation_errors_detected", url=getattr(form_page, "url", ""), errors=errors)
                return {"submitted": False, "submit_error": message, "confirmation_detected": False, "pre_submit_audit": retry_audit}
        # CAPTCHA (e.g. Cloudflare Turnstile) blocks the submit without raising a
        # validation error — the button shows "Submitting…" indefinitely.  Detect
        # this case and hand off to the user to solve it; then retry once.
        if await _captcha_detected(form_page):
            log.warning("captcha_blocking_submit_manual_takeover", url=getattr(form_page, "url", ""))
            await self._manual_takeover(
                _captcha_jctx,
                form_page,
                "CAPTCHA (Cloudflare Turnstile) is blocking the final submit — please tick the 'Verify you are human' checkbox in the browser, then click Submit Application.",
            )
            # User may have already submitted manually during the takeover window.
            # Check for confirmation BEFORE re-clicking to avoid double-submit.
            confirmed = await wait_for_post_submit_confirmation(form_page, timeout_ms=2000)
            if not confirmed:
                confirmed = await detect_page_type(form_page) == "confirmation_page"
            if not confirmed:
                # Re-find and click the submit button only if not yet submitted.
                submit_button = await find_submit_button(form_page)
                if submit_button is not None and not await submit_button.is_disabled():
                    await submit_button.scroll_into_view_if_needed()
                    await asyncio.sleep(random.uniform(config.BROWSER_HUMAN_DELAY_MIN_SECONDS, config.BROWSER_HUMAN_DELAY_MAX_SECONDS))
                    await submit_button.click()
            confirmed = await wait_for_post_submit_confirmation(form_page, timeout_ms=20000)
            if not confirmed:
                try:
                    await form_page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
            if await detect_page_type(form_page) == "confirmation_page" or confirmed:
                return {"submitted": True, "submit_error": None, "confirmation_detected": True, "pre_submit_audit": audit}
        log.warning("submit_without_confirmation", url=getattr(form_page, "url", ""))
        return {"submitted": False, "submit_error": "submit_not_confirmed_manual_review_required", "confirmation_detected": False, "pre_submit_audit": audit}

    async def _resolve_audit_blockers(
        self,
        *,
        form_page,
        adapter,
        fields: list[Any],
        page_answers: list[dict[str, Any]],
        document_paths: dict[str, str | None],
        job_context: dict[str, Any],
        audit: dict[str, Any],
        approval_details: dict[str, Any],
    ) -> tuple[list[Any], dict[str, Any]]:
        """Try to fill every missing required (asterisked) field flagged by the
        audit, asking the user via the alarm if we don't know the answer. Used by
        both the DRY RUN finalize step and the REAL SUBMIT post-submit validation
        retry so we never mark a job as done while a required field is blank.

        Returns the (re-enumerated) fields list and a freshly built audit that
        reflects whatever we managed to fill.
        """
        missing_labels: list[str] = []
        for key in ("required_fields_missing", "unresolved_critical_questions", "file_upload_missing"):
            for label in audit.get(key) or []:
                if label and label not in missing_labels:
                    missing_labels.append(label)
        if not missing_labels:
            return fields, audit

        await self.app_state.stream.publish(
            "progress",
            self._event_envelope(
                "audit_blocker_resolution_started",
                stage="audit_resolution",
                message=f"Attempting to fill {len(missing_labels)} required field(s)",
                outcome="in_progress",
                job_url=approval_details.get("job_url"),
                missing_required_fields=missing_labels,
            ),
        )

        for form_field in fields:
            label = form_field.label_text or form_field.name or ""
            if label not in missing_labels:
                continue
            if not getattr(form_field, "required", False):
                continue
            answer = await self._get_answer(form_field, job_context, self.candidate_data, FillOutcome(page=form_page))
            if answer is None and _is_file_like_field(form_field):
                try:
                    final_value = await self._fill_single_field(form_page, adapter, form_field, None, document_paths, page_answers, job_context)
                    if final_value:
                        _upsert_field_answer(page_answers, form_field, final_value)
                        continue
                except Exception as exc:
                    log.warning("audit_required_file_upload_failed", field=label, error=str(exc))
            if answer is None:
                answer = await self._resolve_unanswered_field(form_field, job_context)
            if answer is None:
                continue
            try:
                final_value = await self._fill_single_field(form_page, adapter, form_field, answer, document_paths, page_answers, job_context)
                _upsert_field_answer(page_answers, form_field, final_value if final_value is not None else answer)
            except Exception as exc:
                log.warning("audit_required_fill_failed", field=label, error=str(exc))

        # Re-enumerate the form and rebuild the audit so callers see a current
        # picture (a previously red required field now reads green if we filled
        # it successfully).
        try:
            fresh_fields = await adapter.enumerate_fields(form_page)
            fresh_fields, _ = _filter_application_fields(fresh_fields)
        except Exception as exc:
            log.debug("audit_reenumerate_failed", error=str(exc))
            fresh_fields = fields
        fresh_audit = _build_pre_submit_audit(fresh_fields, page_answers)
        await _augment_pre_submit_audit_from_dom(form_page, fresh_fields, fresh_audit)
        return fresh_fields, fresh_audit

    def _candidate_data(self) -> dict[str, Any]:
        return build_candidate_data(
            GroundTruthStore().read_if_exists(),
            CandidateProfileStore().read_if_exists(),
        )

    async def _fill_field_by_type(self, page, field, answer) -> None:
        field_type = _normalized_field_type(field)
        field_role = (getattr(field, "role", "") or "").lower()
        field_tag = (getattr(field, "tag", "") or "").lower()
        locator = _field_locator(page, field)
        await _close_open_combobox_menus(page)
        await locator.scroll_into_view_if_needed()
        if field_type == "select" and field_tag == "select":
            await self._fill_native_select(locator, field, str(answer or ""))
            return
        if field_role in {"combobox", "listbox"} or (field_type == "select" and field_tag != "select"):
            await self._fill_combobox(page, locator, str(answer or ""))
            return
        if field_type in {"text", "email", "number", "url"}:
            await self._fill_text_like(locator, str(answer or ""), delay_range=(30, 80))
            return
        if field_type == "tel":
            selected_country_code = await _selected_phone_country_code(locator, answer)
            await self._fill_text_like(
                locator,
                _format_phone_answer(
                    answer,
                    field.placeholder or "",
                    local_number=selected_country_code is not None,
                    dial_code=selected_country_code,
                ),
                delay_range=(30, 80),
            )
            return
        if field_type == "date":
            if await self._is_workday_date_picker(field, locator):
                await self._fill_workday_date_picker(page, locator, str(answer or ""))
            else:
                await self._fill_native_date(locator, str(answer or ""))
            return
        if field_type == "textarea":
            await self._fill_text_like(locator, str(answer or ""), delay_range=(20, 60))
            return
        if field_type == "checkbox":
            await self._fill_checkbox(page, field, answer)
            return
        if field_type == "radio":
            await self._fill_radio(page, field, str(answer or ""))
            return
        await self._fill_text_like(locator, str(answer or ""), delay_range=(30, 80))

    async def _fill_text_like(self, locator, value: str, delay_range: tuple[int, int]) -> None:
        await locator.click()
        try:
            await locator.fill(value)
            await locator.evaluate(
                """(el) => {
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    if (el.blur) el.blur();
                }"""
            )
            if await _locator_contains_value(locator, value):
                return
        except Exception as exc:
            log.debug("field_fast_fill_failed", error=str(exc))
        # Hard clear the input.  Some React-controlled inputs (Workable, Lever,
        # Greenhouse) revert ``locator.fill('')`` on the next render because
        # they ignore non-React-driven value mutations.  Use the native value
        # setter so React's onChange observer sees an empty value, then verify
        # via input_value() before typing — otherwise locator.type() would
        # APPEND to a leftover URL and produce concatenated values like
        # ``https://github.com/Xhttps://other.example/``.
        try:
            await locator.fill("")
        except Exception as exc:
            log.debug("field_clear_via_fill_failed", error=str(exc))
            try:
                await locator.press("Control+a")
                await locator.press("Delete")
            except Exception:
                pass
        if not await _locator_contains_value(locator, ""):
            try:
                await locator.evaluate(
                    """(el) => {
                        if (!el) return;
                        const proto = (el.tagName === 'TEXTAREA')
                            ? window.HTMLTextAreaElement.prototype
                            : window.HTMLInputElement.prototype;
                        const setter = Object.getOwnPropertyDescriptor(proto, 'value');
                        if (setter && setter.set) {
                            setter.set.call(el, '');
                        } else if ('value' in el) {
                            el.value = '';
                        } else if (el.isContentEditable) {
                            el.textContent = '';
                        }
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }"""
                )
            except Exception as exc:
                log.debug("field_native_setter_clear_failed", error=str(exc))
            if not await _locator_contains_value(locator, ""):
                try:
                    await locator.press("Control+a")
                    await locator.press("Delete")
                except Exception as exc:
                    log.debug("field_select_all_clear_failed", error=str(exc))
        delay = 0 if _interaction_delay_disabled() else random.randint(*delay_range)
        await locator.type(value, delay=delay)

    async def _fill_native_date(self, locator, value: str) -> None:
        await locator.fill(value)
        try:
            actual = await locator.input_value()
        except Exception as exc:
            log.debug("native_date_input_value_failed", error=str(exc))
            actual = ""
        if actual.strip() != value.strip():
            await locator.click()
            await locator.type(value, delay=40)

    async def _is_workday_date_picker(self, field, locator) -> bool:
        if "datepicker" in ((getattr(field, "selector", "") or "").lower()):
            return True
        try:
            automation_id = (await locator.get_attribute("data-automation-id") or "").lower()
        except Exception as exc:
            log.debug("workday_datepicker_attribute_read_failed", field=field.label_text or field.name, error=str(exc))
            automation_id = ""
        return "datepicker" in automation_id

    async def _fill_workday_date_picker(self, page, locator, value: str) -> None:
        mmddyyyy = _to_mmddyyyy(value)
        await locator.click()
        if not _interaction_delay_disabled():
            await page.wait_for_timeout(150)
        date_input = await page.query_selector("[data-automation-id='datePickerInputBox']")
        if date_input is not None:
            await date_input.fill(mmddyyyy)
        else:
            await page.keyboard.type(mmddyyyy)
        await page.keyboard.press("Tab")

    async def _fill_native_select(self, locator, field, value: str) -> None:
        try:
            await locator.select_option(label=value)
            return
        except Exception as exc:
            log.debug("select_option_by_label_failed", field=field.label_text or field.name, answer=value, error=str(exc))
        try:
            await locator.select_option(value=value)
            return
        except Exception as exc:
            log.debug("select_option_by_value_failed", field=field.label_text or field.name, answer=value, error=str(exc))
        best = find_best_option_match(value, list(getattr(field, "options", None) or []))
        if best:
            await locator.select_option(label=best)
        else:
            log.warning("dropdown_no_match", field=field.label_text or field.name, answer=value)

    async def _fill_combobox(self, page, locator, value: str) -> None:
        await _close_open_combobox_menus(page)
        await locator.click(force=True)
        try:
            await page.keyboard.press("Control+a")
            await page.keyboard.press("Delete")
        except Exception as exc:
            log.debug("combobox_keyboard_clear_failed", error=str(exc))
        if not _interaction_delay_disabled():
            await page.wait_for_timeout(150)
        await locator.type(value, delay=0 if _interaction_delay_disabled() else 30)
        if not _interaction_delay_disabled():
            await page.wait_for_timeout(150)
        option_scope = await _combobox_option_scope_selector(locator)
        try:
            wait_selector = f"{option_scope} [role='option']" if option_scope else "[role='listbox'], [role='option'], .dropdown-menu"
            await page.wait_for_selector(wait_selector, timeout=1200)
        except Exception as exc:
            log.debug("combobox_option_list_not_observed", error=str(exc))
        target_option = await _best_visible_combobox_option(page, value, option_scope)
        if target_option is not None:
            await target_option.click(force=True)
        else:
            try:
                await page.keyboard.press("Enter")
            except Exception as exc:
                log.debug("combobox_enter_fallback_failed", error=str(exc))
        try:
            await locator.evaluate(
                """(el) => {
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    if (el.blur) el.blur();
                }"""
            )
        except Exception as exc:
            log.debug("combobox_event_dispatch_failed", error=str(exc))
        await _close_open_combobox_menus(page)

    async def _fill_checkbox(self, page, field, answer) -> None:
        locator = _field_locator(page, field)
        label = (field.label_text or "").lower()
        group = await _checkbox_group(page, field)
        truthy = str(answer).strip().lower() in {"yes", "true", "1", "checked", "on"}
        if len(group) > 1:
            desired_answers = _checkbox_desired_answers(answer)
            labels = [checkbox_label for _checkbox, checkbox_label in group]
            selected_labels: set[str] = set()
            for desired in desired_answers:
                match = find_best_option_match(desired, labels) or desired
                for _checkbox, checkbox_label in group:
                    if checkbox_label == match or checkbox_label.lower() == match.lower():
                        selected_labels.add(checkbox_label)
            for checkbox, checkbox_label in group:
                should_check = checkbox_label in selected_labels
                try:
                    checked = await checkbox.is_checked()
                except Exception:
                    checked = False
                if should_check and not checked:
                    await checkbox.check(force=True)
                elif not should_check and checked:
                    await checkbox.uncheck(force=True)
            return
        if any(token in label for token in ("agree", "terms", "authorize", "consent", "acknowledge")):
            try:
                already = await locator.is_checked()
            except Exception:
                already = False
            if not already:
                try:
                    await locator.check(force=True)
                except Exception:
                    try:
                        await locator.click(force=True)
                    except Exception:
                        await locator.evaluate("el => el.click()")
            return
        try:
            is_checked = await locator.is_checked()
        except Exception:
            is_checked = False
        if truthy and not is_checked:
            try:
                await locator.check(force=True)
            except Exception:
                await locator.click(force=True)
        if not truthy and is_checked:
            try:
                await locator.uncheck(force=True)
            except Exception:
                await locator.click(force=True)

    async def _fill_radio(self, page, field, answer: str) -> None:
        options = await _radio_group(page, field)
        if not options:
            raise RuntimeError(f"Radio group not found for field: {field.label_text or field.name}")
        labels = [label for _radio, label in options]
        best = find_best_option_match(answer, labels) or answer
        for radio, label in options:
            if label == best or label.lower() == best.lower():
                try:
                    await radio.check(force=True)
                except Exception as exc:
                    log.debug("radio_check_failed_falling_back_to_click", field=field.label_text or field.name, error=str(exc))
                    await radio.click(force=True)
                return
        raise RuntimeError(f"Radio option not found for answer: {answer}")

    async def _await_pending_document_tasks(
        self,
        document_paths: dict[str, str | None],
        job_url: str | None,
    ) -> None:
        """Block until the resume / cover-letter generation task launched by
        the orchestrator finishes.

        The orchestrator runs document generation in parallel with the field
        fill so the UI feels responsive; this helper is the *barrier* that
        prevents us from clicking Submit while that task is still in flight.
        """
        doc_task = document_paths.get("_doc_task")
        if doc_task is None:
            return
        if doc_task.done():
            return
        await self._emit_progress(
            "awaiting_documents",
            "Form is filled — waiting for the resume to finish compiling",
            outcome="in_progress",
            job_url=job_url,
        )
        try:
            await doc_task
        except Exception as exc:
            log.warning("submit_gate_doc_task_failed", error=str(exc))

    async def _verify_documents_attached(
        self,
        form_page,
        adapter,
        document_paths: dict[str, str | None],
        job_url: str | None,
    ) -> None:
        """If the form has a file-input, confirm a file is attached.

        The user requirement is: "Submit only after the resume is attached".
        We re-enumerate the form fields and inspect every file input.  When a
        file input is required but no file is attached AND the resume PDF is
        on disk, we attach it automatically; otherwise we escalate to manual
        takeover so the user knows precisely why we're stuck.
        """
        try:
            fields = await adapter.enumerate_fields(form_page)
        except Exception as exc:
            log.debug("submit_gate_enumerate_failed", error=str(exc))
            return
        fields, _ = _filter_application_fields(fields)
        file_fields = [f for f in fields if _normalized_field_type(f) == "file" or _is_file_like_field(f)]
        if not file_fields:
            return

        for field in file_fields:
            label = (field.label_text or field.name or "file").lower()
            selector = getattr(field, "selector", None)
            if not selector:
                continue
            try:
                attached = await form_page.locator(selector).first.evaluate(
                    "(el) => !!(el && el.files && el.files.length > 0)"
                )
            except Exception as exc:
                log.debug("submit_gate_attached_probe_failed", selector=selector, error=str(exc))
                continue
            if attached:
                continue
            # Decide which document this field expects so we can re-attach.
            kind = "resume"
            if any(token in label for token in ("cover", "letter", "motivation")):
                kind = "cover_letter"
            doc_path = document_paths.get(f"{kind}_path")
            if not doc_path or not Path(doc_path).exists():
                # The doc generation either failed or hasn't produced a file.
                # Block submission with an actionable message.
                await self._emit_progress(
                    "submit_gate_blocked_no_document",
                    f"Required {kind.replace('_', ' ')} is missing — cannot submit",
                    outcome="blocked",
                    error_code=f"missing_{kind}",
                    job_url=job_url,
                )
                raise RuntimeError(
                    f"submit_gate_blocked: required {kind.replace('_', ' ')} document is not attached"
                )
            try:
                await self._emit_progress(
                    "reattaching_document",
                    f"Re-attaching {kind.replace('_', ' ')} before submit",
                    job_url=job_url,
                )
                await self._set_file_input(form_page, field, doc_path)
            except Exception as exc:
                log.warning("submit_gate_reattach_failed", kind=kind, error=str(exc))
                raise RuntimeError(
                    f"submit_gate_blocked: failed to re-attach {kind.replace('_', ' ')} ({exc})"
                )

    async def _ensure_resume_document(self, document_paths: dict[str, str | None], job_context: dict[str, Any]) -> str:
        resume_path = document_paths.get("resume_path")
        if resume_path and Path(resume_path).exists():
            return resume_path
        # Await concurrent doc-gen task launched by the orchestrator before
        # falling back to an on-demand regeneration.
        doc_task = document_paths.get("_doc_task")
        if doc_task is not None and not doc_task.done():
            log.info("resume_awaiting_background_doc_task")
            try:
                await doc_task
            except Exception:
                pass
        resume_path = document_paths.get("resume_path")
        if resume_path and Path(resume_path).exists():
            return resume_path
        out_dir = _document_output_dir(job_context)
        out_dir.mkdir(parents=True, exist_ok=True)
        builder = ResumeContextBuilder(self.app_state.encoder, self.app_state.generator)
        resume_context = await builder.build(
            str(job_context.get("jd") or "")
        )
        if job_context.get("company"):
            resume_context.setdefault("job_meta", {})["company"] = job_context["company"]
        if job_context.get("title"):
            resume_context.setdefault("job_meta", {})["role_title"] = job_context["title"]
        tex_path = ResumeAssembler(TEMPLATE_DIR / "resume" / "resume.tex.jinja").render_to_file(resume_context, out_dir / "resume.tex")
        try:
            resume_path = await self._compile_document(compile_latex, tex_path, out_dir, stage="resume_on_demand")
        except Exception as exc:
            fallback = create_fallback_artifact(out_dir, stem="resume", reason=f"resume_generation_failed: {exc}", reason_code="resume_generation_failed")
            resume_path = fallback["pdf_path"]
            document_paths["resume_fallback_reason_path"] = fallback["reason_path"]
            log.warning("resume_document_fallback_generated_on_demand", path=resume_path, job_url=job_context.get("job_url"), error=str(exc))
        document_paths["resume_path"] = resume_path
        log.info("resume_document_generated_on_demand", path=resume_path, job_url=job_context.get("job_url"))
        return resume_path

    async def _ensure_cover_letter_document(self, document_paths: dict[str, str | None], job_context: dict[str, Any]) -> str:
        cover_path = document_paths.get("cover_letter_path")
        if cover_path and Path(cover_path).exists():
            return cover_path
        doc_task = document_paths.get("_doc_task")
        if doc_task is not None and not doc_task.done():
            log.info("cover_letter_awaiting_background_doc_task")
            try:
                await doc_task
            except Exception:
                pass
        cover_path = document_paths.get("cover_letter_path")
        if cover_path and Path(cover_path).exists():
            return cover_path
        out_dir = _document_output_dir(job_context)
        out_dir.mkdir(parents=True, exist_ok=True)
        resume_context = await ResumeContextBuilder(self.app_state.encoder, self.app_state.generator).build(
            str(job_context.get("jd") or "")
        )
        if job_context.get("company"):
            resume_context.setdefault("job_meta", {})["company"] = job_context["company"]
        if job_context.get("title"):
            resume_context.setdefault("job_meta", {})["role_title"] = job_context["title"]
        job_meta = {
            "company": resume_context.get("job_meta", {}).get("company") or job_context.get("company") or "Hiring Team",
            "role_title": resume_context.get("job_meta", {}).get("role_title") or job_context.get("title") or "Software Engineer",
        }
        cover_payload = await CoverLetterWriter(self.app_state.generator).build(
            job_meta,
            _candidate_evidence_block_from_resume_context(resume_context),
            _earliest_start_date_from_candidate_data(self.candidate_data),
        )
        cover_context = {
            "sender": resume_context.get("personal", {}),
            "company_name": job_meta["company"],
            **cover_payload,
        }
        tex_path = CoverLetterAssembler().render_to_file(cover_context, out_dir / "cover.tex")
        try:
            cover_path = await self._compile_document(compile_cover_latex, tex_path, out_dir, stage="cover_letter_on_demand")
        except Exception as exc:
            fallback = create_fallback_artifact(out_dir, stem="cover-letter", reason=f"cover_letter_generation_failed: {exc}", reason_code="cover_letter_generation_failed")
            cover_path = fallback["pdf_path"]
            document_paths["cover_letter_fallback_reason_path"] = fallback["reason_path"]
            log.warning("cover_letter_fallback_generated_on_demand", path=cover_path, job_url=job_context.get("job_url"), error=str(exc))
        document_paths["cover_letter_path"] = cover_path
        log.info("cover_letter_generated_on_demand", path=cover_path, job_url=job_context.get("job_url"))
        return cover_path

    async def _set_file_input(self, page, field, file_path: str) -> None:
        locator = _field_locator(page, field)
        trigger = await _find_upload_trigger(page, field)
        if trigger is not None:
            try:
                async with page.expect_file_chooser() as chooser_info:
                    await trigger.click()
                chooser = await chooser_info.value
                await chooser.set_files(file_path)
                log.info("file_attached_via_file_chooser", field=field.label_text or field.name, path=file_path)
                return
            except Exception as exc:
                log.warning("file_chooser_upload_failed", field=field.label_text or field.name, error=str(exc))
        try:
            await locator.set_input_files(file_path)
            log.info("file_attached_via_input", field=field.label_text or field.name, path=file_path)
            return
        except Exception as exc:
            log.warning("set_input_files_failed", field=field.label_text or field.name, error=str(exc))
        raise RuntimeError(f"Upload trigger not found for field: {field.label_text or field.name}")


def _adapter_uses_browser_navigation(adapter) -> bool:
    name = adapter.__class__.__name__
    return name not in {"DoverAdapter"}


def _normalized_field_type(field) -> str:
    field_type = (getattr(field, "field_type", "") or "").lower()
    if field_type:
        return field_type
    return (getattr(field, "tag", "") or "").lower()


def _is_file_like_field(field: Any) -> bool:
    normalized_type = _normalized_field_type(field)
    if normalized_type == "file":
        return True
    label = _field_display_label(field).lower()
    if not label:
        return False
    if _looks_like_filename(label):
        return True
    upload_terms = (
        "upload resume",
        "upload cv",
        "attach resume",
        "attach cv",
        "resume",
        "curriculum vitae",
        "drop your resume",
        "choose file",
    )
    return any(term in label for term in upload_terms)


def _looks_like_filename(text: str) -> bool:
    normalized = text.strip().lower()
    return bool(re.search(r"\b[a-z0-9][a-z0-9._ -]*\.(pdf|docx?|rtf)\b", normalized))


def _order_fields_for_dependencies(fields: list[Any]) -> list[Any]:
    def key(field: Any) -> tuple[int, str]:
        label = ((getattr(field, "label_text", "") or "") + " " + (getattr(field, "name", "") or "")).lower()
        if "country" in label:
            return (0, label)
        if "state" in label or "province" in label:
            return (1, label)
        return (2, label)

    return sorted(fields, key=key)


def _deterministic_field_answer_override(field: Any, candidate_data: dict[str, Any]) -> str | None:
    field_text = " ".join(
        str(part or "")
        for part in (
            getattr(field, "label_text", ""),
            getattr(field, "name", ""),
            getattr(field, "element_id", ""),
            getattr(field, "selector", ""),
            getattr(field, "aria_label", ""),
            getattr(field, "placeholder", ""),
        )
    ).lower()
    options = [str(option) for option in (getattr(field, "options", None) or [])]
    if _looks_like_phone_country_code_field(field_text, options):
        dial_code = _candidate_phone_dial_code(candidate_data, options)
        return _match_dial_code_option(dial_code, options) if dial_code else None
    return None


def _looks_like_phone_country_code_field(field_text: str, options: list[str]) -> bool:
    if "country" not in field_text:
        return False
    phone_terms = ("phone", "telephone", "mobile", "cell", "tel", "dial", "calling")
    return any(term in field_text for term in phone_terms) or _options_look_like_dial_codes(options)


def _options_look_like_dial_codes(options: list[str]) -> bool:
    with_plus_code = sum(1 for option in options if re.search(r"\+\s*\d{1,4}\b", option))
    return with_plus_code >= max(1, min(3, len(options) // 8)) if options else False


def _match_dial_code_option(dial_code: str, options: list[str]) -> str:
    pattern = re.compile(rf"(^|[^\d])\+?\s*{re.escape(dial_code.lstrip('+'))}\b")
    for option in options:
        if pattern.search(option):
            return option
    return dial_code


def _candidate_phone_dial_code(candidate_data: dict[str, Any], options: list[str]) -> str | None:
    personal = candidate_data.get("candidate", {}).get("personal", {})
    phone = str(personal.get("phone", "") or "")
    country_code = _dial_code_for_country(
        str(personal.get("country") or personal.get("location_country") or personal.get("citizenship") or "")
    )
    if country_code:
        return country_code
    if not phone.startswith("+"):
        return None
    phone_digits = re.sub(r"\D+", "", phone)
    option_codes = sorted(
        {
            match.group(1)
            for option in options
            for match in re.finditer(r"\+\s*(\d{1,4})\b", str(option))
        },
        key=len,
        reverse=True,
    )
    for code in option_codes:
        if phone_digits.startswith(code):
            return f"+{code}"
    known_codes = sorted({code.lstrip("+") for code in _DIAL_CODES_BY_COUNTRY.values()}, key=len, reverse=True)
    for code in known_codes:
        if phone_digits.startswith(code):
            return f"+{code}"
    return None


_DIAL_CODES_BY_COUNTRY = {
    "india": "+91",
    "in": "+91",
    "united states": "+1",
    "united states of america": "+1",
    "usa": "+1",
    "us": "+1",
    "canada": "+1",
    "united arab emirates": "+971",
    "uae": "+971",
    "united kingdom": "+44",
    "uk": "+44",
    "great britain": "+44",
    "singapore": "+65",
    "germany": "+49",
    "france": "+33",
    "netherlands": "+31",
    "australia": "+61",
}


def _dial_code_for_country(country: str) -> str | None:
    normalized = re.sub(r"\s+", " ", country).strip().lower()
    if not normalized:
        return None
    return _DIAL_CODES_BY_COUNTRY.get(normalized)


def _format_phone_answer(answer: Any, placeholder: str, *, local_number: bool = False, dial_code: str | None = None) -> str:
    original = str(answer or "").strip()
    raw = re.sub(r"\D+", "", str(answer or ""))
    if local_number:
        dial_digits = re.sub(r"\D+", "", dial_code or "")
        if dial_digits and raw.startswith(dial_digits):
            local = raw[len(dial_digits):]
            if local:
                return local
        if len(raw) >= 10:
            return raw[-10:]
    if not placeholder:
        return f"+{raw}" if original.startswith("+") and raw else (raw or original)
    if placeholder == "(###) ###-####" and len(raw) >= 10:
        return f"({raw[-10:-7]}) {raw[-7:-4]}-{raw[-4:]}"
    if placeholder == "+#-###-###-####" and len(raw) >= 11:
        return f"+{raw[-11]}-{raw[-10:-7]}-{raw[-7:-4]}-{raw[-4:]}"
    if placeholder == "##########" and len(raw) >= 10:
        return raw[-10:]
    return original


async def _selected_phone_country_code(locator, answer: Any) -> str | None:
    phone_digits = re.sub(r"\D+", "", str(answer or ""))
    if not phone_digits:
        return None
    try:
        result = await locator.evaluate(
            """(el, phoneDigits) => {
                    const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                    const matchingCode = (value) => {
                        const codes = Array.from(clean(value).matchAll(/\\+\\s*(\\d{1,4})\\b/g))
                            .map((match) => match[1])
                            .filter((code) => phoneDigits.startsWith(code))
                            .sort((a, b) => b.length - a.length);
                        return codes[0] || null;
                    };
                    const hasAnyDialCode = (value) => /\\+\\s*\\d{1,4}\\b/.test(clean(value));
                    const controlText = (control) => {
                        const selectedOption = control.matches('select')
                            ? control.options?.[control.selectedIndex]?.text
                            : '';
                        const selectedValue = control.querySelector?.('[class*="single-value"], [class*="multi-value"], [class*="value-container"]');
                        return clean(selectedOption || selectedValue?.innerText || selectedValue?.textContent || control.innerText || control.textContent || control.value || control.getAttribute('aria-label') || '');
                    };
                    let node = el;
                    for (let depth = 0; depth < 5 && node; depth += 1, node = node.parentElement) {
                        const text = clean(node.innerText || node.textContent || '');
                        const code = matchingCode(text);
                        if (/\\bcountry\\b/i.test(text) && /\\bphone\\b/i.test(text) && code) {
                            return `+${code}`;
                        }
                    }
                    const form = el.closest('form');
                    if (!form) return null;
                    const phoneBox = el.getBoundingClientRect();
                    const controls = Array.from(form.querySelectorAll('[role="combobox"], select, [aria-haspopup="listbox"]'));
                    for (const control of controls) {
                        const rect = control.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) continue;
                        if (rect.bottom < phoneBox.top - 160 || rect.top > phoneBox.bottom + 80) continue;
                        const wrapper = control.closest('label, fieldset, section, div');
                        const text = clean(`${wrapper && (wrapper.innerText || wrapper.textContent) || ''} ${controlText(control)}`);
                        const code = matchingCode(text);
                        if (/\\bcountry\\b/i.test(text) && code && hasAnyDialCode(text)) {
                            return `+${code}`;
                        }
                    }
                    return null;
                }""",
            phone_digits,
        )
        return str(result) if result else None
    except Exception as exc:
        log.debug("phone_country_code_detection_failed", error=str(exc))
        return None


async def _find_disabled_submit_button(page):
    """Return the first disabled submit-like button, or None. Used to detect
    CAPTCHA-disabled submit on Workable and similar SPAs."""
    from backend.form.navigator import SUBMIT_BUTTON_SELECTORS, _element_text, _element_visible
    _SUBMIT_KEYWORDS = (
        "submit application", "submit", "apply now", "apply", "send application",
        "complete application",
    )
    for selector in SUBMIT_BUTTON_SELECTORS + ["button", "[role='button']"]:
        try:
            elements = await page.query_selector_all(selector)
        except Exception:
            continue
        for element in elements:
            try:
                if not await element.is_visible():
                    continue
                if not await element.is_disabled():
                    continue
                text = (await _element_text(element)).lower()
                if any(kw in text for kw in _SUBMIT_KEYWORDS):
                    return element
            except Exception:
                continue
    return None


async def _captcha_detected(page) -> bool:
    visible_selectors = [
        "iframe[src*='recaptcha']",
        "iframe[src*='hcaptcha']",
        "iframe[src*='cloudflare']",
        "iframe[src*='challenges.cloudflare.com']",
        # Cloudflare Turnstile — widget container and hidden response field
        ".cf-turnstile",
        "[class*='cf-turnstile']",
        "[data-sitekey]",
        "input[name='cf-turnstile-response']",
        "[id*='cf-chl']",
        "[class*='captcha']",
    ]
    for selector in visible_selectors:
        try:
            element = await page.query_selector(selector)
            if element is not None and await element.is_visible():
                return True
        except Exception as exc:
            log.debug("captcha_detection_selector_failed", selector=selector, error=str(exc))
            continue

    # Cloudflare Turnstile in *managed* mode (the configuration used by
    # apply.workable.com) renders the widget invisibly when the underlying
    # bot-management challenge passes silently.  The widget container stays
    # in the DOM either way, but the hidden ``cf-turnstile-response`` input
    # only carries a token when verification succeeded.  When the widget is
    # present without a token, treat the page as gated by a CAPTCHA so the
    # filler escalates to manual takeover instead of clicking a submit
    # button that will silently fail with "Something went wrong".
    try:
        widget_present_no_token = await page.evaluate(
            """() => {
                const widget = document.querySelector(
                    "[data-sitekey], .cf-turnstile, [class*='cf-turnstile']"
                );
                if (!widget) return false;
                const tokenInput = document.querySelector("input[name='cf-turnstile-response']");
                const token = tokenInput ? tokenInput.value : '';
                return !token;
            }"""
        )
        if widget_present_no_token:
            return True
    except Exception as exc:
        log.debug("captcha_turnstile_token_probe_failed", error=str(exc))

    return False


async def _try_continue_past_transient_captcha(page) -> bool:
    """Click one non-submit Continue/Next-style control for dummy CAPTCHA pages.

    Some job boards briefly render a challenge shell that disappears if the user
    continues. We only click buttons discovered by the "next" heuristics here,
    never submit/apply buttons, so a real final submit cannot be triggered by
    CAPTCHA recovery.
    """
    try:
        button = await find_next_button(page)
        if button is None:
            return False
        await button.scroll_into_view_if_needed()
        await asyncio.sleep(random.uniform(config.BROWSER_HUMAN_DELAY_MIN_SECONDS, config.BROWSER_HUMAN_DELAY_MAX_SECONDS))
        await button.click()
        return True
    except Exception as exc:
        log.debug("captcha_continue_once_failed", error=str(exc))
        return False


def _filter_application_fields(fields: list[Any]) -> tuple[list[Any], int]:
    filtered: list[Any] = []
    skipped = 0
    for form_field in fields:
        label = _field_display_label(form_field)
        if not label:
            skipped += 1
            continue
        lower = label.lower()
        if len(label) > 300 and _looks_like_disclaimer(label):
            skipped += 1
            continue
        if any(keyword in lower for keyword in NOISE_FIELD_KEYWORDS):
            # Exception: Required checkboxes for terms/privacy must not be skipped, otherwise submit remains disabled.
            is_checkbox = getattr(form_field, "field_type", "") in ("checkbox", "radio")
            is_agreement = any(k in lower for k in ("terms", "privacy", "policy", "gdpr", "consent", "agree"))
            if is_checkbox and is_agreement:
                filtered.append(form_field)
            else:
                skipped += 1
            continue
        filtered.append(form_field)
    return filtered, skipped


def _field_display_label(field: Any) -> str:
    return " ".join(
        str(part).strip()
        for part in [
            getattr(field, "label_text", "") or "",
            getattr(field, "aria_label", "") or "",
            getattr(field, "placeholder", "") or "",
            getattr(field, "name", "") or "",
        ]
        if str(part).strip()
    ).strip()


def _looks_like_disclaimer(text: str) -> bool:
    lower = text.lower()
    return sum(term in lower for term in ("privacy", "cookie", "consent", "gdpr", "terms", "policy", "legal", "notice")) >= 2


def _field_key(field: Any) -> str:
    return getattr(field, "name", None) or getattr(field, "label_text", "") or ""


def _field_answer_record(field: Any, value: Any) -> dict[str, Any]:
    return {
        "key": _field_key(field),
        "label": getattr(field, "label_text", None),
        "value": "" if value is None else str(value),
        "required": bool(getattr(field, "required", False)),
        "field_type": getattr(field, "field_type", None),
    }


def _field_answer_value(items: list[dict[str, Any]], field: Any) -> str:
    key = _field_key(field)
    for item in items:
        if item.get("key") == key:
            return str(item.get("value") or "")
    return ""


def _upsert_field_answer(items: list[dict[str, Any]], field: Any, value: Any) -> None:
    key = _field_key(field)
    for item in items:
        if item.get("key") == key:
            item["value"] = "" if value is None else str(value)
            return
    items.append(_field_answer_record(field, value))


def _merge_field_answers(existing: list[dict[str, Any]], new_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = {str(item.get("key") or item.get("label")): dict(item) for item in existing}
    for item in new_items:
        merged[str(item.get("key") or item.get("label"))] = dict(item)
    return list(merged.values())


def _interaction_delay_disabled() -> bool:
    return bool(config.BROWSER_TEST_MODE or os.environ.get("PYTEST_CURRENT_TEST"))


def _qa_dry_run_decline_can_continue(approval_details: dict[str, Any]) -> bool:
    if not config.runtime_settings().dry_run or not bool(getattr(config, "DEFERRED_QUESTIONS_MODE", False)):
        return False
    if approval_details.get("missing_required_fields"):
        return False
    warnings = approval_details.get("validation_warnings") or []
    return not any(str(item.get("level", "")).lower() == "error" for item in warnings if isinstance(item, dict))


async def _post_field_pause() -> None:
    if _interaction_delay_disabled():
        return
    low = min(config.BROWSER_HUMAN_DELAY_MIN_SECONDS, 0.08)
    high = min(max(config.BROWSER_HUMAN_DELAY_MAX_SECONDS, low), 0.25)
    if high <= 0:
        return
    await asyncio.sleep(random.uniform(low, high))


async def _locator_contains_value(locator, expected: str) -> bool:
    expected = str(expected or "")
    try:
        actual = await locator.input_value()
    except Exception:
        try:
            actual = await locator.inner_text()
        except Exception:
            return False
    return str(actual or "").strip() == expected.strip()


def _safe_slug(value: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in value)[:80].strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "job"


async def verify_field_filled(page, field: Any, expected_value: Any, field_type: str) -> bool:
    selector = getattr(field, "selector", None)
    if not selector:
        return True
    locator = page.locator(selector).first
    try:
        if _is_custom_combobox_field(field):
            try:
                actual = await locator.input_value()
            except Exception:
                actual = ""
            if (actual or "").strip():
                return True
            visible_text = await _combobox_selected_text(locator)
            expected_text = str(expected_value or "").strip().lower()
            visible_lower = visible_text.lower()
            if expected_text and (expected_text in visible_lower or visible_lower in expected_text):
                return True
            if not visible_text.strip() or visible_text.strip() in ("Select...", "-- Select --", ""):
                log.warning("combobox_selection_may_have_failed", field=field.label_text or field.name)
                return False
            return True
        if field_type in ("text", "email", "tel", "number", "textarea", "url", "date"):
            actual = await locator.input_value()
            return bool((actual or "").strip())
        if field_type in ("select", "dropdown"):
            selected = await locator.evaluate("el => el.options?.[el.selectedIndex]?.text || ''")
            return bool(selected and selected not in {"Select", "-- Select --"})
        if field_type == "checkbox":
            await locator.is_checked()
            return True
        if field_type == "file":
            actual = await locator.input_value()
            return bool(actual)
    except Exception as exc:
        log.warning("verify_field_filled_failed", field=field.label_text or field.name, error=str(exc))
        return False
    return True


def _field_locator(page, field):
    selector = getattr(field, "selector", None)
    if not selector:
        raise RuntimeError(f"Field selector missing for {getattr(field, 'label_text', None) or getattr(field, 'name', None)}")
    return page.locator(selector).first


def _is_custom_combobox_field(field: Any) -> bool:
    role = (getattr(field, "role", "") or "").lower()
    tag = (getattr(field, "tag", "") or "").lower()
    field_type = (getattr(field, "field_type", "") or "").lower()
    return role in {"combobox", "listbox"} or (field_type == "select" and tag != "select")


def _checkbox_desired_answers(answer: Any) -> list[str]:
    if answer is None:
        return []
    if isinstance(answer, (list, tuple, set)):
        values = [str(item) for item in answer]
    else:
        raw = str(answer).strip()
        if not raw:
            return []
        values = [part.strip() for part in re.split(r"[,;/]|\band\b|\n", raw, flags=re.IGNORECASE)]
    return [value for value in values if value]


async def _close_open_combobox_menus(page) -> None:
    try:
        await page.keyboard.press("Escape")
    except Exception as exc:
        log.debug("combobox_menu_close_failed", error=str(exc))
        return
    if not _interaction_delay_disabled():
        try:
            await page.wait_for_timeout(50)
        except Exception:
            pass


async def _combobox_option_scope_selector(locator) -> str | None:
    try:
        field_id = await locator.get_attribute("id")
        aria_controls = await locator.get_attribute("aria-controls")
        aria_owns = await locator.get_attribute("aria-owns")
    except Exception as exc:
        log.debug("combobox_scope_attribute_failed", error=str(exc))
        return None
    candidates = []
    for raw in (aria_controls, aria_owns):
        if raw:
            candidates.extend(part for part in raw.split() if part)
    if field_id:
        candidates.append(f"react-select-{field_id}-listbox")
    for candidate in candidates:
        if candidate:
            return f'[id="{_css_attr_escape(candidate)}"]'
    return None


def _css_attr_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


async def _combobox_selected_text(locator) -> str:
    try:
        text = await locator.evaluate(
            """(el) => {
                const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                const control = el.closest('.select__control, [class*="control"]');
                const single = control?.querySelector('[class*="single-value"]');
                const multi = control?.querySelector('[class*="multi-value"]');
                const valueContainer = control?.querySelector('[class*="value-container"]');
                return clean(single?.innerText || multi?.innerText || valueContainer?.innerText || control?.innerText || el.value || el.textContent);
            }"""
        )
    except Exception as exc:
        log.debug("combobox_selected_text_failed", error=str(exc))
        text = ""
    return " ".join(str(text or "").split())


async def _best_visible_combobox_option(page, value: str, scope_selector: str | None = None):
    value_lower = (value or "").strip().lower()
    selector = "[role='option'], li[class*='option'], div[class*='option']"
    if scope_selector:
        selector = f"{scope_selector} [role='option'], {scope_selector} li[class*='option'], {scope_selector} div[class*='option']"
    options = await page.query_selector_all(selector)
    first_visible = None
    visible_options: list[tuple[Any, str, str]] = []
    for option in options:
        try:
            if not await option.is_visible():
                continue
            if first_visible is None:
                first_visible = option
            option_text = " ".join((await option.inner_text()).split())
        except Exception as exc:
            log.debug("combobox_option_text_read_failed", error=str(exc))
            continue
        visible_options.append((option, option_text, option_text.lower()))
    if value_lower.startswith("+"):
        pattern = re.compile(rf"(^|[^\d])\+?\s*{re.escape(value_lower.lstrip('+'))}\b")
        for option, _, option_lower in visible_options:
            if pattern.search(option_lower):
                return option
    for option, _, option_lower in visible_options:
        if value_lower and option_lower.strip() == value_lower:
            return option
    if value_lower and re.fullmatch(r"[a-z0-9 ]+", value_lower):
        word_pattern = re.compile(rf"(^|\W){re.escape(value_lower)}(\W|$)")
        for option, _, option_lower in visible_options:
            if word_pattern.search(option_lower):
                return option
    for option, _, option_lower in visible_options:
        if value_lower and (value_lower in option_lower or option_lower in value_lower):
            return option
    return first_visible


async def _radio_group(page, field) -> list[tuple[Any, str]]:
    name = getattr(field, "name", None)
    radios = []
    if name:
        radios = await page.query_selector_all(f"input[type='radio'][name='{name}']")
    if not radios and getattr(field, "selector", None):
        locator = page.locator(field.selector).first
        try:
            fieldset = await locator.evaluate_handle("el => el.closest('fieldset')")
            if fieldset:
                radios = await fieldset.query_selector_all("input[type='radio']")
        except Exception as exc:
            log.debug("radio_fieldset_fallback_failed", error=str(exc))
    options: list[tuple[Any, str]] = []
    for radio in radios:
        options.append((radio, await _label_for_input(page, radio)))
    return options


async def _checkbox_group(page, field) -> list[tuple[Any, str]]:
    name = getattr(field, "name", None)
    checkboxes = []
    if name:
        checkboxes = await page.query_selector_all(f"input[type='checkbox'][name='{name}']")
    if not checkboxes and getattr(field, "selector", None):
        try:
            locator = page.locator(field.selector).first
            fieldset = await locator.evaluate_handle("el => el.closest('fieldset')")
            if fieldset:
                checkboxes = await fieldset.query_selector_all("input[type='checkbox']")
        except Exception as exc:
            log.debug("checkbox_fieldset_fallback_failed", error=str(exc))
    return [(box, await _label_for_input(page, box)) for box in checkboxes]


async def _label_for_input(page, input_element) -> str:
    try:
        element_id = await input_element.get_attribute("id")
    except Exception:
        element_id = None
    if element_id:
        label = await page.query_selector(f"label[for='{element_id}']")
        if label is not None:
            return " ".join(((await label.inner_text()) or "").split())
    try:
        wrapper = await input_element.evaluate_handle("el => el.closest('label')")
        if wrapper:
            text = await wrapper.evaluate("(el) => (el.innerText || el.textContent || '').trim()")
            if text:
                return " ".join(str(text).split())
    except Exception as exc:
        log.debug("label_for_input_wrapping_label_failed", error=str(exc))
    try:
        text = await input_element.evaluate(
            """(el) => {
                const next = el.nextSibling;
                return next && next.textContent ? next.textContent.trim() : '';
            }"""
        )
        if text:
            return " ".join(str(text).split())
    except Exception as exc:
        log.debug("label_for_input_sibling_text_failed", error=str(exc))
    return str((await input_element.get_attribute("value")) or "")


def _document_output_dir(job_context: dict[str, Any]) -> Path:
    artifact_dir = job_context.get("artifact_dir")
    if artifact_dir:
        return Path(str(artifact_dir))
    base = job_context.get("job_url") or job_context.get("title") or "job"
    state_slug = _safe_slug(str(base))
    return config.OUTPUT_DIR / "on_demand" / state_slug


def _to_mmddyyyy(value: str) -> str:
    parts = str(value or "").split("-")
    if len(parts) >= 3:
        return f"{parts[1]}/{parts[2]}/{parts[0]}"
    return str(value or "")


async def _find_upload_trigger(page, field):
    label = (getattr(field, "label_text", "") or "").lower()
    selectors = [
        "button:has-text('Upload')",
        "button:has-text('Choose')",
        "button:has-text('Browse')",
        "button:has-text('PDF')",
        "button:has-text('.pdf')",
        "label:has-text('Upload')",
        "label:has-text('Choose')",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count():
                text = " ".join((await locator.inner_text()).split()).lower()
                if not label or any(token in text for token in label.split()[:2]):
                    return locator
        except Exception as exc:
            log.debug("upload_trigger_lookup_failed", selector=selector, error=str(exc))
            continue
    return None


def _candidate_evidence_block_from_resume_context(resume_context: dict[str, Any]) -> str:
    lines: list[str] = []
    for project in resume_context.get("projects_top3", []) or []:
        if not isinstance(project, dict):
            continue
        name = str(project.get("name") or "Project").strip()
        summary = str(project.get("one_line_summary") or "").strip()
        stack = ", ".join(str(item).strip() for item in (project.get("tech_stack") or []) if str(item).strip())
        bullet = f"- {name}: {summary}" if summary else f"- {name}"
        if stack:
            bullet += f" (stack: {stack})"
        lines.append(bullet)
    return "\n".join(lines)


def _earliest_start_date_from_candidate_data(candidate_data: dict[str, Any]) -> str:
    ground_truth = candidate_data.get("ground_truth", {}) or {}
    preferences = ground_truth.get("preferences", {}) or {}
    return str(
        preferences.get("earliest_start_date")
        or preferences.get("notice_period")
        or "Immediately"
    )


def _build_pre_submit_audit(fields: list[Any], field_answers: list[dict[str, Any]]) -> dict[str, Any]:
    answer_map = {str(item.get("key") or item.get("label") or ""): str(item.get("value") or "").strip() for item in field_answers}
    required_fields_missing: list[str] = []
    unresolved_critical_questions: list[str] = []
    file_upload_missing: list[str] = []
    for form_field in fields:
        if not getattr(form_field, "required", False):
            continue
        label = form_field.label_text or form_field.name or "Unnamed field"
        value = answer_map.get(_field_key(form_field), "")
        field_type = _normalized_field_type(form_field)
        if not value:
            required_fields_missing.append(label)
        if (field_type == "file" or _is_file_like_field(form_field)) and not value:
            file_upload_missing.append(label)
        if field_type in {"text", "textarea"} and not value:
            unresolved_critical_questions.append(label)
    return {
        "blocked": bool(required_fields_missing or unresolved_critical_questions or file_upload_missing),
        "required_fields_missing": sorted(set(required_fields_missing)),
        "unresolved_critical_questions": sorted(set(unresolved_critical_questions)),
        "file_upload_missing": sorted(set(file_upload_missing)),
        "invalid_fields": [],
        "field_validation_messages": {},
    }


async def _augment_pre_submit_audit_from_dom(page, fields: list[Any], audit: dict[str, Any]) -> None:
    dom_missing: list[str] = []
    invalid_fields: list[str] = []
    validation_messages: dict[str, str] = {}
    for form_field in fields:
        selector = getattr(form_field, "selector", None)
        if not selector:
            continue
        label = form_field.label_text or form_field.name or "Unnamed field"
        field_type = _normalized_field_type(form_field)
        locator = page.locator(selector).first
        try:
            if _is_custom_combobox_field(form_field):
                value = await _combobox_selected_text(locator)
                is_missing = bool(getattr(form_field, "required", False)) and not value.strip()
                valid = not is_missing
                message = "Required selection missing" if is_missing else ""
            elif field_type == "checkbox":
                group = await _checkbox_group(page, form_field)
                if len(group) > 1:
                    checked_any = False
                    for checkbox, _checkbox_label in group:
                        try:
                            checked_any = checked_any or await checkbox.is_checked()
                        except Exception:
                            continue
                    is_missing = bool(getattr(form_field, "required", False)) and not checked_any
                    valid = not is_missing
                    message = "Required checkbox selection missing" if is_missing else ""
                else:
                    status = await locator.evaluate(
                        """(el) => ({
                            value: el.checked ? 'checked' : '',
                            valid: el.validity ? Boolean(el.validity.valid) : true,
                            message: el.validationMessage || '',
                        })"""
                    )
                    value = str(status.get("value") or "")
                    valid = bool(status.get("valid", True))
                    message = str(status.get("message") or "")
                    is_missing = bool(getattr(form_field, "required", False)) and not value
            elif field_type == "radio":
                group = await _radio_group(page, form_field)
                checked_any = False
                for radio, _radio_label in group:
                    try:
                        checked_any = checked_any or await radio.is_checked()
                    except Exception:
                        continue
                is_missing = bool(getattr(form_field, "required", False)) and not checked_any
                valid = not is_missing
                message = "Required radio selection missing" if is_missing else ""
            else:
                status = await locator.evaluate(
                    """(el) => {
                        const type = (el.type || '').toLowerCase();
                        const tag = (el.tagName || '').toLowerCase();
                        let value = '';
                        if (type === 'checkbox' || type === 'radio') value = el.checked ? 'checked' : '';
                        else if (type === 'file') value = el.files && el.files.length ? 'attached' : '';
                        else if (el.isContentEditable) value = el.innerText || el.textContent || '';
                        else if ('value' in el) value = el.value || '';
                        else value = el.innerText || el.textContent || '';
                        const validity = el.validity;
                        return {
                            value: String(value || '').trim(),
                            valid: validity ? Boolean(validity.valid) : true,
                            message: el.validationMessage || '',
                            type,
                            tag,
                        };
                    }"""
                )
                value = str(status.get("value") or "")
                valid = bool(status.get("valid", True))
                message = str(status.get("message") or "")
                is_missing = bool(getattr(form_field, "required", False)) and not value
            if is_missing:
                dom_missing.append(label)
            if not valid:
                invalid_fields.append(label)
                if message:
                    validation_messages[label] = message
        except Exception as exc:
            log.debug("pre_submit_dom_audit_field_failed", field=label, error=str(exc))
    if dom_missing:
        audit["required_fields_missing"] = sorted(set([*audit.get("required_fields_missing", []), *dom_missing]))
    if invalid_fields:
        audit["invalid_fields"] = sorted(set([*audit.get("invalid_fields", []), *invalid_fields]))
    if validation_messages:
        audit["field_validation_messages"] = {**audit.get("field_validation_messages", {}), **validation_messages}
    audit["blocked"] = bool(
        audit.get("required_fields_missing")
        or audit.get("unresolved_critical_questions")
        or audit.get("file_upload_missing")
        or audit.get("invalid_fields")
    )


def _build_debug_snapshot(fields: list[Any], field_answers: list[dict[str, Any]]) -> dict[str, Any]:
    answer_map = {str(item.get("key") or item.get("label") or ""): str(item.get("value") or "").strip() for item in field_answers}
    return {
        "final_visible_required_fields": [field.label_text or field.name or "Unnamed field" for field in fields if getattr(field, "required", False)],
        "unresolved_labels": [field.label_text or field.name or "Unnamed field" for field in fields if not answer_map.get(_field_key(field), "")],
        "selector_value_verification_failures": [field.label_text or field.name or "Unnamed field" for field in fields if not getattr(field, "selector", None)],
    }
