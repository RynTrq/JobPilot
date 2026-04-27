from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend import config
from backend.orchestrator import Orchestrator
from backend.storage.sqlite_db import SQLiteStore


@pytest.fixture()
def store(tmp_path):
    db = SQLiteStore(tmp_path / "jobpilot.db")
    try:
        yield db
    finally:
        db.close()


def test_successful_dry_run_clears_stale_error_and_locks_only_dry_run(store):
    job_url = "https://example.com/jobs/123"
    store.record_application(
        job_url=job_url,
        company="Example",
        title="Backend Engineer",
        decision="manual_review_required",
        submitted=False,
        submission_outcome="manual_review_required",
        error="missing required field",
        attempt_mode="dry_run",
    )

    store.record_application(
        job_url=job_url,
        company="Example",
        title="Backend Engineer",
        decision="pass",
        submitted=False,
        submission_outcome="dry_run_complete",
        error=None,
        attempt_mode="dry_run",
    )

    row = store.get_application(job_url)
    assert row["error"] is None
    assert row["dry_run_outcome"] == "dry_run_complete"
    assert row["dry_run_error"] is None
    assert store.successful_attempt_mode(job_url, "dry_run") is True
    assert store.successful_attempt_mode(job_url, "real_submit") is False


def test_real_submit_success_locks_real_submit_separately(store):
    job_url = "https://example.com/jobs/456"
    store.record_application(
        job_url=job_url,
        company="Example",
        title="Frontend Engineer",
        decision="pass",
        submitted=False,
        submission_outcome="dry_run_complete",
        attempt_mode="dry_run",
    )
    store.record_application(
        job_url=job_url,
        company="Example",
        title="Frontend Engineer",
        decision="pass",
        submitted=True,
        submission_outcome="submitted",
        attempt_mode="real_submit",
    )

    row = store.get_application(job_url)
    assert row["dry_run_outcome"] == "dry_run_complete"
    assert row["real_submit_outcome"] == "submitted"
    assert store.successful_attempt_mode(job_url, "dry_run") is True
    assert store.successful_attempt_mode(job_url, "real_submit") is True


@pytest.mark.asyncio
async def test_forced_rerun_rejects_green_entry_in_current_mode(store):
    job_url = "https://example.com/jobs/789"
    store.record_application(
        job_url=job_url,
        company="Example",
        title="Platform Engineer",
        decision="pass",
        submitted=False,
        submission_outcome="dry_run_complete",
        attempt_mode="dry_run",
    )
    config.set_live_submit_enabled(False)
    orch = Orchestrator(SimpleNamespace(sqlite=store))

    with pytest.raises(RuntimeError, match="Dry Run already completed"):
        await orch.start(job_url, limit=1, force_reprocess=True, bypass_classifier=True)
