from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import structlog

from backend.config import MONGO_DB, MONGO_URI

log = structlog.get_logger()

APPLICATION_COLLECTION = "applications"
APPLICATION_FIELDS = (
    "company_name",
    "application_date",
    "job_description_url",
    "role",
    "location",
    "application_type",
    "resume_latex_code",
)


@dataclass
class MongoStore:
    client: object | None = None
    db_name: str = MONGO_DB
    bucket_name: str = "application_files"
    _resolved_db_name: str | None = field(default=None, init=False, repr=False)

    @classmethod
    def from_env(cls) -> "MongoStore":
        if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("JOBPILOT_USE_REAL_MONGO") != "1":
            return cls(None)
        if not MONGO_URI:
            return cls(None)
        from pymongo import MongoClient

        return cls(MongoClient(MONGO_URI), MONGO_DB)

    def enabled(self) -> bool:
        return self.client is not None

    def _db(self):
        return self.client[self._effective_db_name()]

    def _effective_db_name(self) -> str:
        if self._resolved_db_name:
            return self._resolved_db_name
        resolved = self.db_name
        try:
            names = getattr(self.client, "list_database_names")()
            for name in names:
                if str(name).lower() == self.db_name.lower():
                    resolved = str(name)
                    if resolved != self.db_name:
                        log.info("mongo_db_case_resolved", configured=self.db_name, existing=resolved)
                    break
        except Exception as exc:
            log.debug("mongo_db_case_probe_failed", db_name=self.db_name, error=str(exc))
        self._resolved_db_name = resolved
        return resolved

    def record_application(self, document: dict[str, Any]) -> None:
        if not self.enabled():
            return
        payload = minimal_application_document(document)
        self._db()[APPLICATION_COLLECTION].replace_one(
            {"job_description_url": payload["job_description_url"]},
            payload,
            upsert=True,
        )

    def list_applications(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        if not self.enabled():
            return []
        docs = list(self._db()[APPLICATION_COLLECTION].find({}, {"_id": False}))
        docs.sort(
            key=lambda doc: (
                doc.get("application_date") or "",
                doc.get("company_name") or "",
                doc.get("role") or "",
            ),
            reverse=True,
        )
        return [_to_ui_application_row(doc) for doc in docs[offset : offset + limit]]

    def get_application(self, job_url: str) -> dict[str, Any] | None:
        if not self.enabled():
            return None
        doc = self._db()[APPLICATION_COLLECTION].find_one({"job_description_url": job_url}, {"_id": False})
        return _to_ui_application_row(doc) if doc else None

    def delete_application(self, job_url: str) -> bool:
        if not self.enabled():
            return False
        result = self._db()[APPLICATION_COLLECTION].delete_one({"job_description_url": job_url})
        return bool(getattr(result, "deleted_count", 0))

    def clear_application_history(self) -> dict[str, int]:
        if not self.enabled():
            return {}
        db = self._db()
        deleted: dict[str, int] = {}
        for collection in (APPLICATION_COLLECTION, f"{self.bucket_name}.files", f"{self.bucket_name}.chunks"):
            result = db[collection].delete_many({})
            deleted[collection] = int(getattr(result, "deleted_count", 0))
        return deleted

    def application_counts(self) -> dict[str, int]:
        if not self.enabled():
            return {"today": 0, "week": 0, "all_time": 0}
        docs = list(self._db()[APPLICATION_COLLECTION].find({"application_date": {"$ne": None}}, {"application_date": True}))
        today = date.today()
        week_start = today - timedelta(days=7)
        counts = {"today": 0, "week": 0, "all_time": 0}
        for doc in docs:
            parsed = _parse_application_date(doc.get("application_date"))
            if parsed is None:
                continue
            counts["all_time"] += 1
            if parsed >= week_start:
                counts["week"] += 1
            if parsed == today:
                counts["today"] += 1
        return counts


def minimal_application_document(document: dict[str, Any]) -> dict[str, Any]:
    submitted = bool(document.get("submitted"))
    job_url = _clean_text(document.get("job_description_url") or document.get("job_url"))
    application_date = document.get("application_date")
    if not application_date and _should_mark_applied(document):
        application_date = _utcdate()
    payload = {
        "company_name": _clean_text(document.get("company_name") or document.get("company")) or _company_from_url(job_url),
        "application_date": application_date or (_utcdate() if submitted else None),
        "job_description_url": job_url,
        "role": _clean_text(document.get("role") or document.get("title")),
        "location": _clean_text(document.get("location")),
        "application_type": _application_type(
            document.get("application_type"),
            document.get("role") or document.get("title"),
            document.get("description") or document.get("job_description_text"),
        ),
        "resume_latex_code": str(document.get("resume_latex_code") or ""),
    }
    return {field: payload.get(field) for field in APPLICATION_FIELDS}


def _to_ui_application_row(document: dict[str, Any]) -> dict[str, Any]:
    applied = bool(document.get("application_date"))
    job_url = document.get("job_description_url")
    return {
        "company": document.get("company_name") or _company_from_url(job_url),
        "title": document.get("role"),
        "job_url": job_url,
        "applied_at": document.get("application_date"),
        "submitted": 1 if applied else 0,
        "decision": "pass" if applied else "human_fail",
        "error": None if applied else "Rejected or not applied yet",
        "resume_path": None,
        "cover_letter_path": None,
        "classifier_score": None,
        "location": document.get("location"),
        "application_type": document.get("application_type"),
        "resume_latex_code": document.get("resume_latex_code"),
    }


def _utcdate() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _company_from_url(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    segments = [segment for segment in parsed.path.split("/") if segment]

    path_first_hosts = (
        "greenhouse.io",
        "workable.com",
        "ashbyhq.com",
        "lever.co",
        "smartrecruiters.com",
    )
    if any(host == suffix or host.endswith(f".{suffix}") for suffix in path_first_hosts):
        for segment in segments:
            lowered = segment.lower()
            if lowered not in {"jobs", "job", "careers", "career", "apply", "en-us", "en"} and not lowered.isdigit():
                return _humanize_company_slug(segment)

    labels = [label for label in host.split(".") if label]
    common_prefixes = {
        "www",
        "jobs",
        "careers",
        "apply",
        "boards",
        "job-boards",
        "greenhouse",
        "workable",
        "ashbyhq",
        "lever",
        "smartrecruiters",
    }
    for label in labels:
        if label in common_prefixes or label.startswith("wd"):
            continue
        if label in {"com", "io", "co", "org", "net"}:
            continue
        return _humanize_company_slug(label)
    return None


def _humanize_company_slug(value: str) -> str | None:
    text = value.strip().strip("/")
    if not text:
        return None
    for suffix in ("-inc", "_inc", ".inc", "-llc", "_llc", ".llc", "-ltd", "_ltd", ".ltd"):
        if text.lower().endswith(suffix):
            text = text[: -len(suffix)]
            break
    words = [word for word in text.replace("_", "-").replace(".", "-").split("-") if word]
    if not words:
        return None
    return " ".join(word.upper() if len(word) <= 3 else word[:1].upper() + word[1:] for word in words)


def _application_type(value: Any, title: Any, description: Any) -> str:
    explicit = _clean_text(value)
    if explicit:
        lowered = explicit.lower()
        if "intern" in lowered:
            return "Intern"
        if "full" in lowered:
            return "Full time"
        return explicit
    text = f"{title or ''}\n{description or ''}".lower()
    return "Intern" if any(token in text for token in ("intern", "internship", "co-op", "coop")) else "Full time"


def _should_mark_applied(document: dict[str, Any]) -> bool:
    if bool(document.get("submitted")):
        return True
    decision = str(document.get("decision") or "").lower()
    error = _clean_text(document.get("error"))
    outcome = str(document.get("submission_outcome") or "").lower()
    if error:
        return False
    if decision != "pass":
        return False
    return outcome not in {
        "manual_review_required",
        "manual_auth_required",
        "blocked_credentials",
        "provider_backoff",
        "failed_transient",
        "submission_error",
        "error",
        "classifier_rejected",
        "liveness_expired",
    }


def _parse_application_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
