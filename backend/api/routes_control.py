from __future__ import annotations

from uuid import uuid4
import os
import shutil

from pydantic import BaseModel, HttpUrl
from fastapi import APIRouter, HTTPException, Request
from backend import config
from backend.config import ANALYTICS_LOOKBACK_DAYS, DATA_DIR, OUTPUT_DIR
from backend.storage.learned_answers import list_pending_questions, resolve_pending_question

router = APIRouter()


class StartRunRequest(BaseModel):
    career_url: HttpUrl
    limit: int | None = None
    force_reprocess: bool = False
    bypass_classifier: bool = False


class StartRunResponse(BaseModel):
    run_id: int
    correlation_id: str


class StopRunResponse(BaseModel):
    ok: bool
    correlation_id: str | None = None


class GapFillRequest(BaseModel):
    question: str
    answer: str


class ApprovalRequest(BaseModel):
    token: str
    approved: bool
    field_answers: list[dict] | None = None
    reason: str | None = None
    checks: dict | None = None


class ClassifierReviewRequest(BaseModel):
    token: str
    passed: bool
    decision_payload: dict | None = None


class ManualTakeoverRequest(BaseModel):
    token: str
    action: str
    registered_button: dict | None = None


class LiveSubmitRequest(BaseModel):
    enabled: bool


class AutoSubmitRequest(BaseModel):
    enabled: bool


class ClassifierAutoPassRequest(BaseModel):
    enabled: bool


class LiveModeRequest(BaseModel):
    enabled: bool


class GapReadRequest(BaseModel):
    question: str


class PendingQuestionAnswerRequest(BaseModel):
    answer: str


@router.post("/browser/focus")
async def focus_browser(request: Request) -> dict:
    page = getattr(request.app.state.alarm, "active_page", None)
    browser = getattr(request.app.state, "browser", None)
    if browser is None:
        return {"ok": False, "url": None}
    return await browser.focus_page(page)


@router.post("/browser/open_external")
async def open_external_browser(request: Request) -> dict:
    page = getattr(request.app.state.alarm, "active_page", None)
    browser = getattr(request.app.state, "browser", None)
    url = getattr(page, "url", None)
    if browser is None:
        return {"ok": False, "url": url}
    return await browser.open_in_default_browser(url)


@router.post("/run/start")
async def start_run(body: StartRunRequest, request: Request) -> StartRunResponse:
    try:
        correlation_id = getattr(request.state, "correlation_id", None) or str(uuid4())
        run_id = await request.app.state.orch.start(
            str(body.career_url),
            body.limit,
            correlation_id=correlation_id,
            force_reprocess=body.force_reprocess,
            bypass_classifier=body.bypass_classifier,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return StartRunResponse(run_id=run_id, correlation_id=correlation_id)


@router.post("/run/stop")
async def stop_run(request: Request) -> StopRunResponse:
    await request.app.state.orch.stop()
    return StopRunResponse(ok=True, correlation_id=getattr(request.state, "correlation_id", None))


@router.get("/status")
async def status(request: Request) -> dict:
    return request.app.state.orch.status(correlation_id=getattr(request.state, "correlation_id", None))


@router.get("/doctor")
async def doctor(request: Request) -> dict:
    sqlite = request.app.state.sqlite
    browser = getattr(request.app.state, "browser", None)
    mongo = getattr(request.app.state, "mongo", None)
    verify_result = sqlite.verify_integrity()
    browser_cli_available = shutil.which("playwright") is not None
    return {
        "config": _doctor_config_payload(),
        "verify": verify_result,
        "preflight": {
            "data_dir_writable": os.access(DATA_DIR, os.W_OK),
            "output_dir_writable": os.access(OUTPUT_DIR, os.W_OK),
            "browser_available": browser is not None and browser_cli_available,
            "browser_context_open": browser is not None and getattr(browser, "browser", None) is not None,
            "browser_cli_available": browser_cli_available,
            "pdflatex_available": shutil.which("pdflatex") is not None,
            "translator_available": _translator_available(),
            "mongo_enabled": mongo is not None and getattr(mongo, "enabled", lambda: False)(),
        },
        "can_run": verify_result.get("critical_failure_count", 0) == 0,
        "maintenance_preview": {"retention_pending_days": config.RETENTION_PENDING_DAYS},
        "correlation_id": getattr(request.state, "correlation_id", None),
    }


def _doctor_config_payload() -> dict:
    payload = config.validate_config()
    if payload.get("mongo_uri"):
        payload["mongo_uri"] = "<redacted>"
    return payload


def _translator_available() -> bool:
    try:
        from backend.specialists.translator import Translator

        return Translator.available()
    except Exception:
        return False


@router.post("/verify")
async def verify(request: Request) -> dict:
    return request.app.state.sqlite.verify_integrity() | {"correlation_id": getattr(request.state, "correlation_id", None)}


@router.get("/analytics")
async def analytics(request: Request, lookback_days: int = ANALYTICS_LOOKBACK_DAYS) -> dict:
    return request.app.state.sqlite.analytics(lookback_days=lookback_days)


@router.get("/applications")
async def applications(request: Request, limit: int = 100, offset: int = 0) -> list[dict]:
    local_rows = request.app.state.sqlite.list_applications(limit=limit, offset=offset)
    mongo = getattr(request.app.state, "mongo", None)
    if mongo is not None and getattr(mongo, "enabled", lambda: False)():
        mongo_rows = mongo.list_applications(limit=limit, offset=offset)
        return _merge_application_rows(local_rows, mongo_rows)[:limit]
    return local_rows


def _merge_application_rows(local_rows: list[dict], mongo_rows: list[dict]) -> list[dict]:
    """Return Mongo's minimal ledger enriched with local per-mode attempt state.

    MongoDB intentionally stores only the small canonical application record.
    The menu-bar History UI still needs transient attempt fields such as
    dry_run_outcome and real_submit_outcome so red rows can be retried and green
    rows are locked per mode. When both stores have a row, prefer the local row
    because it carries the richer current attempt state; append Mongo-only rows
    so older canonical history remains visible.
    """
    merged: dict[str, dict] = {}
    for row in mongo_rows:
        job_url = str(row.get("job_url") or "")
        if job_url:
            merged[job_url] = dict(row)
    for row in local_rows:
        job_url = str(row.get("job_url") or "")
        if not job_url:
            continue
        base = merged.get(job_url, {})
        enriched = {**base, **row}
        for key, value in base.items():
            if enriched.get(key) in (None, "") and value not in (None, ""):
                enriched[key] = value
        merged[job_url] = enriched
    return list(merged.values())


@router.delete("/applications")
async def delete_application(job_url: str, request: Request) -> dict:
    mongo = getattr(request.app.state, "mongo", None)
    mongo_ok = False
    if mongo is not None and getattr(mongo, "enabled", lambda: False)():
        mongo_ok = mongo.delete_application(job_url)
    ok = request.app.state.sqlite.delete_application(job_url) or mongo_ok
    if not ok:
        raise HTTPException(status_code=404, detail=f"Application not found: {job_url}")
    return {"ok": True}


@router.post("/history/clear")
async def clear_history(request: Request) -> dict:
    counts = request.app.state.sqlite.clear_history()
    mongo = getattr(request.app.state, "mongo", None)
    if mongo is not None and getattr(mongo, "enabled", lambda: False)():
        counts["mongo"] = mongo.clear_application_history()
    for gate_name in ("alarm", "approval", "classifier_review", "manual_takeover"):
        gate = getattr(request.app.state, gate_name, None)
        if gate is None:
            continue
        if hasattr(gate, "cancel_all"):
            gate.cancel_all(reason="history_cleared")
            continue
        pending = getattr(gate, "pending", None)
        future = getattr(pending, "future", None)
        if future is not None and not future.done():
            future.cancel()
        if hasattr(gate, "pending"):
            gate.pending = None
    return {"ok": True, "deleted": counts}


@router.get("/runs")
async def runs(request: Request, limit: int = 50) -> list[dict]:
    return request.app.state.sqlite.list_runs(limit=limit)


@router.get("/pending_actions")
async def pending_actions(request: Request) -> dict:
    persisted = request.app.state.sqlite.list_pending_actions()
    by_type = {
        "alarm": [item for item in persisted if item.get("action_type") == "alarm"],
        "approval": [item for item in persisted if item.get("action_type") == "approval"],
        "classifier_review": [item for item in persisted if item.get("action_type") == "classifier_review"],
        "manual_takeover": [item for item in persisted if item.get("action_type") == "manual_takeover"],
    }
    return {
        "alarms": by_type["alarm"] or request.app.state.alarm.snapshot(),
        "classifier_reviews": by_type["classifier_review"] or request.app.state.classifier_review.snapshot(),
        "approvals": by_type["approval"] or request.app.state.approval.snapshot(),
        "manual_takeovers": by_type["manual_takeover"] or request.app.state.manual_takeover.snapshot(),
        "summary": {
            "total": len(persisted),
            "approvals": len(by_type["approval"]),
            "classifier_reviews": len(by_type["classifier_review"]),
            "manual_takeovers": len(by_type["manual_takeover"]),
        },
    }


@router.get("/pending-questions")
async def pending_questions(request: Request) -> dict:
    return {"items": list_pending_questions(request.app.state.sqlite)}


@router.post("/pending-questions/{question_id}/answer")
async def answer_pending_question(question_id: int, body: PendingQuestionAnswerRequest, request: Request) -> dict:
    resolved = resolve_pending_question(question_id, body.answer, request.app.state.sqlite)
    if not resolved:
        raise HTTPException(status_code=404, detail=f"Pending question not found: {question_id}")
    return {"item": resolved}


@router.get("/learned-answers")
async def learned_answers(request: Request) -> dict:
    return {"items": request.app.state.sqlite.list_learned_answers()}


@router.delete("/learned-answers/{answer_id}")
async def delete_learned_answer(answer_id: int, request: Request) -> dict:
    ok = request.app.state.sqlite.delete_learned_answer(answer_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Learned answer not found: {answer_id}")
    return {"ok": True}


@router.get("/logs")
async def logs(max_chars: int = 120000) -> dict:
    from backend.config import DATA_DIR

    # Clamp to a sane range to avoid accidental huge reads.
    max_chars = max(1024, min(int(max_chars), 2_000_000))
    logs_dir = DATA_DIR / "logs"
    files = {
        "stdout": logs_dir / "menubar-backend.stdout.log",
        "stderr": logs_dir / "menubar-backend.stderr.log",
    }
    payload: dict[str, str] = {}
    for name, path in files.items():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            payload[name] = text[-max_chars:]
        except FileNotFoundError:
            payload[name] = ""
        except Exception as exc:
            payload[name] = f"[error reading log: {exc}]"
    return {"logs_dir": str(logs_dir), **payload}


@router.post("/logs/clear")
async def clear_logs() -> dict[str, bool]:
    from backend.config import DATA_DIR

    logs_dir = DATA_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    for path in [
        logs_dir / "menubar-backend.stdout.log",
        logs_dir / "menubar-backend.stderr.log",
    ]:
        path.write_text("", encoding="utf-8")
    return {"ok": True}


@router.post("/run/retry")
async def retry_run(body: StartRunRequest, request: Request) -> dict[str, int]:
    try:
        run_id = await request.app.state.orch.start(
            str(body.career_url),
            body.limit,
            force_reprocess=True,
            bypass_classifier=True,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"run_id": run_id}


@router.post("/gap/fill")
async def fill_gap(body: GapFillRequest, request: Request) -> dict[str, bool]:
    alarm = request.app.state.alarm
    pending = alarm.pending
    if pending and pending.question == body.question:
        from backend.storage.learned_answers import store_learned_answer
        field_type = pending.details.get("field_type", "unknown")
        store_learned_answer(body.question, field_type, body.answer, request.app.state.sqlite)
    ok = alarm.fill(body.question, body.answer)
    return {"ok": ok}


@router.post("/gap/read_browser")
async def gap_read_browser(body: GapReadRequest, request: Request) -> dict:
    """Read whatever the user just typed into the focused field in the live browser
    and use it as the alarm answer, so the app can learn from that manual input."""
    alarm = request.app.state.alarm
    value = await alarm.read_from_browser()
    if value is None:
        return {"ok": False, "value": None, "reason": "No focused input in the browser window."}
    
    pending = alarm.pending
    if pending and pending.question == body.question:
        from backend.storage.learned_answers import store_learned_answer
        field_type = pending.details.get("field_type", "unknown")
        store_learned_answer(body.question, field_type, value, request.app.state.sqlite)
        
    ok = alarm.fill(body.question, value)
    return {"ok": ok, "value": value}


@router.post("/approval/respond")
async def approval_respond(body: ApprovalRequest, request: Request) -> dict[str, bool]:
    ok = request.app.state.approval.respond(body.token, body.approved, body.field_answers, reason=body.reason, checks=body.checks)
    return {"ok": ok}


@router.post("/classifier/respond")
async def classifier_respond(body: ClassifierReviewRequest, request: Request) -> dict[str, bool]:
    ok = request.app.state.classifier_review.respond(body.token, body.passed, body.decision_payload)
    return {"ok": ok}


@router.post("/manual/respond")
async def manual_respond(body: ManualTakeoverRequest, request: Request) -> dict[str, bool]:
    ok = request.app.state.manual_takeover.respond(body.token, body.action, body.registered_button)
    return {"ok": ok}


def _settings_payload() -> dict:
    payload = config.runtime_settings_payload()
    payload["live_mode_enabled"] = payload["live_mode"]
    payload["auto_submit_without_approval"] = payload["auto_submit_without_approval"]
    payload["final_review_required"] = not payload["auto_submit_without_approval"]
    return payload


@router.get("/settings")
async def get_settings(request: Request) -> dict:
    return _settings_payload() | {"correlation_id": getattr(request.state, "correlation_id", None)}


async def _publish_config_change(request: Request, setting_name: str, enabled: bool) -> None:
    await request.app.state.stream.publish(
        "config",
        {
            "type": "config_changed",
            "event_type": "config_changed",
            "stage": "settings",
            "outcome": "updated",
            "error_code": None,
            "correlation_id": getattr(request.state, "correlation_id", None),
            "setting": setting_name,
            "enabled": enabled,
            "settings": _settings_payload(),
        },
    )


@router.put("/settings/live_submit")
async def set_live_submit(body: LiveSubmitRequest, request: Request) -> dict:
    config.set_live_submit_enabled(body.enabled)
    await _publish_config_change(request, "live_submit_enabled", body.enabled)
    return _settings_payload() | {"correlation_id": getattr(request.state, "correlation_id", None)}


@router.put("/settings/auto_submit")
async def set_auto_submit(body: AutoSubmitRequest, request: Request) -> dict:
    config.set_auto_submit_without_approval(body.enabled)
    await _publish_config_change(request, "auto_submit_without_approval", body.enabled)
    return _settings_payload() | {"correlation_id": getattr(request.state, "correlation_id", None)}


@router.put("/settings/classifier_auto_pass")
async def set_classifier_auto_pass(body: ClassifierAutoPassRequest, request: Request) -> dict:
    config.set_classifier_auto_pass_enabled(body.enabled)
    await _publish_config_change(request, "classifier_auto_pass_when_above_threshold", body.enabled)
    return _settings_payload() | {"correlation_id": getattr(request.state, "correlation_id", None)}


@router.put("/settings/live_mode")
async def set_live_mode(body: LiveModeRequest, request: Request) -> dict:
    """Toggle Watch Browser.

    OFF means the automator still runs, but future browser sessions are headless.
    ON means future sessions are visible and can be brought to the front.
    """
    config.set_live_mode_enabled(body.enabled)
    orch = getattr(request.app.state, "orch", None)
    browser = getattr(request.app.state, "browser", None)
    try:
        page = getattr(request.app.state.alarm, "active_page", None)
        if body.enabled and page is not None:
            await page.bring_to_front()
    except Exception:
        pass
    if browser is not None and not bool(getattr(orch, "running", lambda: False)()):
        try:
            await browser.close()
        except Exception:
            pass
    await _publish_config_change(request, "live_mode_enabled", body.enabled)
    return _settings_payload() | {"correlation_id": getattr(request.state, "correlation_id", None)}
