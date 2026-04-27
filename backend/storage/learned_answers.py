from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

from backend.storage.sqlite_db import SQLiteStore

log = structlog.get_logger()


def lookup_learned_answer(label: str, classification: str, store: SQLiteStore | None = None) -> str | None:
    if not label or not label.strip():
        return None
    normalized = label.strip().lower()
    db = store or SQLiteStore()
    should_close = store is None
    try:
        rows = db.list_learned_answers()
        for row in rows:
            if str(row.get("label_normalized", "")).lower() == normalized and row.get("answer"):
                return str(row["answer"])
        query_words = set(normalized.split())
        for row in rows:
            if row.get("classification") != classification or not row.get("answer"):
                continue
            stored_words = set(str(row.get("label_normalized", "")).split())
            if not stored_words or not query_words:
                continue
            overlap = len(stored_words & query_words) / max(len(stored_words), len(query_words))
            if overlap >= 0.80:
                return str(row["answer"])
        return None
    finally:
        if should_close:
            db.close()


def store_learned_answer(label: str, classification: str, answer: str, store: SQLiteStore | None = None) -> None:
    if not label or not answer:
        return
    db = store or SQLiteStore()
    should_close = store is None
    try:
        db.upsert_learned_answer(label.strip().lower(), classification, answer, datetime.now(timezone.utc).isoformat())
    except Exception as exc:
        log.error("store_learned_answer_failed", error=str(exc), label=label[:80], classification=classification)
    finally:
        if should_close:
            db.close()


def store_pending_question(
    *,
    label: str,
    classification: str,
    job_id: str,
    job_title: str,
    company: str,
    store: SQLiteStore | None = None,
) -> None:
    if not label:
        return
    db = store or SQLiteStore()
    should_close = store is None
    try:
        db.upsert_pending_question(
            label_normalized=label.strip().lower(),
            classification=classification,
            job_id=job_id,
            job_title=job_title,
            company=company,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:
        log.error("store_pending_question_failed", error=str(exc), label=label[:80], classification=classification)
    finally:
        if should_close:
            db.close()


def list_pending_questions(store: SQLiteStore | None = None) -> list[dict[str, Any]]:
    db = store or SQLiteStore()
    should_close = store is None
    try:
        return db.list_pending_questions()
    finally:
        if should_close:
            db.close()


def resolve_pending_question(question_id: int, answer: str, store: SQLiteStore | None = None) -> dict[str, Any] | None:
    db = store or SQLiteStore()
    should_close = store is None
    try:
        row = db.resolve_pending_question(question_id, answer)
        if row:
            store_learned_answer(
                label=row.get("label_normalized", ""),
                classification=row.get("classification", ""),
                answer=answer,
                store=db,
            )
        return row
    finally:
        if should_close:
            db.close()
