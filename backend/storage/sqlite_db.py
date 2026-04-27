from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from contextlib import suppress
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from rapidfuzz import fuzz

from backend.config import BACKUP_BEFORE_MIGRATION, DATA_DIR, DEDUP_THRESHOLD, RETENTION_PENDING_DAYS
from backend.contracts import (
    CanonicalDecision,
    ListingLifecycle,
    RunLifecycle,
    can_transition_listing_state,
    decision_rank,
    merge_status_values,
    normalize_decision,
    normalize_submission_outcome,
    unknown_alias_warning,
)


SCHEMA_VERSION = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS site_limits (
  domain TEXT PRIMARY KEY,
  daily_limit INTEGER,
  applied_today INTEGER NOT NULL DEFAULT 0,
  last_reset_date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_url TEXT UNIQUE NOT NULL,
  job_id_ext TEXT,
  company TEXT,
  title TEXT,
  location TEXT,
  classifier_score REAL,
  decision TEXT,
  canonical_status TEXT,
  canonical_rank INTEGER NOT NULL DEFAULT 0,
  applied_at TEXT,
  submitted INTEGER NOT NULL DEFAULT 0,
  submission_outcome TEXT,
  resume_path TEXT,
  cover_letter_path TEXT,
  provenance_json TEXT,
  error TEXT,
  liveness_state TEXT,
  liveness_reasons_json TEXT,
  duplicate_of_job_url TEXT,
  duplicate_reason_code TEXT,
  -- Per-mode attempt tracking. The History UI splits attempts into a DRY RUN
  -- and a REAL SUBMITS section; an entry is "green" in a section only when its
  -- per-mode outcome is success and the per-mode error is cleared. The Apply
  -- button stays enabled while the per-mode entry is red so the user can retry.
  dry_run_outcome TEXT,
  dry_run_completed_at TEXT,
  dry_run_error TEXT,
  real_submit_outcome TEXT,
  real_submit_completed_at TEXT,
  real_submit_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_applications_applied_at ON applications(applied_at);
CREATE INDEX IF NOT EXISTS idx_applications_decision ON applications(decision);
CREATE TABLE IF NOT EXISTS job_cache (
  url TEXT PRIMARY KEY,
  description_text TEXT NOT NULL,
  scraped_at TEXT NOT NULL,
  embedding BLOB
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  career_page_url TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  jobs_seen INTEGER DEFAULT 0,
  jobs_passed INTEGER DEFAULT 0,
  jobs_applied INTEGER DEFAULT 0,
  skip_reasons_json TEXT DEFAULT '{}',
  soft_fail_count INTEGER DEFAULT 0,
  hard_fail_count INTEGER DEFAULT 0,
  browser_open INTEGER DEFAULT 0,
  summary_artifact_path TEXT,
  status TEXT
);
CREATE TABLE IF NOT EXISTS listing_runs (
  run_id INTEGER NOT NULL,
  job_url TEXT NOT NULL,
  state TEXT NOT NULL,
  retry_count INTEGER NOT NULL DEFAULT 0,
  last_error_code TEXT,
  last_error_message TEXT,
  checkpoint_json TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(run_id, job_url)
);
CREATE TABLE IF NOT EXISTS pending_actions (
  token TEXT PRIMARY KEY,
  action_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  correlation_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_actions_type ON pending_actions(action_type);
CREATE TABLE IF NOT EXISTS learned_answers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label_normalized TEXT UNIQUE NOT NULL,
  classification TEXT NOT NULL,
  answer TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label_normalized TEXT NOT NULL,
  classification TEXT NOT NULL,
  job_id TEXT,
  job_title TEXT,
  company TEXT,
  user_answer TEXT,
  resolved INTEGER DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(label_normalized, job_id)
);
CREATE TABLE IF NOT EXISTS duplicate_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_url TEXT NOT NULL,
  duplicate_of_job_url TEXT NOT NULL,
  similarity_score REAL NOT NULL,
  reason_code TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER,
  job_url TEXT,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS translation_cache (
  src TEXT NOT NULL,
  dst TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  translated_text TEXT NOT NULL,
  translator TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(src, dst, text_hash)
);
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


class SQLiteStore:
    def __init__(self, path: Path | None = None):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.path = path or DATA_DIR / "jobpilot.db"
        self._lock = threading.RLock()
        self.conn = self._connect()
        try:
            with self._lock:
                self.conn.executescript(SCHEMA)
                self._migrate()
                self.conn.commit()
        except (sqlite3.DatabaseError, sqlite3.OperationalError):
            self.close()
            self._quarantine_corrupt_database()
            self.conn = self._connect()
            with self._lock:
                self.conn.executescript(SCHEMA)
                self._migrate()
                self.conn.commit()

    def _connect(self) -> sqlite3.Connection:
        if BACKUP_BEFORE_MIGRATION and self.path.exists():
            backup = self.path.with_suffix(f".v{SCHEMA_VERSION}.bak")
            if not backup.exists():
                shutil.copy2(self.path, backup)
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
        except Exception:
            pass
        return conn

    def _quarantine_corrupt_database(self) -> None:
        if not self.path.exists():
            return
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        quarantine = self.path.with_suffix(f".corrupt.{timestamp}.bak")
        try:
            shutil.move(str(self.path), str(quarantine))
        except Exception:
            quarantine = self.path.with_suffix(f".corrupt.{timestamp}.bak")
            shutil.copy2(self.path, quarantine)
            self.path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{self.path}{suffix}")
            if sidecar.exists():
                with suppress(Exception):
                    sidecar.unlink()

    def _migrate(self) -> None:
        current = self.schema_version()
        if current >= SCHEMA_VERSION:
            return
        app_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(applications)").fetchall()}
        run_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(runs)").fetchall()}
        for name, ddl in {
            "canonical_status": "ALTER TABLE applications ADD COLUMN canonical_status TEXT",
            "canonical_rank": "ALTER TABLE applications ADD COLUMN canonical_rank INTEGER NOT NULL DEFAULT 0",
            "liveness_state": "ALTER TABLE applications ADD COLUMN liveness_state TEXT",
            "liveness_reasons_json": "ALTER TABLE applications ADD COLUMN liveness_reasons_json TEXT",
            "duplicate_of_job_url": "ALTER TABLE applications ADD COLUMN duplicate_of_job_url TEXT",
            "duplicate_reason_code": "ALTER TABLE applications ADD COLUMN duplicate_reason_code TEXT",
            "updated_at": "ALTER TABLE applications ADD COLUMN updated_at TEXT",
            "dry_run_outcome": "ALTER TABLE applications ADD COLUMN dry_run_outcome TEXT",
            "dry_run_completed_at": "ALTER TABLE applications ADD COLUMN dry_run_completed_at TEXT",
            "dry_run_error": "ALTER TABLE applications ADD COLUMN dry_run_error TEXT",
            "real_submit_outcome": "ALTER TABLE applications ADD COLUMN real_submit_outcome TEXT",
            "real_submit_completed_at": "ALTER TABLE applications ADD COLUMN real_submit_completed_at TEXT",
            "real_submit_error": "ALTER TABLE applications ADD COLUMN real_submit_error TEXT",
        }.items():
            if name not in app_columns:
                self.conn.execute(ddl)
        for name, ddl in {
            "skip_reasons_json": "ALTER TABLE runs ADD COLUMN skip_reasons_json TEXT DEFAULT '{}'",
            "soft_fail_count": "ALTER TABLE runs ADD COLUMN soft_fail_count INTEGER DEFAULT 0",
            "hard_fail_count": "ALTER TABLE runs ADD COLUMN hard_fail_count INTEGER DEFAULT 0",
            "browser_open": "ALTER TABLE runs ADD COLUMN browser_open INTEGER DEFAULT 0",
            "summary_artifact_path": "ALTER TABLE runs ADD COLUMN summary_artifact_path TEXT",
        }.items():
            if name not in run_columns:
                self.conn.execute(ddl)
        rows = self.conn.execute(
            "SELECT job_url, decision, submission_outcome, created_at, updated_at FROM applications"
        ).fetchall()
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_applications_canonical_status ON applications(canonical_status)")
        now = _now()
        for row in rows:
            decision = normalize_decision(row["decision"]).value
            outcome = normalize_submission_outcome(row["submission_outcome"]).value
            self.conn.execute(
                """
                UPDATE applications
                SET decision=?, canonical_status=?, canonical_rank=?, submission_outcome=?, updated_at=COALESCE(updated_at, ?, created_at)
                WHERE job_url=?
                """,
                (decision, decision, decision_rank(decision), outcome, now, row["job_url"]),
            )
        self.conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self.conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('last_migration_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_now(),),
        )

    def schema_version(self) -> int:
        row = self.conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        return int(row["value"]) if row else 0

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def create_run(self, career_page_url: str) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO runs(career_page_url, started_at, status) VALUES (?, ?, ?)",
                (career_page_url, _now(), RunLifecycle.RUNNING.value),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def update_run(self, run_id: int, **values) -> None:
        if not values:
            return
        assignments = ", ".join(f"{key}=?" for key in values)
        with self._lock:
            self.conn.execute(f"UPDATE runs SET {assignments} WHERE id=?", (*values.values(), run_id))
            self.conn.commit()

    def finish_run(self, run_id: int, status: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE runs SET ended_at=?, status=? WHERE id=?", (_now(), status, run_id))
            self.conn.commit()

    def reconcile_orphan_runs(self) -> int:
        with self._lock:
            now = _now()
            cur = self.conn.execute(
                "UPDATE runs SET status=?, ended_at=COALESCE(ended_at, ?) WHERE status IN ('running','starting','stopping')",
                (RunLifecycle.STOPPED.value, now),
            )
            self.conn.execute(
                "UPDATE listing_runs SET state=?, updated_at=? WHERE state=?",
                (ListingLifecycle.PENDING.value, now, ListingLifecycle.IN_PROGRESS.value),
            )
            self.conn.commit()
            return cur.rowcount or 0

    def record_listing_state(self, run_id: int, job_url: str, *, state: str, retry_count: int = 0, checkpoint: dict | None = None, error_code: str | None = None, error_message: str | None = None) -> None:
        with self._lock:
            existing = self.conn.execute(
                "SELECT state, retry_count FROM listing_runs WHERE run_id=? AND job_url=?",
                (run_id, job_url),
            ).fetchone()
            if existing and not can_transition_listing_state(existing["state"], state):
                raise ValueError(f"invalid listing transition {existing['state']} -> {state}")
            self.conn.execute(
                """
                INSERT INTO listing_runs(run_id, job_url, state, retry_count, last_error_code, last_error_message, checkpoint_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, job_url) DO UPDATE SET
                  state=excluded.state,
                  retry_count=excluded.retry_count,
                  last_error_code=excluded.last_error_code,
                  last_error_message=excluded.last_error_message,
                  checkpoint_json=excluded.checkpoint_json,
                  updated_at=excluded.updated_at
                """,
                (run_id, job_url, state, retry_count, error_code, error_message, json.dumps(checkpoint or {}), _now()),
            )
            self.conn.commit()

    def list_resume_candidates(self, run_id: int) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT job_url, state, retry_count, last_error_code, last_error_message, checkpoint_json
                FROM listing_runs
                WHERE run_id=? AND state IN (?, ?)
                ORDER BY updated_at ASC
                """,
                (run_id, ListingLifecycle.PENDING.value, ListingLifecycle.FAILED_TRANSIENT.value),
            ).fetchall()
        return [dict(row) for row in rows]

    def has_application(self, job_url: str) -> bool:
        with self._lock:
            return self.conn.execute("SELECT 1 FROM applications WHERE job_url=?", (job_url,)).fetchone() is not None

    def get_application(self, job_url: str) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM applications WHERE job_url=?", (job_url,)).fetchone()
        return dict(row) if row else None

    def successful_attempt_mode(self, job_url: str, mode: str) -> bool:
        row = self.get_application(job_url)
        if not row:
            return False
        mode = (mode or "").strip().lower()
        if mode == "dry_run":
            outcome = str(row.get("dry_run_outcome") or "").lower()
            error = str(row.get("dry_run_error") or "").strip()
            return outcome in {"dry_run_complete", "completed_with_deferred"} and not error
        if mode == "real_submit":
            outcome = str(row.get("real_submit_outcome") or "").lower()
            error = str(row.get("real_submit_error") or "").strip()
            return outcome in {"submitted", "submitted_unconfirmed"} and not error
        return False

    def semantic_duplicate_candidate_lookup(self, *, company: str | None, title: str | None, location: str | None) -> list[dict]:
        normalized_company = _normalize_company(company)
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT job_url, company, title, location, decision, canonical_status, canonical_rank, submitted, liveness_state
                FROM applications
                WHERE company IS NOT NULL
                ORDER BY id DESC
                LIMIT 200
                """
            ).fetchall()
        candidates: list[dict] = []
        for row in rows:
            record = dict(row)
            if normalized_company and _normalize_company(record.get("company")) != normalized_company:
                continue
            score = _semantic_similarity(title, record.get("title"))
            if location and record.get("location"):
                if location.strip().lower() == str(record["location"]).strip().lower():
                    score = min(1.0, score + 0.05)
            if score >= DEDUP_THRESHOLD:
                record["similarity_score"] = score
                candidates.append(record)
        return candidates

    def record_duplicate_audit(self, *, job_url: str, duplicate_of_job_url: str, similarity_score: float, reason_code: str, snapshot: dict) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO duplicate_audit(job_url, duplicate_of_job_url, similarity_score, reason_code, snapshot_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (job_url, duplicate_of_job_url, similarity_score, reason_code, json.dumps(snapshot, sort_keys=True), _now()),
            )
            self.conn.commit()

    def record_application(
        self,
        *,
        job_url: str,
        job_id_ext: str | None = None,
        company: str | None = None,
        title: str | None = None,
        location: str | None = None,
        classifier_score: float | None = None,
        decision: str = "skipped",
        submitted: bool = False,
        submission_outcome: str | None = None,
        resume_path: str | None = None,
        cover_letter_path: str | None = None,
        provenance: dict | None = None,
        error: str | None = None,
        liveness_state: str | None = None,
        liveness_reasons: list[str] | None = None,
        duplicate_of_job_url: str | None = None,
        duplicate_reason_code: str | None = None,
        attempt_mode: str | None = None,
    ) -> None:
        applied_at = _now() if submitted else None
        normalized_decision = normalize_decision(decision)
        normalized_outcome = normalize_submission_outcome(submission_outcome)
        warning = unknown_alias_warning("decision", decision, normalized_decision.value) or unknown_alias_warning(
            "submission_outcome", submission_outcome, normalized_outcome.value
        )
        existing = self.get_application(job_url) or {}
        merged = merge_status_values(
            existing,
            {
                "job_id_ext": job_id_ext,
                "company": company,
                "title": title,
                "location": location,
                "classifier_score": classifier_score,
                "decision": normalized_decision.value,
                "submitted": submitted,
                "submission_outcome": normalized_outcome.value,
                "resume_path": resume_path,
                "cover_letter_path": cover_letter_path,
                "error": error,
                "liveness_state": liveness_state,
                "duplicate_of_job_url": duplicate_of_job_url,
                "duplicate_reason_code": duplicate_reason_code,
            },
        )
        payload = dict(provenance or {})
        if warning:
            payload.setdefault("warnings", []).append({"field": warning.field, "raw": warning.raw_value, "normalized_to": warning.normalized_to})

        # On a successful attempt we explicitly clear the top-level error so a
        # row that was previously red flips to green in the UI. The previous
        # COALESCE behaviour kept stale errors and caused successful retries to
        # never update.
        success_outcomes = {"submitted", "submitted_unconfirmed", "dry_run_complete", "completed_with_deferred"}
        is_success_attempt = (
            normalized_outcome.value in success_outcomes
            and not error
        )
        merged_error = None if is_success_attempt else merged.get("error")

        # Per-mode tracking. Whether this attempt belongs to the DRY RUN section
        # or the REAL SUBMITS section is decided by the caller. If unspecified
        # we infer it from the attempt: a real submit either succeeded or hit a
        # post-submit error; everything else with a recognised dry-run outcome
        # belongs to the dry-run column.
        mode = (attempt_mode or "").lower()
        if mode not in {"dry_run", "real_submit"}:
            if normalized_outcome.value in {"submitted", "submitted_unconfirmed"}:
                mode = "real_submit"
            elif normalized_outcome.value in {"dry_run_complete", "completed_with_deferred", "deferred_blocked_required"}:
                mode = "dry_run"
            else:
                mode = ""

        dry_run_outcome = existing.get("dry_run_outcome")
        dry_run_completed_at = existing.get("dry_run_completed_at")
        dry_run_error = existing.get("dry_run_error")
        real_submit_outcome = existing.get("real_submit_outcome")
        real_submit_completed_at = existing.get("real_submit_completed_at")
        real_submit_error = existing.get("real_submit_error")

        if mode == "dry_run":
            dry_run_outcome = normalized_outcome.value
            dry_run_completed_at = _now()
            dry_run_error = None if is_success_attempt else (error or merged.get("error"))
        elif mode == "real_submit":
            real_submit_outcome = normalized_outcome.value
            real_submit_completed_at = _now()
            real_submit_error = None if is_success_attempt else (error or merged.get("error"))

        with self._lock:
            self.conn.execute(
                """
                INSERT INTO applications(
                  job_url, job_id_ext, company, title, location, classifier_score, decision, canonical_status, canonical_rank,
                  applied_at, submitted, submission_outcome, resume_path, cover_letter_path, provenance_json, error,
                  liveness_state, liveness_reasons_json, duplicate_of_job_url, duplicate_reason_code,
                  dry_run_outcome, dry_run_completed_at, dry_run_error,
                  real_submit_outcome, real_submit_completed_at, real_submit_error,
                  created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_url) DO UPDATE SET
                  job_id_ext=excluded.job_id_ext,
                  company=COALESCE(excluded.company, applications.company),
                  title=COALESCE(excluded.title, applications.title),
                  location=COALESCE(excluded.location, applications.location),
                  classifier_score=COALESCE(excluded.classifier_score, applications.classifier_score),
                  decision=excluded.decision,
                  canonical_status=excluded.canonical_status,
                  canonical_rank=excluded.canonical_rank,
                  applied_at=COALESCE(excluded.applied_at, applications.applied_at),
                  submitted=MAX(applications.submitted, excluded.submitted),
                  submission_outcome=excluded.submission_outcome,
                  resume_path=COALESCE(excluded.resume_path, applications.resume_path),
                  cover_letter_path=COALESCE(excluded.cover_letter_path, applications.cover_letter_path),
                  provenance_json=excluded.provenance_json,
                  error=excluded.error,
                  liveness_state=COALESCE(excluded.liveness_state, applications.liveness_state),
                  liveness_reasons_json=COALESCE(excluded.liveness_reasons_json, applications.liveness_reasons_json),
                  duplicate_of_job_url=COALESCE(excluded.duplicate_of_job_url, applications.duplicate_of_job_url),
                  duplicate_reason_code=COALESCE(excluded.duplicate_reason_code, applications.duplicate_reason_code),
                  dry_run_outcome=excluded.dry_run_outcome,
                  dry_run_completed_at=excluded.dry_run_completed_at,
                  dry_run_error=excluded.dry_run_error,
                  real_submit_outcome=excluded.real_submit_outcome,
                  real_submit_completed_at=excluded.real_submit_completed_at,
                  real_submit_error=excluded.real_submit_error,
                  updated_at=excluded.updated_at
                """,
                (
                    job_url,
                    merged.get("job_id_ext"),
                    merged.get("company"),
                    merged.get("title"),
                    merged.get("location"),
                    merged.get("classifier_score"),
                    merged["decision"],
                    merged["decision"],
                    decision_rank(merged["decision"]),
                    applied_at or existing.get("applied_at"),
                    int(bool(merged["submitted"])),
                    merged["submission_outcome"],
                    merged.get("resume_path"),
                    merged.get("cover_letter_path"),
                    json.dumps(payload, sort_keys=True),
                    merged_error,
                    merged.get("liveness_state"),
                    json.dumps(liveness_reasons or []),
                    merged.get("duplicate_of_job_url"),
                    merged.get("duplicate_reason_code"),
                    dry_run_outcome,
                    dry_run_completed_at,
                    dry_run_error,
                    real_submit_outcome,
                    real_submit_completed_at,
                    real_submit_error,
                    existing.get("created_at") or _now(),
                    _now(),
                ),
            )
            if submitted and not existing.get("submitted"):
                self._increment_site_counter_locked(job_url)
            self.conn.commit()

    def application_counts(self) -> dict[str, int]:
        today = date.today().isoformat()
        week_start = (date.today() - timedelta(days=7)).isoformat()
        with self._lock:
            row = self.conn.execute(
                """
                SELECT
                  SUM(CASE WHEN submitted=1 AND date(applied_at, 'localtime')=date(?) THEN 1 ELSE 0 END) AS today,
                  SUM(CASE WHEN submitted=1 AND date(applied_at, 'localtime')>=date(?) THEN 1 ELSE 0 END) AS week,
                  SUM(CASE WHEN submitted=1 THEN 1 ELSE 0 END) AS all_time
                FROM applications
                """,
                (today, week_start),
            ).fetchone()
        return {key: int(row[key] or 0) for key in ["today", "week", "all_time"]}

    def last_applications(self, limit: int) -> list[dict]:
        return self.list_applications(limit=limit, offset=0)

    def list_applications(self, limit: int = 100, offset: int = 0) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM applications ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_application(self, job_url: str) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM listing_runs WHERE job_url=?", (job_url,))
            app_cur = self.conn.execute("DELETE FROM applications WHERE job_url=?", (job_url,))
            self.conn.commit()
            return app_cur.rowcount > 0 or cur.rowcount > 0

    def clear_history(self) -> dict[str, int]:
        tables = (
            "applications",
            "listing_runs",
            "runs",
            "event_log",
            "pending_actions",
            "pending_questions",
            "duplicate_audit",
        )
        counts: dict[str, int] = {}
        with self._lock:
            for table in tables:
                row = self.conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
                counts[table] = int(row["count"] or 0)
                self.conn.execute(f"DELETE FROM {table}")
            with suppress(sqlite3.OperationalError):
                placeholders = ",".join("?" for _ in tables)
                self.conn.execute(f"DELETE FROM sqlite_sequence WHERE name IN ({placeholders})", tables)
            self.conn.commit()
        return counts

    def list_runs(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def list_site_limits(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT domain, daily_limit, applied_today FROM site_limits").fetchall()
        return [dict(row) for row in rows]

    def upsert_site_limit(self, domain: str, daily_limit: int | None) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO site_limits(domain, daily_limit, applied_today, last_reset_date)
                VALUES (?, ?, 0, ?)
                ON CONFLICT(domain) DO UPDATE SET daily_limit=excluded.daily_limit
                """,
                (domain, daily_limit, date.today().isoformat()),
            )
            self.conn.commit()

    def site_limit_hit(self, job_url: str) -> bool:
        domain = urlparse(job_url).hostname or "unknown"
        with self._lock:
            row = self.conn.execute("SELECT * FROM site_limits WHERE domain=?", (domain,)).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO site_limits(domain, daily_limit, applied_today, last_reset_date) VALUES (?, ?, 0, ?)",
                    (domain, 50, date.today().isoformat()),
                )
                self.conn.commit()
                row = self.conn.execute("SELECT * FROM site_limits WHERE domain=?", (domain,)).fetchone()
            if row["last_reset_date"] != date.today().isoformat():
                self.conn.execute(
                    "UPDATE site_limits SET applied_today=0, last_reset_date=? WHERE domain=?",
                    (date.today().isoformat(), domain),
                )
                self.conn.commit()
                row = self.conn.execute("SELECT * FROM site_limits WHERE domain=?", (domain,)).fetchone()
            return row is not None and row["daily_limit"] is not None and row["applied_today"] >= row["daily_limit"]

    def _increment_site_counter_locked(self, job_url: str) -> None:
        domain = urlparse(job_url).hostname or "unknown"
        row = self.conn.execute("SELECT * FROM site_limits WHERE domain=?", (domain,)).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO site_limits(domain, daily_limit, applied_today, last_reset_date) VALUES (?, ?, 0, ?)",
                (domain, 50, date.today().isoformat()),
            )
        elif row["last_reset_date"] != date.today().isoformat():
            self.conn.execute("UPDATE site_limits SET applied_today=0, last_reset_date=? WHERE domain=?", (date.today().isoformat(), domain))
        self.conn.execute("UPDATE site_limits SET applied_today=applied_today+1 WHERE domain=?", (domain,))

    def increment_site_counter(self, job_url: str) -> None:
        with self._lock:
            self._increment_site_counter_locked(job_url)
            self.conn.commit()

    def upsert_pending_action(self, token: str, action_type: str, payload: dict, *, correlation_id: str | None = None, expires_at: str | None = None) -> None:
        now = _now()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO pending_actions(token, action_type, payload_json, status, correlation_id, created_at, updated_at, expires_at)
                VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)
                ON CONFLICT(token) DO UPDATE SET
                  action_type=excluded.action_type,
                  payload_json=excluded.payload_json,
                  status='pending',
                  correlation_id=COALESCE(excluded.correlation_id, pending_actions.correlation_id),
                  updated_at=excluded.updated_at,
                  expires_at=excluded.expires_at
                """,
                (token, action_type, json.dumps(payload), correlation_id, now, now, expires_at),
            )
            self.conn.commit()

    def resolve_pending_action(self, token: str, *, status: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE pending_actions SET status=?, updated_at=? WHERE token=?", (status, _now(), token))
            self.conn.commit()

    def clear_pending_action(self, token: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM pending_actions WHERE token=?", (token,))
            self.conn.commit()

    def list_pending_actions(self, action_type: str | None = None) -> list[dict]:
        with self._lock:
            if action_type:
                rows = self.conn.execute("SELECT * FROM pending_actions WHERE action_type=? ORDER BY created_at ASC", (action_type,)).fetchall()
            else:
                rows = self.conn.execute("SELECT * FROM pending_actions ORDER BY created_at ASC").fetchall()
        results = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            results.append({"token": row["token"], "action_type": row["action_type"], "status": row["status"], "created_at": row["created_at"], **payload})
        return results

    def cleanup_stale_pending_records(self) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_PENDING_DAYS)).isoformat()
        with self._lock:
            cur = self.conn.execute("DELETE FROM pending_actions WHERE created_at < ?", (cutoff,))
            self.conn.commit()
            return cur.rowcount or 0

    def maintenance(self) -> dict[str, int]:
        cleaned = self.cleanup_stale_pending_records()
        with self._lock:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.conn.execute("VACUUM")
        return {"stale_pending_deleted": cleaned}

    def verify_integrity(self) -> dict:
        findings: list[dict] = []
        with self._lock:
            pragma = self.conn.execute("PRAGMA integrity_check").fetchone()
            if pragma and pragma[0] != "ok":
                findings.append({"severity": "critical", "code": "sqlite_integrity_failed", "message": pragma[0]})
            apps = self.conn.execute("SELECT * FROM applications").fetchall()
            states = self.conn.execute("SELECT run_id, job_url, state FROM listing_runs").fetchall()
            run_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(runs)").fetchall()}
            select_cols = ["id", "status"]
            if "summary_artifact_path" in run_columns:
                select_cols.append("summary_artifact_path")
            runs = self.conn.execute(f"SELECT {', '.join(select_cols)} FROM runs").fetchall()
        for row in apps:
            record = dict(row)
            if record.get("resume_path") and not Path(record["resume_path"]).exists():
                findings.append({"severity": "warning", "code": "artifact_missing", "job_url": record["job_url"], "path": record["resume_path"]})
            if record.get("cover_letter_path") and not Path(record["cover_letter_path"]).exists():
                findings.append({"severity": "warning", "code": "artifact_missing", "job_url": record["job_url"], "path": record["cover_letter_path"]})
            if normalize_decision(record.get("decision")).value != (record.get("canonical_status") or CanonicalDecision.SKIPPED.value):
                findings.append({"severity": "error", "code": "decision_not_canonical", "job_url": record["job_url"]})
        for row in states:
            if row["state"] not in {state.value for state in ListingLifecycle}:
                findings.append({"severity": "error", "code": "invalid_listing_state", "job_url": row["job_url"]})
            if row["state"] == ListingLifecycle.COMPLETED.value and not any(app["job_url"] == row["job_url"] for app in apps):
                findings.append({"severity": "error", "code": "listing_completed_without_application", "job_url": row["job_url"]})
        for row in runs:
            record = dict(row)
            if record["status"] in {"done", "stopped", "error"} and "summary_artifact_path" in record and not record.get("summary_artifact_path"):
                findings.append({"severity": "warning", "code": "run_summary_missing", "run_id": record["id"]})
            if record.get("summary_artifact_path") and not Path(record["summary_artifact_path"]).exists():
                findings.append({"severity": "warning", "code": "run_summary_artifact_missing", "run_id": record["id"], "path": record["summary_artifact_path"]})
        duplicates = self._verify_semantic_duplicates()
        findings.extend(duplicates)
        critical = [item for item in findings if item["severity"] == "critical"]
        return {"ok": not critical, "critical_failure_count": len(critical), "findings": findings}

    def _verify_semantic_duplicates(self) -> list[dict]:
        apps = self.list_applications(limit=1000)
        findings: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for app in apps:
            candidates = self.semantic_duplicate_candidate_lookup(company=app.get("company"), title=app.get("title"), location=app.get("location"))
            for candidate in candidates:
                if candidate["job_url"] == app["job_url"]:
                    continue
                pair = tuple(sorted((app["job_url"], candidate["job_url"])))
                if pair in seen:
                    continue
                seen.add(pair)
                findings.append({"severity": "warning", "code": "semantic_duplicate_candidate", "jobs": list(pair)})
        return findings

    def event(self, *, event_type: str, payload: dict, run_id: int | None = None, job_url: str | None = None) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO event_log(run_id, job_url, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (run_id, job_url, event_type, json.dumps(payload, sort_keys=True), _now()),
            )
            self.conn.commit()

    def get_translation_cache(self, *, src: str, dst: str, text_hash: str) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT src, dst, text_hash, translated_text, translator, created_at FROM translation_cache WHERE src=? AND dst=? AND text_hash=?",
                (src, dst, text_hash),
            ).fetchone()
        return dict(row) if row else None

    def upsert_translation_cache(
        self,
        *,
        src: str,
        dst: str,
        text_hash: str,
        translated_text: str,
        translator: str,
        created_at: str | None = None,
    ) -> None:
        timestamp = created_at or _now()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO translation_cache(src, dst, text_hash, translated_text, translator, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(src, dst, text_hash)
                DO UPDATE SET translated_text=excluded.translated_text, translator=excluded.translator, created_at=excluded.created_at
                """,
                (src, dst, text_hash, translated_text, translator, timestamp),
            )
            self.conn.commit()

    def analytics(self, *, lookback_days: int) -> dict:
        cutoff = (datetime.now() - timedelta(days=lookback_days)).isoformat()
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT company, title, job_url, decision, canonical_status, submission_outcome, error, created_at
                FROM applications
                WHERE created_at >= ?
                ORDER BY created_at DESC
                """,
                (cutoff,),
            ).fetchall()
        apps = [dict(row) for row in rows]
        by_domain: dict[str, dict[str, int]] = {}
        by_archetype: dict[str, int] = {}
        rejection_reasons: dict[str, int] = {}
        follow_up_queue: list[dict] = []
        submitted = 0
        for app in apps:
            domain = urlparse(app["job_url"]).hostname or "unknown"
            bucket = by_domain.setdefault(domain, {"total": 0, "submitted": 0})
            bucket["total"] += 1
            bucket["submitted"] += int(app.get("submission_outcome") == "submitted")
            archetype = "manual" if "manual" in (app.get("canonical_status") or "") else "standard"
            by_archetype[archetype] = by_archetype.get(archetype, 0) + 1
            if app.get("error"):
                rejection_reasons[app["error"]] = rejection_reasons.get(app["error"], 0) + 1
            if app.get("submission_outcome") != "submitted":
                follow_up_queue.append(
                    {
                        "job_url": app["job_url"],
                        "category": "retry" if app.get("canonical_status") == CanonicalDecision.FAILED_TRANSIENT.value else "review",
                        "recommendation": "retry later" if app.get("canonical_status") == CanonicalDecision.FAILED_TRANSIENT.value else "manual review",
                        "confidence": 0.92 if app.get("canonical_status") == CanonicalDecision.FAILED_TRANSIENT.value else 0.75,
                    }
                )
            submitted += int(app.get("submission_outcome") == "submitted")
        recommendations = [{"type": "follow_up_queue", "count": len(follow_up_queue), "confidence": 0.84}]
        return {
            "computed_at": _now(),
            "lookback_days": lookback_days,
            "applications_total": len(apps),
            "outcome_by_archetype": by_archetype,
            "conversion_by_domain": {
                domain: {"total": values["total"], "submitted": values["submitted"], "conversion_rate": (values["submitted"] / values["total"]) if values["total"] else 0.0}
                for domain, values in by_domain.items()
            },
            "top_gap_reasons": sorted(
                [{"reason": reason, "count": count} for reason, count in rejection_reasons.items()],
                key=lambda item: item["count"],
                reverse=True,
            )[:5],
            "follow_up_queue": follow_up_queue,
            "recommendations": recommendations,
        }

    def upsert_learned_answer(self, label_normalized: str, classification: str, answer: str, timestamp: str) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO learned_answers(label_normalized, classification, answer, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(label_normalized) DO UPDATE SET classification=excluded.classification, answer=excluded.answer, updated_at=excluded.updated_at
                """,
                (label_normalized, classification, answer, timestamp, timestamp),
            )
            self.conn.commit()

    def list_learned_answers(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM learned_answers ORDER BY updated_at DESC").fetchall()
        return [dict(row) for row in rows]

    def delete_learned_answer(self, answer_id: int) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM learned_answers WHERE id=?", (answer_id,))
            self.conn.commit()
            return bool(cur.rowcount)

    def upsert_pending_question(self, *, label_normalized: str, classification: str, job_id: str, job_title: str, company: str, created_at: str) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO pending_questions(label_normalized, classification, job_id, job_title, company, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(label_normalized, job_id) DO NOTHING
                """,
                (label_normalized, classification, job_id, job_title, company, created_at),
            )
            self.conn.commit()

    def list_pending_questions(self, resolved: bool = False) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM pending_questions WHERE resolved=? ORDER BY created_at DESC", (1 if resolved else 0,)).fetchall()
        return [dict(row) for row in rows]

    def resolve_pending_question(self, question_id: int, answer: str) -> dict | None:
        with self._lock:
            self.conn.execute("UPDATE pending_questions SET user_answer=?, resolved=1 WHERE id=?", (answer, question_id))
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM pending_questions WHERE id=?", (question_id,)).fetchone()
        return dict(row) if row else None


def _normalize_company(value: str | None) -> str:
    text = (value or "").lower().strip()
    for suffix in (" inc.", " ltd.", " llc", ", inc", " private limited", " pvt ltd"):
        text = text.replace(suffix, "")
    return " ".join(text.split())


def _semantic_similarity(left: str | None, right: str | None) -> float:
    if not left or not right:
        return 0.0
    return fuzz.token_sort_ratio(left, right) / 100.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
