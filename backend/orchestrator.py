from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import structlog

from backend import config
from backend.artifacts import create_fallback_artifact
from backend.config import CLASSIFIER_THRESHOLD, OUTPUT_DIR, ROOT_DIR
from backend.contracts import ListingLifecycle
from backend.retry import TransientJobError, run_with_retry
from backend.cover_letter.assembler import CoverLetterAssembler
from backend.cover_letter.compiler import compile_latex as compile_cover_latex
from backend.cover_letter.detector import cover_letter_requested
from backend.cover_letter.writer import CoverLetterWriter
from backend.form.filler import FormFiller
from backend.models.classifier_feedback import ClassifierFeedbackStore
from backend.resume.assembler import ResumeAssembler
from backend.resume.builder import ResumeContextBuilder
from backend.resume.compiler import compile_latex
from backend.resume.ats_scorer import score_resume_pdf
from backend.resume.form_detector import resume_requested
from backend.scraping.adapters import dispatch_adapter
from backend.specialists.fit_decision import (
    assess_parsing_coverage,
    decide_fit,
    load_candidate_fit_facts,
    proposed_profile_diff,
)
from backend.specialists.liveness_detector import classify_liveness_text
from backend.storage.candidate_profile import PROFILE_PATH
from backend.storage.ground_truth import GROUND_TRUTH_PATH, GroundTruthStore

log = structlog.get_logger()


def _latex_retryable(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(token in text for token in ("temporar", "timeout", "busy", "locked", "resource unavailable"))


@dataclass
class RunState:
    state: str = "idle"
    run_id: int | None = None
    correlation_id: str | None = None
    current_job: str | None = None
    current_stage: str | None = None
    current_stage_message: str | None = None
    last_failure: dict[str, Any] | None = None
    today: int = 0
    week: int = 0
    all_time: int = 0
    last_10: list[dict[str, Any]] | None = None


class Orchestrator:
    def __init__(self, app_state: Any):
        self.app_state = app_state
        self._task: asyncio.Task | None = None
        self._stop_requested = False
        self.state = RunState(last_10=[])
        self._skip_reasons: dict[str, int] = {}
        self._outcome_counters: dict[str, int] = {"soft_fail": 0, "hard_fail": 0}

    async def start(
        self,
        career_url: str,
        limit: int | None = None,
        correlation_id: str | None = None,
        *,
        force_reprocess: bool = False,
        bypass_classifier: bool = False,
    ) -> int:
        if self._task and not self._task.done():
            raise RuntimeError("run already active")
        domain = urlparse(career_url).hostname or "unknown"
        if limit is not None:
            self.app_state.sqlite.upsert_site_limit(domain, limit)
        if force_reprocess:
            mode = _current_attempt_mode()
            successful = False
            if hasattr(self.app_state.sqlite, "successful_attempt_mode"):
                successful = self.app_state.sqlite.successful_attempt_mode(career_url, mode)
            if not successful and mode == "real_submit":
                existing = self._existing_application(career_url)
                successful = bool(existing and existing.get("submitted"))
            if successful:
                label = "Dry Run" if mode == "dry_run" else "Real Submit"
                raise RuntimeError(f"{label} already completed for this job. Green history entries are locked for the same mode.")
        run_id = self.app_state.sqlite.create_run(career_url)
        self._stop_requested = False
        self.state.state = "running"
        self.state.run_id = run_id
        self.state.correlation_id = correlation_id or str(uuid4())
        self.state.last_failure = None
        self.state.current_stage = None
        self.state.current_stage_message = None
        self.state.current_job = None
        self._task = asyncio.create_task(
            self._run(
                run_id,
                career_url,
                limit=limit,
                force_reprocess=force_reprocess,
                bypass_classifier=bypass_classifier,
            )
        )
        # Ensure the task handle is cleared once the run finishes so subsequent
        # start() calls succeed without having to check `.done()` manually.
        self._task.add_done_callback(self._on_task_done)
        return run_id

    def _on_task_done(self, task: asyncio.Task) -> None:
        # Swallow any unhandled exceptions so the event loop doesn't complain;
        # _run already logs & surfaces errors through the stream.
        try:
            task.result()
        except BaseException:
            pass
        if self._task is task:
            self._task = None

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def stop(self) -> None:
        self._stop_requested = True
        self.state.state = "stopping"
        # If there's no active task there's nothing to wait for.
        task = self._task
        if task is None or task.done():
            self.state.state = "idle"
            return
        # Give the cooperative stop flag a few seconds to take effect.
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        finally:
            if self.state.state != "error":
                self.state.state = "stopped"

    async def _stage(self, stage: str, message: str, **extra: Any) -> None:
        """Set current stage and emit a progress event so the UI shows exactly what's happening."""
        self.state.current_stage = stage
        self.state.current_stage_message = message
        log.info("run_stage", stage=stage, message=message, **extra)
        payload = self._event_envelope(
            "stage_progress",
            stage=stage,
            message=message,
            outcome="in_progress",
            **extra,
        )
        await self.app_state.stream.publish("progress", payload)

    async def _show_live_browser(self, page: Any) -> None:
        if not config.live_mode_enabled():
            return
        try:
            await page.bring_to_front()
        except Exception:
            pass

    def status(self, correlation_id: str | None = None) -> dict[str, Any]:
        if not self.running():
            if self.state.state in {"running", "starting", "stopping"}:
                self.state.state = "idle"
            self.state.current_job = None
            self.state.current_stage = None
            self.state.current_stage_message = None
            try:
                self.app_state.sqlite.reconcile_orphan_runs()
            except Exception:
                pass
        mongo = getattr(self.app_state, "mongo", None)
        if mongo is not None and getattr(mongo, "enabled", lambda: False)():
            counts = mongo.application_counts()
            recent = mongo.list_applications(10)
        else:
            counts = self.app_state.sqlite.application_counts()
            recent = self.app_state.sqlite.last_applications(10)
        self.state.today = counts["today"]
        self.state.week = counts["week"]
        self.state.all_time = counts["all_time"]
        self.state.last_10 = recent
        payload = asdict(self.state)
        payload["correlation_id"] = correlation_id or self.state.correlation_id
        payload["resume_summary"] = self._resume_summary()
        if payload.get("last_failure") and isinstance(payload["last_failure"], dict):
            payload["last_failure"] = {k: str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v for k, v in payload["last_failure"].items()}
        return payload

    def _resume_summary(self) -> dict[str, int]:
        if self.state.run_id is None or not hasattr(self.app_state.sqlite, "list_resume_candidates"):
            return {"pending": 0, "retry_eligible": 0}
        items = self.app_state.sqlite.list_resume_candidates(self.state.run_id)
        pending = sum(1 for item in items if item.get("state") == ListingLifecycle.PENDING.value)
        retry_eligible = sum(1 for item in items if item.get("state") == ListingLifecycle.FAILED_TRANSIENT.value)
        return {"pending": pending, "retry_eligible": retry_eligible}

    def _failure_envelope(self, stage: str, exc: Exception, *, category: str = "unknown_failure", retryable: bool = False, hint: str | None = None) -> dict[str, Any]:
        return self._event_envelope(
            "error",
            stage=stage,
            message=str(exc) or exc.__class__.__name__,
            outcome="failed",
            error_code=category,
            error_type=exc.__class__.__name__,
            retryable=retryable,
            recovery_hint=hint or "Inspect failure artifacts and rerun in dry-run mode.",
        )

    def _event_envelope(self, event_type: str, *, stage: str | None = None, message: str | None = None, outcome: str | None = None, error_code: str | None = None, **extra: Any) -> dict[str, Any]:
        return {
            "type": event_type,
            "event_type": event_type,
            "run_id": self.state.run_id,
            "correlation_id": self.state.correlation_id,
            "stage": stage,
            "message": message,
            "outcome": outcome,
            "error_code": error_code,
            "current_job": self.state.current_job,
            "state": self.state.state,
            **extra,
        }

    async def _publish_failure(self, stage: str, exc: Exception, *, category: str, retryable: bool = False, hint: str | None = None) -> dict[str, Any]:
        envelope = self._failure_envelope(stage, exc, category=category, retryable=retryable, hint=hint)
        self.state.last_failure = envelope
        await self.app_state.stream.publish("error", envelope)
        return envelope

    async def _run(
        self,
        run_id: int,
        career_url: str,
        *,
        limit: int | None = None,
        force_reprocess: bool = False,
        bypass_classifier: bool = False,
    ) -> None:
        browser = self.app_state.browser
        jobs_passed = 0
        jobs_applied = 0
        page = None
        try:
            await self._stage("opening_browser", "Opening browser")
            try:
                page = await asyncio.wait_for(browser.new_page(), timeout=30.0)
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    "Browser startup timed out after 30 seconds. Close any stuck Chrome/Chromium windows using the JobPilot profile and try again."
                ) from exc
            await self._show_live_browser(page)
            # Make the page available for live-browser takeover (AlarmNotifier reads focused fields from this page).
            try:
                self.app_state.alarm.active_page = page
            except Exception:
                pass
            adapter = dispatch_adapter(career_url)
            await self._stage("listing_jobs", f"Listing jobs on {urlparse(career_url).hostname or 'career page'}")
            listings = await self._retry_stage(
                "listing_jobs",
                lambda: adapter.list_jobs(page, career_url),
                category="adapter_list_failed",
                hint="Verify the careers URL still exposes a listings page and the adapter selectors still match.",
            )
            await self._stage("listed_jobs", f"Found {len(listings)} job listings")
            self.app_state.sqlite.update_run(run_id, jobs_seen=len(listings), status="running")
            if not listings:
                # A 0-listing return typically means the user pasted a URL that isn't a careers
                # index (e.g. a single-job listing page, a company homepage, or a login wall).
                # Staying silent in this state looked identical to "running" and was the single
                # worst UX bug on JobPilot — users stared at an idle dashboard for hours. Fail
                # loudly so they know exactly what to fix.
                hint = (
                    "The page returned zero job links. This usually means the URL isn't a careers "
                    "listings page — e.g. you pasted a single job posting, a company homepage, or "
                    "a login-gated page. Try the top-level 'Careers' or 'Open Positions' URL."
                )
                await self.app_state.stream.publish(
                    "progress",
                    self._event_envelope("empty_listings", stage="listing_jobs", message=hint, outcome="failed", error_code="empty_listings", career_url=career_url, hint=hint),
                )
                await self._stage("no_jobs_found", "No jobs found on the provided career page.")
                self._skip_reasons["empty_listings"] = self._skip_reasons.get("empty_listings", 0) + 1
                no_jobs_dir = OUTPUT_DIR / f"run_{run_id}" / "no-jobs-found"
                no_jobs_listing = SimpleNamespace(
                    url=career_url,
                    ext_id="career-page",
                    company=urlparse(career_url).hostname or "",
                    title_preview="No jobs found",
                    location_preview=None,
                )
                _write_job_artifact_bundle(
                    out_dir=no_jobs_dir,
                    run_id=run_id,
                    listing=no_jobs_listing,
                    correlation_id=self.state.correlation_id,
                    submission_outcome="no_jobs_found",
                    listing_decision="skipped",
                    listing_error_code="empty_listings",
                    classifier_score=0.0,
                    resume_path=None,
                    cover_letter_path=None,
                    fields=[],
                    field_answers=[],
                    provenance={"listing_jobs": {"outcome": "no_jobs_found", "error_code": "empty_listings", "hint": hint}},
                    error=hint,
                    parsed_text="",
                    visible_text="",
                )
                if hasattr(self.app_state.sqlite, "record_listing_state"):
                    self.app_state.sqlite.record_listing_state(
                        run_id,
                        career_url,
                        state=ListingLifecycle.SKIPPED.value,
                        error_code="empty_listings",
                        error_message=hint,
                    )
                summary_path = self._write_run_summary(run_id, "done")
                self.app_state.sqlite.update_run(
                    run_id,
                    skip_reasons_json=json.dumps(self._skip_reasons, sort_keys=True),
                    soft_fail_count=self._outcome_counters["soft_fail"],
                    hard_fail_count=self._outcome_counters["hard_fail"],
                    browser_open=int(bool(config.live_mode_enabled())),
                    summary_artifact_path=str(summary_path) if summary_path else None,
                    status="done",
                )
                self.state.state = "done"
                self.app_state.sqlite.finish_run(run_id, "done")
                return
            listings = _limit_listings_for_run(listings, limit)
            single_listing_retry = force_reprocess and len(listings) == 1
            for listing in listings:
                if self._stop_requested:
                    break
                forced_listing = force_reprocess and (single_listing_retry or _same_job_url(listing.url, career_url))
                listing_adapter = dispatch_adapter(listing.url)
                # Clean up stale tabs from previous iterations to prevent resource exhaustion
                await browser.cleanup_stale_tabs()
                if hasattr(self.app_state.sqlite, "record_listing_state"):
                    self.app_state.sqlite.record_listing_state(run_id, listing.url, state=ListingLifecycle.IN_PROGRESS.value)
                duplicate = None if forced_listing else self._find_semantic_duplicate(listing)
                if duplicate is not None:
                    self._skip_reasons["duplicate"] = self._skip_reasons.get("duplicate", 0) + 1
                    self._record_local_application(
                        job_url=listing.url,
                        job_id_ext=listing.ext_id,
                        company=listing.company,
                        title=listing.title_preview,
                        location=listing.location_preview,
                        decision="duplicate",
                        submitted=False,
                        submission_outcome="not_submitted",
                        duplicate_of_job_url=duplicate["job_url"],
                        duplicate_reason_code="semantic_match",
                        liveness_state=duplicate.get("liveness_state"),
                        provenance={"duplicate_candidate": duplicate},
                    )
                    if not self._mongo_enabled() and hasattr(self.app_state.sqlite, "record_duplicate_audit"):
                        self.app_state.sqlite.record_duplicate_audit(
                            job_url=listing.url,
                            duplicate_of_job_url=duplicate["job_url"],
                            similarity_score=duplicate.get("similarity_score", 1.0),
                            reason_code="semantic_match",
                            snapshot=duplicate,
                        )
                    await self.app_state.stream.publish(
                        "progress",
                        self._event_envelope(
                            "duplicate_skip",
                            stage="dedup",
                            message="Skipped semantic duplicate candidate",
                            outcome="skipped",
                            error_code="semantic_match",
                            url=listing.url,
                            title=listing.title_preview,
                            decision=duplicate.get("decision"),
                            submitted=bool(duplicate.get("submitted")),
                            prior_liveness_state=duplicate.get("liveness_state"),
                            reason_code="semantic_match",
                        ),
                    )
                    if hasattr(self.app_state.sqlite, "record_listing_state"):
                        self.app_state.sqlite.record_listing_state(run_id, listing.url, state=ListingLifecycle.SKIPPED.value)
                    continue
                existing = None if forced_listing else self._existing_application(listing.url)
                if existing:
                    if _should_reprocess_existing_application(existing):
                        await self.app_state.stream.publish(
                            "progress",
                            self._event_envelope(
                                "reprocessing_previous_attempt",
                                stage="dedup",
                                message="Reprocessing a previous attention-needed attempt",
                                outcome="in_progress",
                                url=listing.url,
                                title=listing.title_preview,
                                prior_decision=existing.get("decision"),
                            ),
                        )
                    else:
                        prior_error = existing.get("error")
                        submitted = bool(existing.get("submitted"))
                        detail = (
                            f"Skipping already processed job: {listing.title_preview or listing.url}"
                            if submitted
                            else f"Skipping previously recorded job: {listing.title_preview or listing.url}"
                        )
                        await self._stage("skipping_duplicate", detail)
                        await self.app_state.stream.publish(
                            "progress",
                            self._event_envelope(
                                "duplicate_skip",
                                stage="dedup",
                                message=prior_error or detail,
                                outcome="skipped",
                                error_code="existing_application",
                                url=listing.url,
                                title=listing.title_preview,
                                decision=existing.get("decision"),
                                submitted=submitted,
                                error=prior_error,
                            ),
                        )
                        if hasattr(self.app_state.sqlite, "record_listing_state"):
                            self.app_state.sqlite.record_listing_state(run_id, listing.url, state=ListingLifecycle.SKIPPED.value)
                        continue
                if self.app_state.sqlite.site_limit_hit(listing.url):
                    await self.app_state.stream.publish("progress", self._event_envelope("limit_hit", stage="listing_jobs", message="Site limit reached", outcome="stopped", url=listing.url))
                    break

                active_check = getattr(listing_adapter, "is_active_listing", None)
                if callable(active_check):
                    await self._stage("checking_listing_liveness", f"Checking listing status: {listing.title_preview or listing.url}")
                    is_active = await self._retry_stage(
                        "listing_liveness",
                        lambda listing_url=listing.url: active_check(page, listing_url),
                        category="listing_liveness_failed",
                        hint="Open the posting manually and confirm it is still accepting applications.",
                    )
                    if is_active is False:
                        self._record_local_application(
                            job_url=listing.url,
                            job_id_ext=listing.ext_id,
                            company=listing.company,
                            title=listing.title_preview,
                            location=listing.location_preview,
                            decision="fail",
                            submitted=False,
                            submission_outcome="expired",
                            liveness_state="inactive",
                            liveness_reasons=["adapter_active_listing_check"],
                            provenance={"liveness": {"state": "inactive", "source": listing_adapter.__class__.__name__}},
                        )
                        await self.app_state.stream.publish(
                            "progress",
                            self._event_envelope(
                                "liveness_check",
                                stage="liveness",
                                message="Skipped inactive listing before processing",
                                outcome="inactive",
                                url=listing.url,
                                state="inactive",
                                reasons=["adapter_active_listing_check"],
                            ),
                        )
                        if hasattr(self.app_state.sqlite, "record_listing_state"):
                            self.app_state.sqlite.record_listing_state(run_id, listing.url, state=ListingLifecycle.SKIPPED.value)
                        continue

                self.state.current_job = f"{listing.company or ''} {listing.title_preview or listing.url}".strip()
                role_label = listing.title_preview or listing.url
                # Checkpoint: starting job description extraction
                self._save_stage_checkpoint(run_id, listing.url, "extract_description", {"role_label": role_label})
                await self._stage("parsing_job_description", f"Parsing job description: {role_label}")
                description = await self._retry_stage(
                    "extract_description",
                    lambda listing_url=listing.url, adapter=listing_adapter: adapter.extract_description(page, listing_url),
                    category="job_description_failed",
                    hint="Open the posting manually and confirm the job description renders without auth or client-side errors.",
                )
                visible_text = await _page_visible_text(page)
                parsing_assessment = assess_parsing_coverage(visible_text=visible_text, parsed_text=description)
                out_dir = OUTPUT_DIR / f"run_{run_id}" / _safe_slug(listing.ext_id or listing.title_preview or listing.url)
                job_slug = out_dir.name
                try:
                    job_html = await page.content()
                except Exception:
                    job_html = ""
                liveness = self._classify_liveness(description, html=job_html)
                if liveness["state"] != "active":
                    decision = "manual_review_required" if liveness["state"] == "uncertain" else "fail"
                    outcome = "manual_review_required" if liveness["state"] == "uncertain" else "liveness_expired"
                    provenance = {
                        "liveness": liveness,
                        "parsing": parsing_assessment,
                        "parsed_text_sha256": _sha256_text(description),
                        "visible_text_sha256": _sha256_text(visible_text),
                    }
                    _write_job_artifact_bundle(
                        out_dir=out_dir,
                        run_id=run_id,
                        listing=listing,
                        correlation_id=self.state.correlation_id,
                        submission_outcome=outcome,
                        listing_decision=decision,
                        listing_error_code=liveness["state"],
                        classifier_score=0.0,
                        resume_path=None,
                        cover_letter_path=None,
                        fields=[],
                        field_answers=[],
                        provenance=provenance,
                        error=None,
                        parsed_text=description,
                        visible_text=visible_text,
                    )
                    self._record_local_application(
                        job_url=listing.url,
                        job_id_ext=listing.ext_id,
                        company=listing.company,
                        title=listing.title_preview,
                        location=listing.location_preview,
                        decision=decision,
                        submitted=False,
                        submission_outcome=outcome,
                        liveness_state=liveness["state"],
                        liveness_reasons=liveness["reasons"],
                        provenance=provenance,
                    )
                    self._record_mongo_application(
                        run_id=run_id,
                        career_url=career_url,
                        listing=listing,
                        description=description,
                        classifier_score=0.0,
                        decision=decision,
                        submitted=False,
                    )
                    await self.app_state.stream.publish("progress", self._event_envelope("liveness_check", stage="liveness", message="Liveness classified", outcome=liveness["state"], url=listing.url, **liveness))
                    if hasattr(self.app_state.sqlite, "record_listing_state"):
                        self.app_state.sqlite.record_listing_state(run_id, listing.url, state=ListingLifecycle.SKIPPED.value)
                    continue
                await self._stage("classifying_job", f"Classifying fit for {role_label}")
                classifier_details = self.app_state.classifier.score_details(description, self.app_state.encoder)
                score = float(classifier_details["score"])
                classifier_threshold_decision = "pass" if score >= CLASSIFIER_THRESHOLD else "fail"
                strict_fit_payload = _build_fit_decision_payload(
                    listing=listing,
                    description=description,
                    classifier_details=classifier_details,
                    classifier_threshold_decision=classifier_threshold_decision,
                )
                if forced_listing and bypass_classifier:
                    fit_decision_payload = dict(strict_fit_payload)
                    fit_decision_payload["fit"] = True
                    fit_decision_payload["manual_override"] = True
                    fit_decision_payload["manual_override_reason"] = "User clicked Apply on a rejected role."
                    human_passed = True
                    ClassifierFeedbackStore().append(
                        job_url=listing.url,
                        label="pass",
                        score=score,
                        description_text=description,
                        title=listing.title_preview,
                        company=listing.company,
                    )
                    await self.app_state.stream.publish(
                        "progress",
                        self._event_envelope(
                            "classifier_user_override",
                            stage="classifying_job",
                            message="User override: continuing rejected role",
                            outcome="pass",
                            url=listing.url,
                            score=score,
                        ),
                    )
                elif config.classifier_auto_pass_enabled():
                    if not bool(strict_fit_payload.get("fit")):
                        filtered_outcome = _autonomous_classifier_rejection_outcome(
                            strict_fit_payload,
                            classifier_threshold_decision,
                        )
                        provenance = {
                            "classifier": classifier_details,
                            "fit_decision": strict_fit_payload,
                            "run_id": run_id,
                            "approval": {"mode": "classifier_auto_pass", "approved": False},
                            "parsing": parsing_assessment,
                            "parsed_text_sha256": _sha256_text(description),
                            "visible_text_sha256": _sha256_text(visible_text),
                        }
                        _write_job_artifact_bundle(
                            out_dir=out_dir,
                            run_id=run_id,
                            listing=listing,
                            correlation_id=self.state.correlation_id,
                            submission_outcome=filtered_outcome,
                            listing_decision="fail",
                            listing_error_code=filtered_outcome,
                            classifier_score=score,
                            resume_path=None,
                            cover_letter_path=None,
                            fields=[],
                            field_answers=[],
                            provenance=provenance,
                            error=_fit_decision_summary(strict_fit_payload) or "Classifier auto-pass rejected job",
                            parsed_text=description,
                            visible_text=visible_text,
                        )
                        self._record_local_application(
                            job_url=listing.url,
                            job_id_ext=listing.ext_id,
                            company=listing.company,
                            title=listing.title_preview,
                            location=listing.location_preview,
                            classifier_score=score,
                            decision="fail",
                            submitted=False,
                            submission_outcome=filtered_outcome,
                            provenance=provenance,
                            error=_fit_decision_summary(strict_fit_payload) or "Classifier auto-pass rejected job",
                        )
                        self._record_mongo_application(
                            run_id=run_id,
                            career_url=career_url,
                            listing=listing,
                            description=description,
                            classifier_score=score,
                            classifier_mode=str(classifier_details["mode"]),
                            classifier_confidence=float(classifier_details["confidence"]),
                            decision="fail",
                            submitted=False,
                            error=_fit_decision_summary(strict_fit_payload) or "Classifier auto-pass rejected job",
                        )
                        ClassifierFeedbackStore().append(
                            job_url=listing.url,
                            label="fail",
                            score=score,
                            description_text=description,
                            title=listing.title_preview,
                            company=listing.company,
                        )
                        if hasattr(self.app_state.sqlite, "record_listing_state"):
                            self.app_state.sqlite.record_listing_state(run_id, listing.url, state=ListingLifecycle.COMPLETED.value)
                        continue
                    fit_decision_payload = strict_fit_payload
                    human_passed = True
                    ClassifierFeedbackStore().append(
                        job_url=listing.url,
                        label="pass",
                        score=score,
                        description_text=description,
                        title=listing.title_preview,
                        company=listing.company,
                    )
                    await self.app_state.stream.publish(
                        "progress",
                        self._event_envelope(
                            "classifier_auto_passed",
                            stage="classifying_job",
                            message="Classifier auto-pass accepted strict fit rules and score threshold",
                            outcome="pass",
                            url=listing.url,
                            score=score,
                        ),
                    )
                else:
                    fit_decision_payload = strict_fit_payload
                    human_passed = False

                if not (forced_listing and bypass_classifier) and not config.classifier_auto_pass_enabled():
                    await self._stage("classifier_review", f"Awaiting classifier review for {role_label}")
                    classifier_token = f"classifier:{run_id}:{_safe_slug(listing.url)}"
                    classifier_review = await self.app_state.classifier_review.request(
                        classifier_token,
                        {
                            "company": listing.company,
                            "title": listing.title_preview,
                            "job_url": listing.url,
                            "run_id": run_id,
                            "career_url": career_url,
                            "artifact_dir": str(out_dir),
                            "job_slug": job_slug,
                            "classifier_score": score,
                            "classifier_threshold_decision": classifier_threshold_decision,
                            "location": listing.location_preview,
                            "description_preview": description[:2500],
                            "description_text": description,
                            "parsed_text_sha256": _sha256_text(description),
                            "visible_text_sha256": _sha256_text(visible_text),
                            "parsing_assessment": parsing_assessment,
                            "fit_decision": strict_fit_payload,
                            "fit_decision_summary": _fit_decision_summary(strict_fit_payload),
                            "reason": _fit_decision_summary(strict_fit_payload) or "Classifier review needed",
                        },
                    )
                    human_passed = bool(classifier_review.get("passed"))
                    fit_decision_payload = classifier_review.get("decision_payload") or strict_fit_payload
                    _append_classifier_agent_signal(
                        listing=listing,
                        description=description,
                        score=score,
                        sut_decision=classifier_threshold_decision,
                        decision_payload=fit_decision_payload,
                    )
                if not human_passed:
                    filtered_outcome = _filtered_outcome_from_review(fit_decision_payload, classifier_threshold_decision)
                    provenance = {
                        "classifier": classifier_details,
                        "fit_decision": fit_decision_payload,
                        "run_id": run_id,
                        "approval": {"mode": "classifier_review", "approved": False},
                        "parsing": parsing_assessment,
                        "parsed_text_sha256": _sha256_text(description),
                        "visible_text_sha256": _sha256_text(visible_text),
                    }
                    _write_job_artifact_bundle(
                        out_dir=out_dir,
                        run_id=run_id,
                        listing=listing,
                        correlation_id=self.state.correlation_id,
                        submission_outcome=filtered_outcome,
                        listing_decision="human_fail",
                        listing_error_code=str(fit_decision_payload.get("submission_outcome") or "classifier_rejected"),
                        classifier_score=score,
                        resume_path=None,
                        cover_letter_path=None,
                        fields=[],
                        field_answers=[],
                        provenance=provenance,
                        error="Marked fail during classifier review",
                        parsed_text=description,
                        visible_text=visible_text,
                    )
                    self._record_local_application(
                        job_url=listing.url,
                        job_id_ext=listing.ext_id,
                        company=listing.company,
                        title=listing.title_preview,
                        location=listing.location_preview,
                        classifier_score=score,
                        decision="human_fail",
                        submitted=False,
                        submission_outcome=filtered_outcome,
                        provenance=provenance,
                        error="Marked fail during classifier review",
                    )
                    self._record_mongo_application(
                        run_id=run_id,
                        career_url=career_url,
                        listing=listing,
                        description=description,
                        classifier_score=score,
                        classifier_mode=str(classifier_details["mode"]),
                        classifier_confidence=float(classifier_details["confidence"]),
                        decision="human_fail",
                        submitted=False,
                        error="Marked fail during classifier review",
                    )
                    await self.app_state.stream.publish(
                        "progress",
                        self._event_envelope(
                            "classifier_marked_fail",
                            stage="classifier_review",
                            message="Marked fail during classifier review",
                            outcome=filtered_outcome,
                            error_code=str(fit_decision_payload.get("submission_outcome") or "classifier_rejected"),
                            url=listing.url,
                            score=score,
                        ),
                    )
                    if hasattr(self.app_state.sqlite, "record_listing_state"):
                        self.app_state.sqlite.record_listing_state(run_id, listing.url, state=ListingLifecycle.COMPLETED.value)
                    continue

                jobs_passed += 1
                self.app_state.sqlite.update_run(run_id, jobs_passed=jobs_passed)

                resume_path = None
                cover_letter_path = None
                form_fields_answered: list[dict[str, Any]] = []
                submit_error = None
                artifact_paths: dict[str, str] = {}
                submitted = False
                resume_context: dict[str, Any] | None = None
                resume_needed = False
                cover_needed = False
                enumerated_fields_snapshot: list[dict[str, Any]] = []
                fallback_reasons: list[dict[str, Any]] = []
                warnings: list[dict[str, Any]] = []
                ats_score_payload: dict[str, Any] | None = None
                try:
                    out_dir.mkdir(parents=True, exist_ok=True)
                    filler = FormFiller(self.app_state)
                    try:
                        await self._stage("opening_application", f"Opening application form for {role_label}")
                        page = await filler._open_form_page(page, listing_adapter, listing.url)
                        await self._show_live_browser(page)
                    except NotImplementedError as exc:
                        takeover_token = f"manual:{run_id}:{_safe_slug(listing.url)}"
                        takeover = await self.app_state.manual_takeover.request(
                            takeover_token,
                            {
                                "job_url": listing.url,
                                "company": listing.company,
                                "title": listing.title_preview,
                                "reason": str(exc),
                                "current_url": getattr(page, "url", listing.url),
                            },
                        )
                        if takeover.get("action") != "continue":
                            raise
                    await self._stage("enumerating_fields", "Reading form fields")
                    fields = await listing_adapter.enumerate_fields(page)
                    await self._stage("enumerated_fields", f"Found {len(fields)} form fields")
                    fields, skipped_noise = _filter_application_fields(fields)
                    enumerated_fields_snapshot = [_serialize_form_field(field) for field in fields]
                    log.info(
                        "application_fields_filtered",
                        real_fields=len(fields),
                        skipped_noise=skipped_noise,
                        job_url=listing.url,
                    )

                    resume_needed = resume_requested(fields)
                    cover_needed = cover_letter_requested(fields)
                    await self.app_state.stream.publish(
                        "progress",
                        self._event_envelope("document_requirements", stage="enumerated_fields", message="Document requirements detected", outcome="ready", url=listing.url, resume_needed=resume_needed, cover_letter_needed=cover_needed),
                    )

                    if resume_needed or cover_needed:
                        await self._stage("building_resume_context", f"Tailoring resume content for {role_label}")
                        builder = ResumeContextBuilder(self.app_state.encoder, self.app_state.generator)
                        resume_context = await builder.build(description)
                        if listing.company:
                            resume_context.setdefault("job_meta", {})["company"] = listing.company
                        if listing.title_preview:
                            resume_context.setdefault("job_meta", {})["role_title"] = listing.title_preview
                            resume_context["tagline"] = _tagline_for_role(listing.title_preview, description)
                            resume_context["profile_paragraph"] = _retarget_profile(
                                resume_context.get("profile_paragraph", ""),
                                listing.title_preview,
                                listing.company,
                            )

                    if resume_needed and resume_context is not None:
                        try:
                            await self._stage("generating_resume", f"Generating tailored resume PDF for {role_label}")
                            tex_path = ResumeAssembler(ROOT_DIR / "templates" / "resume" / "resume.tex.jinja").render_to_file(resume_context, out_dir / "resume.tex")
                            resume_path = await self._compile_document(compile_latex, tex_path, out_dir, stage="generating_resume")
                        except Exception as exc:
                            log.exception("resume_generation_failed", job_url=listing.url, error=str(exc))
                            fallback = create_fallback_artifact(out_dir, stem="resume", reason=f"resume_generation_failed: {exc}", reason_code="resume_generation_failed")
                            resume_path = fallback["pdf_path"]
                            artifact_paths["resume_fallback_reason_path"] = fallback["reason_path"]
                            fallback_reasons.append({"artifact": "resume", "reason_code": "resume_generation_failed", "message": str(exc), "reason_path": fallback["reason_path"]})
                            await self.app_state.stream.publish(
                                "progress",
                                self._event_envelope("artifact_fallback", stage="generating_resume", message="Resume fallback artifact created", outcome="degraded", error_code="resume_generation_failed", url=listing.url, artifact="resume", reason_code="resume_generation_failed", fallback_path=resume_path, fallback_reason_path=fallback["reason_path"]),
                            )

                    if cover_needed and resume_context is not None:
                        try:
                            await self._stage("generating_cover_letter", "Generating cover letter")
                            cover_payload = await CoverLetterWriter(self.app_state.generator).build(
                                resume_context["job_meta"],
                                _candidate_evidence_block(resume_context),
                                _earliest_start_date(),
                            )
                            cover_context = {
                                "sender": resume_context.get("personal", {}),
                                "company_name": resume_context["job_meta"].get("company") or listing.company or "Hiring Team",
                                **cover_payload,
                            }
                            cover_tex = CoverLetterAssembler().render_to_file(cover_context, out_dir / "cover.tex")
                            cover_letter_path = await self._compile_document(compile_cover_latex, cover_tex, out_dir, stage="generating_cover_letter")
                        except Exception as exc:
                            log.exception("cover_letter_generation_failed", job_url=listing.url, error=str(exc))
                            fallback = create_fallback_artifact(out_dir, stem="cover-letter", reason=f"cover_letter_generation_failed: {exc}", reason_code="cover_letter_generation_failed")
                            cover_letter_path = fallback["pdf_path"]
                            artifact_paths["cover_letter_fallback_reason_path"] = fallback["reason_path"]
                            fallback_reasons.append({"artifact": "cover_letter", "reason_code": "cover_letter_generation_failed", "message": str(exc), "reason_path": fallback["reason_path"]})
                            await self.app_state.stream.publish(
                                "progress",
                                self._event_envelope("artifact_fallback", stage="generating_cover_letter", message="Cover letter fallback artifact created", outcome="degraded", error_code="cover_letter_generation_failed", url=listing.url, artifact="cover_letter", reason_code="cover_letter_generation_failed", fallback_path=cover_letter_path, fallback_reason_path=fallback["reason_path"]),
                            )

                    warnings = _validation_warnings(form_fields_answered)
                    approval_company = (
                        (resume_context["job_meta"].get("company") if resume_context else None)
                        or listing.company
                    )
                    approval_title = (
                        (resume_context["job_meta"].get("role_title") if resume_context else None)
                        or listing.title_preview
                    )
                    document_paths = {
                        "resume_path": resume_path,
                        "cover_letter_path": cover_letter_path,
                    }
                    fill_outcome = await filler.run(
                        page=page,
                        adapter=listing_adapter,
                        job_context={
                            "run_id": run_id,
                            "job_url": listing.url,
                            "career_url": career_url,
                            "fixture_url": listing.url,
                            "company": listing.company,
                            "title": listing.title_preview,
                            "jd": description,
                            "artifact_dir": str(out_dir),
                            "job_slug": job_slug,
                        },
                        document_paths=document_paths,
                        approval_details={
                            "approval_token": f"{run_id}:{_safe_slug(listing.url)}",
                            "company": approval_company,
                            "title": approval_title,
                            "job_url": listing.url,
                            "classifier_score": score,
                            "description_text": description,
                            "resume_path": resume_path,
                            "cover_letter_path": cover_letter_path,
                            "resume_needed": resume_needed,
                            "cover_letter_needed": cover_needed,
                            "validation_warnings": warnings,
                            "missing_required_fields": [warning["label"] for warning in warnings if warning["level"] == "error"],
                            "dry_run": config.DRY_RUN,
                            "live_submit_enabled": config.live_submit_enabled(),
                            "auto_submit_without_approval": config.auto_submit_without_approval_enabled(),
                        },
                    )
                    page = fill_outcome.page
                    form_fields_answered = fill_outcome.field_answers
                    submitted = fill_outcome.submitted
                    submit_error = fill_outcome.submit_error
                    resume_path = document_paths.get("resume_path") or resume_path
                    cover_letter_path = document_paths.get("cover_letter_path") or cover_letter_path
                    if document_paths.get("resume_fallback_reason_path"):
                        artifact_paths["resume_fallback_reason_path"] = document_paths["resume_fallback_reason_path"]
                    if document_paths.get("cover_letter_fallback_reason_path"):
                        artifact_paths["cover_letter_fallback_reason_path"] = document_paths["cover_letter_fallback_reason_path"]
                    artifact_paths["pre_submit_audit"] = json.dumps(fill_outcome.pre_submit_audit or {}, sort_keys=True)
                    ats_score_payload = _score_resume_artifact(resume_path=resume_path, resume_context=resume_context, out_dir=out_dir)
                    if ats_score_payload is not None and config.RETAIN_LOCAL_ARTIFACTS:
                        artifact_paths["ats_score_path"] = str(out_dir / "ats_score.json")
                except NotImplementedError:
                    submit_error = "Application form automation is not supported for this site yet; resume generated only."
                    artifact_paths = await _save_failure_artifacts(page, out_dir, "manual-review")
                    await self.app_state.stream.publish(
                        "progress",
                        self._event_envelope("manual_review_required", stage="opening_application", message=submit_error, outcome="manual_review_required", error_code="unsupported_site", url=listing.url, **artifact_paths),
                    )
                except Exception as exc:
                    submit_error = str(exc) or exc.__class__.__name__
                    blocker = self._classify_blocker(submit_error, getattr(page, "url", listing.url))
                    if blocker["decision"] != "error":
                        submit_error = blocker["message"]
                        self._skip_reasons[blocker["status"]] = self._skip_reasons.get(blocker["status"], 0) + 1
                    artifact_paths = await _save_failure_artifacts(page, out_dir, "failure")
                    log.exception("application_dry_run_failed", job_url=listing.url, error=submit_error)

                resume_ok = resume_path or not resume_needed
                submission_outcome = _submission_outcome(submitted, submit_error, resume_ok)
                blocker = self._classify_blocker(submit_error, getattr(page, "url", listing.url)) if submit_error else None
                if blocker is not None:
                    submission_outcome = _submission_outcome_for_blocker(blocker, submission_outcome)
                listing_decision = blocker["decision"] if blocker else "pass"
                listing_error_code = blocker["status"] if blocker else ("submission_error" if submit_error else None)
                application_provenance = {
                    "classifier": classifier_details,
                    "fit_decision": fit_decision_payload,
                    "run_id": run_id,
                    "approval": {
                        "mode": "auto" if config.auto_submit_without_approval_enabled() and not warnings else "manual",
                        "warnings": warnings,
                    },
                    "filled_field_summary": {
                        "count": len(form_fields_answered),
                        "required_missing": [warning["label"] for warning in warnings if warning["level"] == "error"],
                    },
                    "artifacts": artifact_paths,
                    "ats_score": ats_score_payload,
                    "parsing": parsing_assessment,
                    "parsed_text_sha256": _sha256_text(description),
                    "visible_text_sha256": _sha256_text(visible_text),
                    "artifact_fallbacks": fallback_reasons,
                    "artifact_status": {
                        "resume": "fallback" if any(item["artifact"] == "resume" for item in fallback_reasons) else ("ready" if resume_path else "not_needed"),
                        "cover_letter": "fallback" if any(item["artifact"] == "cover_letter" for item in fallback_reasons) else ("ready" if cover_letter_path else "not_needed"),
                    },
                }
                _write_job_artifact_bundle(
                    out_dir=out_dir,
                    run_id=run_id,
                    listing=listing,
                    correlation_id=self.state.correlation_id,
                    submission_outcome=submission_outcome,
                    listing_decision=listing_decision,
                    listing_error_code=listing_error_code,
                    classifier_score=score,
                    resume_path=resume_path,
                    cover_letter_path=cover_letter_path,
                    fields=enumerated_fields_snapshot,
                    field_answers=form_fields_answered,
                    provenance=application_provenance,
                    error=submit_error,
                    parsed_text=description,
                    visible_text=visible_text,
                )
                # Tag the attempt so the History UI can split it into the DRY RUN
                # vs REAL SUBMITS section. Live submit is the only mode that can
                # produce a "submitted" outcome; everything else is a dry run.
                attempt_mode = _current_attempt_mode()
                self._record_local_application(
                    job_url=listing.url,
                    job_id_ext=listing.ext_id,
                    company=listing.company,
                    title=listing.title_preview,
                    location=listing.location_preview,
                    classifier_score=score,
                    decision=listing_decision,
                    submitted=submitted,
                    submission_outcome=submission_outcome,
                    resume_path=resume_path,
                    cover_letter_path=cover_letter_path,
                    provenance=application_provenance,
                    error=submit_error if submit_error else (None if resume_ok else "resume generation failed"),
                    attempt_mode=attempt_mode,
                )
                if hasattr(self.app_state.sqlite, "record_listing_state"):
                    final_listing_state = (
                        ListingLifecycle.COMPLETED.value
                        if not submit_error
                        else (ListingLifecycle.BLOCKED.value if blocker and blocker["blocked"] else ListingLifecycle.FAILED_TRANSIENT.value)
                    )
                    retries = 1 if submit_error and not (blocker and blocker["blocked"]) else 0
                    self.app_state.sqlite.record_listing_state(
                        run_id,
                        listing.url,
                        state=final_listing_state,
                        retry_count=retries,
                        checkpoint={"submission_outcome": submission_outcome, "resume_path": resume_path, "cover_letter_path": cover_letter_path},
                        error_code=listing_error_code,
                        error_message=submit_error,
                    )
                if submit_error:
                    self._count_failure(blocker)
                self._record_mongo_application(
                    run_id=run_id,
                    career_url=career_url,
                    listing=listing,
                    description=description,
                    classifier_score=score,
                    classifier_mode=str(classifier_details["mode"]),
                    classifier_confidence=float(classifier_details["confidence"]),
                    decision=listing_decision,
                    submitted=submitted,
                    resume_path=resume_path,
                    cover_letter_path=cover_letter_path,
                    error=submit_error if submit_error else (None if resume_ok else "resume generation failed"),
                    form_fields_answered=form_fields_answered,
                    artifact_paths=artifact_paths,
                    resume_needed=resume_needed,
                    cover_needed=cover_needed,
                    approval_mode="auto" if config.auto_submit_without_approval_enabled() and not warnings else "manual",
                    artifact_fallbacks=fallback_reasons,
                )
                if submitted:
                    jobs_applied += 1
                self.app_state.sqlite.update_run(run_id, jobs_applied=jobs_applied)
                await self.app_state.stream.publish(
                    "progress",
                    self._event_envelope(
                        "listing_result",
                        stage="finalize_listing",
                        message="Listing finalized",
                        outcome=submission_outcome,
                        error_code=listing_error_code,
                        url=listing.url,
                        score=score,
                        resume_path=resume_path,
                        cover_letter_path=cover_letter_path,
                        submission_outcome=submission_outcome,
                        fields_answered=len(form_fields_answered),
                        error=submit_error,
                        **artifact_paths,
                    ),
                )
                if hasattr(self.app_state.sqlite, "event"):
                    self.app_state.sqlite.event(
                        run_id=run_id,
                        job_url=listing.url,
                        event_type="listing_finalized",
                        payload={"submission_outcome": submission_outcome, "submitted": submitted, "error": submit_error},
                    )
                _cleanup_local_artifacts(out_dir)

            final_status = "stopped" if self._stop_requested else "done"
            summary_path = self._write_run_summary(run_id, final_status)
            self.app_state.sqlite.update_run(
                run_id,
                skip_reasons_json=json.dumps(self._skip_reasons, sort_keys=True),
                soft_fail_count=self._outcome_counters["soft_fail"],
                hard_fail_count=self._outcome_counters["hard_fail"],
                browser_open=int(bool(config.live_mode_enabled())),
                summary_artifact_path=str(summary_path) if summary_path else None,
            )
            self.app_state.sqlite.finish_run(run_id, final_status)
            self.state.state = final_status
        except Exception as exc:
            log.exception("run_failed", run_id=run_id, error=str(exc))
            self.app_state.sqlite.finish_run(run_id, "error")
            self.state.state = "error"
            await self._publish_failure("run", exc, category="run_failed", hint="Check the structured failure payload and generated artifacts.")
        finally:
            self.state.run_id = None if self.state.state != "running" else self.state.run_id
            self.state.current_job = None
            self.state.current_stage = None
            self.state.current_stage_message = None
            try:
                if page is not None and getattr(self.app_state.alarm, "active_page", None) is page:
                    self.app_state.alarm.active_page = None
            except Exception:
                pass
            # Watch Browser ON keeps the visible page available for inspection.
            # Watch Browser OFF should leave no automation browser on screen after a run.
            if not config.live_mode_enabled():
                try:
                    await browser.close()
                except Exception:
                    pass

    async def _retry_stage(self, stage: str, operation, *, category: str, hint: str, attempts: int = 2):
        attempt_counter = {"value": 0}

        async def wrapped():
            try:
                timeout = config.STAGE_TIMEOUT_EXTRACT_DESCRIPTION_SECONDS if stage == "extract_description" else config.STAGE_TIMEOUT_BROWSER_ACTION_SECONDS
                return await asyncio.wait_for(operation(), timeout=timeout)
            except (asyncio.TimeoutError, TimeoutError) as exc:
                raise TransientJobError(str(exc), code=f"{stage}_timeout") from exc
            except Exception as exc:
                current_url = None
                try:
                    current_url = getattr(getattr(self.app_state, "alarm", None), "active_page", None).url
                except Exception:
                    current_url = None
                blocker = self._classify_blocker(str(exc), current_url)
                if blocker["status"] in {"failed_transient", "provider_backoff"}:
                    raise TransientJobError(str(exc), code=f"{stage}_{blocker['status']}") from exc
                if isinstance(exc, RuntimeError) and not any(token in str(exc).lower() for token in ("timeout", "network", "connection", "temporar", "retry", "429", "rate limit")):
                    raise
                raise TransientJobError(str(exc), code=f"{stage}_retryable") from exc

        try:
            return await run_with_retry(wrapped, attempts=max(attempts, config.MAX_RETRY_BUDGET))
        except Exception as exc:
            attempt_counter["value"] += 1
            log.warning("stage_attempt_failed", stage=stage, error=str(exc))
            # Capture a debug screenshot on final failure for post-mortem analysis
            await self._capture_debug_screenshot(stage, category)
            await self.app_state.stream.publish(
                "progress",
                self._event_envelope(
                    "stage_retry",
                    stage=stage,
                    message=str(exc),
                    outcome="retrying",
                    error_code=category,
                    attempt=attempt_counter["value"],
                    max_attempts=max(attempts, config.MAX_RETRY_BUDGET),
                ),
            )
            await self._publish_failure(stage, exc, category=category, retryable=True, hint=hint)
            raise

    async def _capture_debug_screenshot(self, stage: str, category: str) -> None:
        """Capture a debug screenshot of the current page state on stage failure.

        Screenshots are saved to data/outputs/debug/ with stage and timestamp
        for post-mortem analysis.
        """
        if not config.RETAIN_LOCAL_ARTIFACTS:
            return
        try:
            page = getattr(self.app_state.alarm, "active_page", None)
            if page is None or (hasattr(page, "is_closed") and page.is_closed()):
                return
            debug_dir = config.OUTPUT_DIR / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_path = debug_dir / f"{stage}_{category}_{timestamp}.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            log.info("debug_screenshot_captured", stage=stage, path=str(screenshot_path))
        except Exception as exc:
            log.debug("debug_screenshot_failed", stage=stage, error=str(exc))

    def _save_stage_checkpoint(self, run_id: int, job_url: str, stage: str, data: dict[str, Any]) -> None:
        """Save a checkpoint for the current stage so recovery is possible after crashes."""
        if not hasattr(self.app_state.sqlite, "record_listing_state"):
            return
        try:
            checkpoint = {
                "stage": stage,
                "job_url": job_url,
                "timestamp": datetime.now().isoformat(),
                **data,
            }
            self.app_state.sqlite.record_listing_state(
                run_id, job_url,
                state=ListingLifecycle.IN_PROGRESS.value,
                checkpoint=checkpoint,
            )
        except Exception as exc:
            log.debug("checkpoint_save_failed", stage=stage, job_url=job_url, error=str(exc))

    def _mongo_enabled(self) -> bool:
        mongo = getattr(self.app_state, "mongo", None)
        return bool(mongo is not None and getattr(mongo, "enabled", lambda: False)())

    def _record_local_application(self, **kwargs: Any) -> None:
        # SQLite is the menu-bar UI's rich local ledger. Even when MongoDB is
        # enabled for the canonical application history, keep SQLite updated so
        # per-mode dry-run / real-submit status can turn rows green and lock
        # retries correctly.
        self.app_state.sqlite.record_application(**kwargs)

    def _existing_application(self, job_url: str) -> dict[str, Any] | None:
        if self._mongo_enabled():
            mongo = getattr(self.app_state, "mongo", None)
            if hasattr(mongo, "get_application"):
                return mongo.get_application(job_url)
            for record in mongo.list_applications(limit=500):
                if record.get("job_url") == job_url:
                    return record
            return None
        if not self.app_state.sqlite.has_application(job_url):
            return None
        return self.app_state.sqlite.get_application(job_url) or None

    def _find_semantic_duplicate(self, listing: Any) -> dict[str, Any] | None:
        if self._mongo_enabled():
            mongo = getattr(self.app_state, "mongo", None)
            candidates: list[dict[str, Any]] = []
            normalized_company = _normalize_company(getattr(listing, "company", None))
            for record in mongo.list_applications(limit=500):
                if normalized_company and _normalize_company(record.get("company")) != normalized_company:
                    continue
                score = _semantic_similarity(getattr(listing, "title_preview", None), record.get("title"))
                location = getattr(listing, "location_preview", None)
                if location and record.get("location") and location.strip().lower() == str(record["location"]).strip().lower():
                    score = min(1.0, score + 0.05)
                if score >= config.DEDUP_THRESHOLD:
                    candidate = dict(record)
                    candidate["similarity_score"] = score
                    candidates.append(candidate)
            return candidates[0] if candidates else None
        if not hasattr(self.app_state.sqlite, "semantic_duplicate_candidate_lookup"):
            return None
        candidates = self.app_state.sqlite.semantic_duplicate_candidate_lookup(
            company=getattr(listing, "company", None),
            title=getattr(listing, "title_preview", None),
            location=getattr(listing, "location_preview", None),
        )
        return candidates[0] if candidates else None

    def _classify_blocker(self, message: str | None, current_url: str | None) -> dict[str, Any]:
        text = f"{message or ''} {current_url or ''}".lower()
        if any(token in text for token in ("captcha", "hcaptcha", "recaptcha", "cloudflare")):
            return {"status": "manual_review_required", "decision": "manual_review_required", "blocked": True, "message": message or "Robot detection requires manual review"}
        if any(token in text for token in ("/login", "/signin", "session expired", "auth", "sso", "mfa")):
            return {"status": "manual_auth_required", "decision": "manual_auth_required", "blocked": True, "message": message or "Manual authentication required"}
        if any(token in text for token in ("api key", "credential", "unauthorized", "forbidden")):
            return {"status": "blocked_credentials", "decision": "blocked_credentials", "blocked": True, "message": message or "Blocked by credentials"}
        if any(token in text for token in ("rate limit", "retry-after", "too many requests", "429")):
            return {"status": "provider_backoff", "decision": "provider_backoff", "blocked": True, "message": message or "Provider requested backoff"}
        if any(token in text for token in ("pdflatex not found", "browser is not available", "dependency", "playwright")):
            return {"status": "dependency_missing", "decision": "dependency_missing", "blocked": True, "message": message or "Required dependency missing"}
        if any(token in text for token in ("timeout", "temporarily", "network", "connection reset")):
            return {"status": "failed_transient", "decision": "failed_transient", "blocked": False, "message": message or "Transient failure"}
        return {"status": "submission_error", "decision": "error", "blocked": False, "message": message or "Unknown failure"}

    def _count_failure(self, blocker: dict[str, Any] | None) -> None:
        if blocker is None:
            self._outcome_counters["hard_fail"] += 1
            return
        if blocker["status"] in {"provider_backoff", "failed_transient"}:
            self._outcome_counters["soft_fail"] += 1
            return
        self._outcome_counters["hard_fail"] += 1

    def _write_run_summary(self, run_id: int, final_status: str):
        if not config.RETAIN_LOCAL_ARTIFACTS:
            return None
        out_dir = OUTPUT_DIR / f"run_{run_id}"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "run-summary.json"
        path.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "status": final_status,
                    "skip_reasons": self._skip_reasons,
                    "soft_fail_count": self._outcome_counters["soft_fail"],
                    "hard_fail_count": self._outcome_counters["hard_fail"],
                },
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        return path

    async def _compile_document(self, compile_fn, tex_path: Path, out_dir: Path, *, stage: str) -> str:
        async def wrapped():
            try:
                return str(compile_fn(tex_path, out_dir))
            except Exception as exc:
                if _latex_retryable(exc):
                    raise TransientJobError(str(exc), code=f"{stage}_latex_retryable") from exc
                raise

        return await run_with_retry(wrapped, attempts=max(2, config.MAX_RETRY_BUDGET))

    def _classify_liveness(self, description: str, *, html: str = "") -> dict[str, Any]:
        return classify_liveness_text(description, html=html)

    def _record_mongo_application(
        self,
        *,
        run_id: int,
        career_url: str,
        listing: Any,
        description: str,
        classifier_score: float,
        classifier_mode: str | None = None,
        classifier_confidence: float | None = None,
        decision: str,
        submitted: bool,
        resume_path: str | None = None,
        cover_letter_path: str | None = None,
        error: str | None = None,
        form_fields_answered: list[dict[str, Any]] | None = None,
        artifact_paths: dict[str, str] | None = None,
        resume_needed: bool | None = None,
        cover_needed: bool | None = None,
        approval_mode: str | None = None,
        artifact_fallbacks: list[dict[str, Any]] | None = None,
    ) -> None:
        mongo = getattr(self.app_state, "mongo", None)
        if mongo is None or not getattr(mongo, "enabled", lambda: False)():
            return
        resume_latex_code = ""
        if resume_path:
            tex_path = Path(resume_path).with_suffix(".tex")
            if not tex_path.exists():
                tex_path = Path(resume_path).parent / "resume.tex"
            if tex_path.exists():
                resume_latex_code = tex_path.read_text(encoding="utf-8", errors="replace")
        document = {
            "company": listing.company,
            "title": listing.title_preview,
            "job_url": listing.url,
            "location": listing.location_preview,
            "submitted": submitted,
            "decision": decision,
            "error": error,
            "description": description,
            "application_type": _application_type_for_listing(listing, description),
            "resume_latex_code": resume_latex_code,
        }
        try:
            mongo.record_application(document)
        except Exception as exc:
            log.warning("mongo_record_application_failed", job_url=listing.url, error=str(exc))


def _safe_slug(value: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in value)[:80].strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "job"


def _same_job_url(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return left.rstrip("/") == right.rstrip("/")


def _normalize_company(value: str | None) -> str:
    text = (value or "").lower().strip()
    for suffix in (" inc.", " ltd.", " llc", ", inc", " private limited", " pvt ltd"):
        text = text.replace(suffix, "")
    return " ".join(text.split())


def _semantic_similarity(left: str | None, right: str | None) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, " ".join(left.lower().split()), " ".join(right.lower().split())).ratio()


def _now_iso() -> str:
    return datetime.now().isoformat()


def _score_resume_artifact(*, resume_path: str | None, resume_context: dict[str, Any] | None, out_dir: Path) -> dict[str, Any] | None:
    if not resume_path:
        return None
    path = Path(resume_path)
    if not path.exists():
        return None
    latex_text = ""
    tex_path = out_dir / "resume.tex"
    if tex_path.exists():
        latex_text = tex_path.read_text(encoding="utf-8", errors="replace")
    job_meta = resume_context.get("job_meta", {}) if isinstance(resume_context, dict) else {}
    keywords = [str(item) for item in job_meta.get("keywords_exact", []) if str(item).strip()]
    allowed_domains: set[str] = set()
    personal = resume_context.get("personal", {}) if isinstance(resume_context, dict) else {}
    if isinstance(personal, dict):
        for value in personal.values():
            host = urlparse(str(value)).hostname
            if host:
                allowed_domains.add(host.removeprefix("www."))
    score = score_resume_pdf(path, latex_text=latex_text, keywords_exact=keywords, allowed_link_domains=allowed_domains).model_dump()
    if config.RETAIN_LOCAL_ARTIFACTS:
        (out_dir / "ats_score.json").write_text(json.dumps(score, indent=2, sort_keys=True), encoding="utf-8")
    return score


def _cleanup_local_artifacts(out_dir: Path) -> None:
    if config.RETAIN_LOCAL_ARTIFACTS:
        return
    try:
        shutil.rmtree(out_dir, ignore_errors=True)
    except Exception as exc:
        log.debug("local_artifact_cleanup_failed", out_dir=str(out_dir), error=str(exc))


def _application_type_for_listing(listing: Any, description: str) -> str:
    text = f"{getattr(listing, 'title_preview', '') or ''}\n{description or ''}".lower()
    return "Intern" if any(token in text for token in ("intern", "internship", "co-op", "coop")) else "Full time"


def _build_fit_decision_payload(
    *,
    listing: Any,
    description: str,
    classifier_details: dict[str, Any],
    classifier_threshold_decision: str,
) -> dict[str, Any]:
    score = float(classifier_details.get("score") or 0.0)
    try:
        facts = load_candidate_fit_facts(ground_truth_path=GROUND_TRUTH_PATH, profile_path=PROFILE_PATH)
        decision = decide_fit(
            title=str(getattr(listing, "title_preview", "") or ""),
            jd_text=description,
            facts=facts,
        )
        profile_diff = proposed_profile_diff(facts)
        if profile_diff:
            decision["proposed_profile_diff"] = profile_diff
    except Exception as exc:
        log.exception("strict_fit_decision_failed", job_url=getattr(listing, "url", None), error=str(exc))
        decision = {
            "fit": False,
            "submission_outcome": "manual_review_required",
            "reason": "strict_fit_decision_failed",
            "error": str(exc),
            "rule_a": {"passed": False, "reasons": ["strict_fit_decision_failed"]},
            "rule_b": {"passed": False, "matched_expertise_area": None, "lexicon_hits": {}},
            "rule_c": {"passed": False, "hard_fail_reasons": ["strict_fit_decision_failed"]},
        }
    decision["sut_score"] = score
    decision["sut_decision"] = classifier_threshold_decision
    decision["classifier_threshold"] = CLASSIFIER_THRESHOLD
    decision["classifier"] = {
        "score": score,
        "base_score": classifier_details.get("base_score"),
        "mode": classifier_details.get("mode"),
        "confidence": classifier_details.get("confidence"),
        "threshold_decision": classifier_threshold_decision,
    }
    decision["rule_d"] = {
        "passed": classifier_threshold_decision == "pass",
        "score": score,
        "threshold": CLASSIFIER_THRESHOLD,
        "reason": None if classifier_threshold_decision == "pass" else "classifier_score_below_threshold",
    }
    if decision.get("fit") and classifier_threshold_decision == "fail":
        decision["fit"] = False
        decision["submission_outcome"] = "filtered_low_score"
        decision["reason"] = "classifier_score_below_threshold_after_strict_rules"
    elif decision.get("fit"):
        decision["reason"] = "strict_rules_and_classifier_threshold_passed"
    else:
        decision.setdefault("reason", str(decision.get("submission_outcome") or "strict_fit_rules_failed"))
    return decision


def _autonomous_classifier_rejection_outcome(decision_payload: dict[str, Any], classifier_threshold_decision: str) -> str:
    outcome = str(decision_payload.get("submission_outcome") or "")
    if outcome and outcome != "dry_run_complete":
        return outcome
    if classifier_threshold_decision == "fail":
        return "filtered_low_score"
    return "manual_review_required"


def _fit_decision_summary(decision_payload: dict[str, Any]) -> str:
    if not decision_payload:
        return ""
    if decision_payload.get("fit"):
        area = ""
        rule_b = decision_payload.get("rule_b")
        if isinstance(rule_b, dict) and rule_b.get("matched_expertise_area"):
            area = f" ({rule_b['matched_expertise_area']})"
        return f"Strict fit passed{area}; score {float(decision_payload.get('sut_score') or 0.0):.2f}."
    reasons: list[str] = []
    rule_a = decision_payload.get("rule_a")
    if isinstance(rule_a, dict):
        reasons.extend(str(item) for item in rule_a.get("reasons", []) if item)
    rule_b = decision_payload.get("rule_b")
    if isinstance(rule_b, dict) and not rule_b.get("passed", True):
        reasons.append("no_profile_overlap")
    rule_c = decision_payload.get("rule_c")
    if isinstance(rule_c, dict):
        reasons.extend(str(item) for item in rule_c.get("hard_fail_reasons", []) if item)
    rule_d = decision_payload.get("rule_d")
    if isinstance(rule_d, dict) and not rule_d.get("passed", True):
        reasons.append(str(rule_d.get("reason") or "classifier_score_below_threshold"))
    if not reasons:
        reason = str(decision_payload.get("reason") or decision_payload.get("submission_outcome") or "strict_fit_failed")
        reasons.append(reason)
    compact = ", ".join(dict.fromkeys(reasons))
    return f"Strict fit blocked: {compact}."


def _field_key(field: Any) -> str:
    return field.name or field.label_text


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
)


def _filter_application_fields(fields: list[Any]) -> tuple[list[Any], int]:
    filtered: list[Any] = []
    skipped = 0
    for field in fields:
        if _is_non_application_field(field):
            skipped += 1
            log.info(
                "field_skipped_non_application",
                label=_field_display_label(field),
                field_type=getattr(field, "field_type", None),
                reason="non_application_field",
            )
            continue
        filtered.append(field)
    return filtered, skipped


def _is_non_application_field(field: Any) -> bool:
    label = _field_display_label(field)
    if not label:
        return True
    name = str(getattr(field, "name", "") or "").lower()
    if len(label) > 300 and _looks_like_disclaimer(label):
        return True
    lower = label.lower()
    if any(token in lower for token in ("robots only", "do not enter if you're human", "honeypot")):
        return True
    if name in {"website", "homepage", "middle_name"} and any(token in lower for token in ("robot", "human", "leave blank", "do not fill")):
        return True
    if any(keyword in lower for keyword in NOISE_FIELD_KEYWORDS):
        return True
    return False


def _looks_like_disclaimer(text: str) -> bool:
    lower = text.lower()
    disclaimer_terms = ("privacy", "cookie", "consent", "gdpr", "terms", "policy", "legal", "notice")
    return sum(term in lower for term in disclaimer_terms) >= 2


def _field_display_label(field: Any) -> str:
    label = " ".join(
        str(part).strip()
        for part in [
            getattr(field, "label_text", "") or "",
            getattr(field, "aria_label", "") or "",
            getattr(field, "placeholder", "") or "",
            getattr(field, "name", "") or "",
        ]
        if str(part).strip()
    ).strip()
    return label[:1000]


def _serialize_form_field(field: Any) -> dict[str, Any]:
    return {
        "key": getattr(field, "name", None) or getattr(field, "label_text", None),
        "label": getattr(field, "label_text", None),
        "name": getattr(field, "name", None),
        "selector": getattr(field, "selector", None),
        "field_type": getattr(field, "field_type", None),
        "required": bool(getattr(field, "required", False)),
        "visible": bool(getattr(field, "visible", True)),
        "options": list(getattr(field, "options", None) or []),
    }


def _approved_answer_map(field_answers: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in field_answers:
        key = item.get("key") or item.get("label")
        if key:
            out[str(key)] = "" if item.get("value") is None else str(item.get("value"))
    return out


def _validation_warnings(field_answers: list[dict[str, Any]]) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    for item in field_answers:
        label = str(item.get("label") or item.get("key") or "Unnamed field")
        value = "" if item.get("value") is None else str(item.get("value")).strip()
        required = bool(item.get("required"))
        field_type = str(item.get("field_type") or "text")
        if required and not value:
            warnings.append(
                {
                    "level": "error",
                    "label": label,
                    "message": f"Required {field_type} field has no planned answer.",
                }
            )
        if field_type == "file" and required and not value:
            warnings.append(
                {
                    "level": "error",
                    "label": label,
                    "message": "Required upload is missing.",
                }
            )
    return warnings


async def _save_failure_artifacts(page: Any, out_dir: Any, prefix: str) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    if not config.RETAIN_LOCAL_ARTIFACTS:
        return artifacts
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return artifacts
    try:
        screenshot = out_dir / f"{prefix}.png"
        await page.screenshot(path=str(screenshot), full_page=True)
        artifacts["screenshot_path"] = str(screenshot)
    except Exception:
        pass
    try:
        html = out_dir / f"{prefix}.html"
        html.write_text(await page.content(), encoding="utf-8")
        artifacts["html_path"] = str(html)
    except Exception:
        pass
    try:
        artifacts["failure_url"] = str(getattr(page, "url", ""))
    except Exception:
        pass
    return artifacts


async def _page_visible_text(page: Any) -> str:
    try:
        value = await page.evaluate("() => document.body ? document.body.innerText || '' : ''")
    except Exception:
        return ""
    return str(value or "")


def _write_job_artifact_bundle(
    *,
    out_dir: Path,
    run_id: int,
    listing: Any,
    correlation_id: str | None,
    submission_outcome: str,
    listing_decision: str,
    listing_error_code: str | None,
    classifier_score: float,
    resume_path: str | None,
    cover_letter_path: str | None,
    fields: list[dict[str, Any]],
    field_answers: list[dict[str, Any]],
    provenance: dict[str, Any],
    error: str | None,
    parsed_text: str | None = None,
    visible_text: str | None = None,
) -> None:
    if not config.RETAIN_LOCAL_ARTIFACTS:
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.utcnow().isoformat() + "Z"
    manifest = {
        "run_id": run_id,
        "correlation_id": correlation_id,
        "job_url": listing.url,
        "job_id_ext": listing.ext_id,
        "company": listing.company,
        "title": listing.title_preview,
        "location": listing.location_preview,
        "decision": listing_decision,
        "terminal_outcome": submission_outcome,
        "submission_outcome": submission_outcome,
        "error_code": listing_error_code,
        "error": error,
        "classifier_score": classifier_score,
        "resume_path": resume_path,
        "cover_letter_path": cover_letter_path,
        "generated_at": now,
    }
    fit_decision = provenance.get("fit_decision") if isinstance(provenance, dict) else None
    if isinstance(fit_decision, dict):
        rule_b = fit_decision.get("rule_b") if isinstance(fit_decision.get("rule_b"), dict) else {}
        manifest["matched_expertise_area"] = rule_b.get("matched_expertise_area")
        manifest["jd_min_years"] = fit_decision.get("jd_min_years")
    parsing = provenance.get("parsing") if isinstance(provenance, dict) else None
    if isinstance(parsing, dict):
        manifest["parsing_coverage_ratio"] = parsing.get("parsing_coverage_ratio")
    ats_score = provenance.get("ats_score") if isinstance(provenance, dict) else None
    if isinstance(ats_score, dict):
        manifest["ats_score"] = ats_score.get("score")
        manifest["ats_score_passed"] = ats_score.get("passed")
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    if isinstance(fit_decision, dict) and fit_decision:
        (out_dir / "fit_decision.json").write_text(json.dumps(fit_decision, indent=2, sort_keys=True), encoding="utf-8")
    if parsed_text is not None:
        (out_dir / "parsed_text_1.txt").write_text(parsed_text, encoding="utf-8")
    if visible_text is not None:
        (out_dir / "visible_text_1.txt").write_text(visible_text, encoding="utf-8")
    answer_by_key = {str(item.get("key") or item.get("label")): item for item in field_answers}
    field_keys = {str(item.get("key") or item.get("label") or "") for item in fields}
    field_payload = []
    for field in fields:
        key = str(field.get("key") or field.get("label") or "")
        answer = answer_by_key.get(key, {})
        field_payload.append({**field, "value": answer.get("value", ""), "answer": answer.get("value", "")})
    for answer in field_answers:
        key = str(answer.get("key") or answer.get("label") or "")
        if key and key not in field_keys:
            field_payload.append(dict(answer))
    (out_dir / "fields.json").write_text(json.dumps({"fields": field_payload}, indent=2, sort_keys=True), encoding="utf-8")
    (out_dir / "provenance.json").write_text(json.dumps(provenance, indent=2, sort_keys=True), encoding="utf-8")
    with (out_dir / "steps.jsonl").open("w", encoding="utf-8") as handle:
        for step in _artifact_steps(submission_outcome, listing_error_code, fields, field_answers):
            handle.write(json.dumps(step, sort_keys=True) + "\n")


def _artifact_steps(
    submission_outcome: str,
    error_code: str | None,
    fields: list[dict[str, Any]],
    field_answers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    terminal_without_form = _terminal_without_form(submission_outcome)
    form_validated = submission_outcome in {"dry_run_complete", "completed_with_deferred", "deferred_blocked_required"}
    return [
        {"name": "discover_listing", "post_condition_passed": True},
        {"name": "open_posting", "post_condition_passed": True},
        {"name": "extract_jd_meta", "post_condition_passed": True},
        {"name": "classify_fit", "post_condition_passed": True},
        {"name": "tailor_resume", "post_condition_passed": True, "skipped": True},
        {"name": "click_apply", "post_condition_passed": True, "skipped": terminal_without_form},
        {"name": "enumerate_fields", "post_condition_passed": terminal_without_form or bool(fields), "field_count": len(fields), "skipped": terminal_without_form},
        {
            "name": "validate_form",
            "post_condition_passed": terminal_without_form or form_validated,
            "answered_count": len(field_answers),
            "error_code": error_code,
            "skipped": terminal_without_form,
        },
        {"name": "record_outcome", "post_condition_passed": True, "submission_outcome": submission_outcome, "error_code": error_code},
    ]


def _candidate_evidence_block(resume_context: dict[str, Any]) -> str:
    lines: list[str] = []
    for experience in resume_context.get("experience_entries", [])[:1]:
        headline = " ".join(
            part for part in [experience.get("title"), experience.get("company")] if part
        ).strip()
        if headline:
            bullets = experience.get("bullets", [])
            if bullets:
                lines.append(f"{headline}: {bullets[0]}")
            else:
                lines.append(headline)
    for project in resume_context.get("projects_top3", [])[:2]:
        lines.append(f"{project.get('name')}: {project.get('one_line_summary')}")
        for bullet in project.get("bullets", [])[:2]:
            lines.append(f"- {bullet}")
    return "\n".join(lines)


def _earliest_start_date() -> str:
    ground_truth = GroundTruthStore().read_if_exists()
    return str(ground_truth.get("preferences", {}).get("earliest_start_date") or "")


def _limit_listings_for_run(listings: list[Any], limit: int | None) -> list[Any]:
    if limit is None:
        return list(listings)
    return list(listings)[: max(0, limit)]


def _submission_outcome(submitted: bool, submit_error: str | None, resume_ok: bool) -> str:
    if submitted and submit_error == "submitted_unconfirmed":
        return "submitted_unconfirmed"
    if submitted:
        return "submitted"
    if not config.live_submit_enabled() and resume_ok and not submit_error:
        return "dry_run_complete"
    return "not_submitted"


def _current_attempt_mode() -> str:
    return "real_submit" if config.live_submit_enabled() else "dry_run"


def _submission_outcome_for_blocker(blocker: dict[str, Any], fallback: str) -> str:
    status = blocker.get("status")
    if status in {"external_interstitial", "manual_review_required"}:
        return "parked_robot_detection"
    if status == "manual_auth_required":
        return "parked_auth_required"
    if status == "blocked_credentials":
        return "parked_credentials"
    if status == "provider_backoff":
        return "parked_rate_limit"
    if status == "dependency_missing":
        return "parked_pending_environment"
    if status == "failed_transient":
        return "failed_transient"
    return fallback


def _terminal_without_form(submission_outcome: str) -> bool:
    return submission_outcome.startswith("filtered_") or submission_outcome in {
        "liveness_expired",
        "parked_robot_detection",
        "parked_auth_required",
        "parked_credentials",
        "parked_rate_limit",
        "parked_pending_environment",
        "failed_transient",
        "no_jobs_found",
        "manual_review_required",
        "parked_external_interstitial",
    }


def _filtered_outcome_from_review(decision_payload: dict[str, Any], classifier_threshold_decision: str) -> str:
    outcome = str(decision_payload.get("submission_outcome") or "")
    if outcome.startswith("filtered_"):
        return outcome
    if outcome in {"liveness_expired"}:
        return outcome
    if classifier_threshold_decision == "fail":
        return "filtered_low_score"
    return "not_submitted"


def _append_classifier_agent_signal(
    *,
    listing: Any,
    description: str,
    score: float,
    sut_decision: str,
    decision_payload: dict[str, Any],
) -> None:
    try:
        rule_b = decision_payload.get("rule_b") if isinstance(decision_payload.get("rule_b"), dict) else {}
        matched = rule_b.get("matched_expertise_area")
        regions_matched = [str(matched)] if matched else []
        ClassifierFeedbackStore().append_agent_signal(
            job_url=listing.url,
            jd_text=description,
            candidate_facts={
                "experience_years_professional_product": decision_payload.get("experience_years_professional_product"),
                "missing_structured_fields": decision_payload.get("missing_structured_fields", []),
                "fact_sources": decision_payload.get("fact_sources", {}),
            },
            agent_decision=str(decision_payload.get("submission_outcome") or ("pass" if decision_payload.get("fit") else "fail")),
            agent_reasoning=decision_payload,
            sut_score=score,
            sut_decision=sut_decision,
            title=listing.title_preview,
            company=listing.company,
            regions_matched=regions_matched,
            jd_min_years=decision_payload.get("jd_min_years"),
        )
    except Exception as exc:
        log.warning("classifier_agent_signal_append_failed", job_url=getattr(listing, "url", None), error=str(exc))


def _sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _should_reprocess_existing_application(existing: dict[str, Any]) -> bool:
    if bool(existing.get("submitted")):
        return False
    error = str(existing.get("error") or "").lower()
    if "application form automation is not supported" in error or "unsupported_site" in error:
        return True
    decision = str(existing.get("decision") or "").lower()
    outcome = str(existing.get("submission_outcome") or "").lower()
    return decision in {
        "manual_review_required",
        "failed_transient",
        "provider_backoff",
        "error",
    } or outcome in {"manual_review_required", "error"}


def _tagline_for_role(role: str, description: str) -> str:
    clean_role = role.split(" - ")[0].strip() or "Software Engineer"
    text = description.lower()
    if "postgres" in text and "python" in text:
        return f"{clean_role} | Python & PostgreSQL | Distributed Systems"
    if "react" in text or "typescript" in text:
        return f"{clean_role} | TypeScript & React | Full-Stack Systems"
    return f"{clean_role} | Backend Systems | Product Engineering"


def _retarget_profile(profile: str, role: str, company: str | None) -> str:
    if not profile:
        return profile
    out = profile.replace("Senior Backend Developer", role)
    if company:
        out = out.replace("Tech Innovators Inc.", company).replace("Tech Innovators", company)
    return out
