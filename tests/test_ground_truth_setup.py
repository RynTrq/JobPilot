from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import backend.models.classifier_feedback as classifier_feedback_module
from backend.api.routes_control import _record_classifier_review_feedback
from backend.form.field_answerer import _phone_country_code_answer, build_candidate_data
from backend.form.filler import _deterministic_field_answer_override, _format_phone_answer
from backend.models.classifier_feedback import ClassifierFeedbackStore, feedback_adjusted_score
from backend.storage import ground_truth as ground_truth_module
from backend.storage.ground_truth import empty_ground_truth


def test_empty_ground_truth_contains_no_personal_seed_data():
    payload = empty_ground_truth().model_dump(mode="json")

    assert payload["personal"]["full_name"] == ""
    assert payload["personal"]["email"] == ""
    assert payload["personal"]["phone_e164"] == ""
    assert payload["personal"]["github_url"] == ""


def test_ground_truth_path_prefers_new_directory_and_falls_back_to_legacy(tmp_path, monkeypatch):
    canonical = tmp_path / "data" / "ground_truth" / "ground_truth.json"
    legacy = tmp_path / "data" / "ground_truth.json"
    monkeypatch.setattr(ground_truth_module, "GROUND_TRUTH_PATH", canonical)
    monkeypatch.setattr(ground_truth_module, "LEGACY_GROUND_TRUTH_PATH", legacy)

    assert ground_truth_module.resolve_ground_truth_path() == canonical

    legacy.parent.mkdir(parents=True)
    legacy.write_text("{}", encoding="utf-8")
    assert ground_truth_module.resolve_ground_truth_path() == legacy

    canonical.parent.mkdir(parents=True)
    canonical.write_text("{}", encoding="utf-8")
    assert ground_truth_module.resolve_ground_truth_path() == canonical


def test_classifier_feedback_uses_human_review_label(tmp_path: Path):
    path = tmp_path / "classifier_feedback.jsonl"
    store = ClassifierFeedbackStore(path)

    store.append_agent_signal(
        job_url="https://example.com/jobs/1",
        jd_text="Backend role with Python.",
        candidate_facts={},
        agent_decision="filtered_low_score",
        agent_reasoning={"fit": False},
        sut_score=0.22,
        sut_decision="fail",
        review_label="pass",
        title="Backend Engineer",
        company="Example",
    )

    rows = store.read()
    assert len(rows) == 1
    assert rows[0]["label"] == "pass"
    assert rows[0]["review_label"] == "pass"


def test_classifier_feedback_reads_legacy_and_canonical_but_writes_canonical(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    ground_truth_dir = data_dir / "ground_truth"
    legacy = data_dir / "classifier_feedback.jsonl"
    canonical = ground_truth_dir / "classifier_feedback.jsonl"
    data_dir.mkdir(parents=True)
    ground_truth_dir.mkdir(parents=True)
    legacy.write_text(
        '{"job_url":"https://example.com/old","label":"pass","score":0.8,'
        '"description_text":"Old Python backend role","created_at":"2026-01-01T00:00:00"}\n',
        encoding="utf-8",
    )
    canonical.write_text(
        '{"job_url":"https://example.com/new","label":"fail","score":0.2,'
        '"description_text":"New sales role","created_at":"2026-01-02T00:00:00"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(classifier_feedback_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(classifier_feedback_module, "GROUND_TRUTH_DIR", ground_truth_dir)

    store = ClassifierFeedbackStore()
    store.append(
        job_url="https://example.com/next",
        label="pass",
        score=0.9,
        description_text="Next backend role",
    )

    rows = store.read()
    assert [row["job_url"] for row in rows[:2]] == ["https://example.com/old", "https://example.com/new"]
    assert rows[-1]["job_url"] == "https://example.com/next"
    assert "https://example.com/next" in canonical.read_text(encoding="utf-8")
    assert "https://example.com/next" not in legacy.read_text(encoding="utf-8")


def test_feedback_adjustment_ignores_unreviewed_auto_labels(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    ground_truth_dir = data_dir / "ground_truth"
    monkeypatch.setattr(classifier_feedback_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(classifier_feedback_module, "GROUND_TRUTH_DIR", ground_truth_dir)

    store = ClassifierFeedbackStore()
    for idx in range(5):
        store.append(
            job_url=f"https://example.com/jobs/auto-{idx}",
            label="fail",
            score=0.1,
            description_text="Python backend engineer building APIs.",
        )

    class Encoder:
        def encode(self, text):
            import numpy as np

            return np.asarray([1.0, 0.0], dtype=np.float32)

        def encode_batch(self, texts):
            import numpy as np

            return np.stack([self.encode(text) for text in texts])

    assert feedback_adjusted_score(0.82, "Python backend engineer", Encoder()) == 0.82


def test_feedback_adjustment_dedupes_reviewed_examples(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    ground_truth_dir = data_dir / "ground_truth"
    monkeypatch.setattr(classifier_feedback_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(classifier_feedback_module, "GROUND_TRUTH_DIR", ground_truth_dir)

    store = ClassifierFeedbackStore()
    for _ in range(2):
        store.append_agent_signal(
            job_url="https://example.com/jobs/same",
            jd_text="Python backend engineer building APIs.",
            candidate_facts={},
            agent_decision="filtered_low_score",
            agent_reasoning={"fit": False},
            sut_score=0.2,
            sut_decision="fail",
            review_label="fail",
            title="Backend Engineer",
            company="Example",
        )
    store.append_agent_signal(
        job_url="https://example.com/jobs/pass",
        jd_text="Python backend engineer building APIs.",
        candidate_facts={},
        agent_decision="fit",
        agent_reasoning={"fit": True},
        sut_score=0.8,
        sut_decision="pass",
        review_label="pass",
        title="Backend Engineer",
        company="Example",
    )

    class Encoder:
        def encode(self, text):
            import numpy as np

            return np.asarray([1.0, 0.0], dtype=np.float32)

        def encode_batch(self, texts):
            import numpy as np

            return np.stack([self.encode(text) for text in texts])

    assert feedback_adjusted_score(0.82, "Python backend engineer", Encoder()) == 0.82


def test_classifier_response_records_feedback_immediately(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    ground_truth_dir = data_dir / "ground_truth"
    monkeypatch.setattr(classifier_feedback_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(classifier_feedback_module, "GROUND_TRUTH_DIR", ground_truth_dir)
    pending = SimpleNamespace(
        details={
            "job_url": "https://example.com/jobs/feedback",
            "description_text": "Python backend role with APIs.",
            "classifier_score": 0.31,
            "classifier_threshold_decision": "fail",
            "title": "Backend Engineer",
            "company": "Example",
            "fit_decision": {"fit": False, "submission_outcome": "filtered_low_score"},
        }
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                classifier_review=SimpleNamespace(pending={"token": pending})
            )
        )
    )

    recorded = _record_classifier_review_feedback(
        request=request,
        token="token",
        passed=True,
        decision_payload=None,
    )

    rows = ClassifierFeedbackStore().read()
    assert recorded is True
    assert rows[-1]["job_url"] == "https://example.com/jobs/feedback"
    assert rows[-1]["label"] == "pass"
    assert rows[-1]["review_label"] == "pass"


def test_candidate_data_uses_user_country_code_and_compensation():
    candidate_data = build_candidate_data(
        {
            "personal": {
                "full_name": "Example User",
                "email": "user@example.com",
                "phone_e164": "+15551234567",
                "location_city": "New York",
                "location_country": "United States",
                "citizenship": "United States",
            },
            "education": [],
            "experience": [],
            "projects": [],
            "preferences": {"salary_min_usd_annual": 120000},
        },
        {
            "work_authorization": {
                "sponsorship": {
                    "united_states": {"now_requires": False, "future_requires": False},
                    "default_outside_authorized_countries": {"now_requires": True, "future_requires": True},
                }
            },
            "compensation_policy": {"target_salary": {"amount": 130000, "currency": "USD"}},
            "application_defaults": {"disclosures": {}},
        },
    )
    options = ["United States (+1)", "India (+91)", "United Arab Emirates (+971)"]

    assert candidate_data["candidate"]["work_authorization"]["requires_sponsorship"] == "No"
    assert candidate_data["candidate"]["preferences"]["expected_salary"] == "130000 USD"
    assert _phone_country_code_answer("phone country code", options, candidate_data) == "United States (+1)"

    field = SimpleNamespace(
        label_text="Phone country",
        name="phoneCountry",
        element_id="",
        selector="",
        aria_label="",
        placeholder="",
        options=options,
    )
    assert _deterministic_field_answer_override(field, candidate_data) == "United States (+1)"
    assert _format_phone_answer("+15551234567", "", local_number=True) == "5551234567"


def test_telephone_country_code_without_options_uses_candidate_country():
    candidate_data = build_candidate_data(
        {
            "personal": {
                "full_name": "Example User",
                "email": "user@example.com",
                "phone_e164": "+919876543210",
                "location_city": "Bengaluru",
                "location_country": "India",
                "citizenship": "India",
            },
            "education": [],
            "experience": [],
            "projects": [],
            "preferences": {},
        },
        {},
    )

    assert _phone_country_code_answer("telephone country code", [], candidate_data) == "+91"

    field = SimpleNamespace(
        label_text="Telephone country code",
        name="telephoneCountryCode",
        element_id="",
        selector="",
        aria_label="",
        placeholder="",
        options=[],
    )
    assert _deterministic_field_answer_override(field, candidate_data) == "+91"
