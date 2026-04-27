from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, ConfigDict


class CanonicalDecision(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    HUMAN_FAIL = "human_fail"
    SKIPPED = "skipped"
    DUPLICATE = "duplicate"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"
    MANUAL_AUTH_REQUIRED = "manual_auth_required"
    BLOCKED_CREDENTIALS = "blocked_credentials"
    FAILED_TRANSIENT = "failed_transient"
    PROVIDER_BACKOFF = "provider_backoff"
    DEPENDENCY_MISSING = "dependency_missing"
    DEGRADED_MODE = "degraded_mode"


class SubmissionOutcome(StrEnum):
    SUBMITTED = "submitted"
    SUBMITTED_UNCONFIRMED = "submitted_unconfirmed"
    NOT_SUBMITTED = "not_submitted"
    DRY_RUN_COMPLETE = "dry_run_complete"
    FAILED = "failed"
    FAILED_TRANSIENT = "failed_transient"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"
    PARKED_EXTERNAL_INTERSTITIAL = "parked_external_interstitial"
    PARKED_ROBOT_DETECTION = "parked_robot_detection"
    PARKED_AUTH_REQUIRED = "parked_auth_required"
    PARKED_CREDENTIALS = "parked_credentials"
    PARKED_RATE_LIMIT = "parked_rate_limit"
    LIVENESS_EXPIRED = "liveness_expired"
    FILTERED_LOW_SCORE = "filtered_low_score"
    FILTERED_SENIORITY = "filtered_seniority"
    FILTERED_NO_EXPERTISE_OVERLAP = "filtered_no_expertise_overlap"
    FILTERED_HARD_FAIL_DEGREE_REQUIREMENT = "filtered_hard_fail_degree_requirement"
    FILTERED_HARD_FAIL_CLEARANCE = "filtered_hard_fail_clearance"
    FILTERED_HARD_FAIL_CITIZENSHIP = "filtered_hard_fail_citizenship"
    FILTERED_HARD_FAIL_SALARY_FLOOR = "filtered_hard_fail_salary_floor"
    NO_JOBS_FOUND = "no_jobs_found"
    PARKED_PENDING_ENVIRONMENT = "parked_pending_environment"
    COMPLETED_WITH_DEFERRED = "completed_with_deferred"
    DEFERRED_BLOCKED_REQUIRED = "deferred_blocked_required"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class RunLifecycle(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    DONE = "done"
    ERROR = "error"


class ListingLifecycle(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED_TRANSIENT = "failed_transient"
    FAILED_PERMANENT = "failed_permanent"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class LivenessState(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    UNCERTAIN = "uncertain"


class SpecialistName(StrEnum):
    JD_EXTRACTOR = "JD_Extractor"
    JD_CLEANER = "JD_Cleaner"
    FIT_CLASSIFIER = "Fit_Classifier"
    FIT_TIEBREAKER = "Fit_Tiebreaker"
    LIVENESS_DETECTOR = "Liveness_Detector"
    DEDUP_DETECTOR = "Dedup_Detector"
    RESUME_BULLETPICKER = "Resume_BulletPicker"
    RESUME_BULLET_REWRITER = "Resume_BulletRewriter"
    RESUME_SUMMARY = "Resume_Summary"
    RESUME_SKILLS = "Resume_Skills"
    RESUME_TAGLINE = "Resume_Tagline"
    COVERLETTER_HOOK = "CoverLetter_Hook"
    COVERLETTER_FIT = "CoverLetter_Fit"
    COVERLETTER_CLOSE = "CoverLetter_Close"
    FORM_FIELDCLASSIFIER = "Form_FieldClassifier"
    FORM_OPTIONMATCHER = "Form_OptionMatcher"
    FORM_FREETEXT = "Form_FreeText"
    FORM_DATE_NORMALIZER = "Form_DateNormalizer"
    FORM_LOCALE = "Form_Locale"
    BROWSER_STEERER = "Browser_Steerer"
    ANOMALY_SENTINEL = "Anomaly_Sentinel"
    PROVENANCE_LEDGER = "Provenance_Ledger"
    EVAL_RUNNER = "Eval_Runner"


class PrivacyLevel(StrEnum):
    PUBLIC = "public"
    REDACTED = "redacted"
    SENSITIVE = "sensitive"


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PromptMetadata(StrictContract):
    prompt_id: str
    specialist: SpecialistName
    default_model: str
    temperature: float = Field(ge=0.0, le=2.0)
    max_tokens: int = Field(gt=0, le=4096)
    schema_ref: str
    updated: str
    eval_path: str | None = None


class JdMeta(StrictContract):
    company: str = ""
    role_title: str = ""
    top_requirements: list[str] = Field(default_factory=list, max_length=3)
    why_company_fact: str = ""
    jd_domain_tags: list[str] = Field(default_factory=list, min_length=0, max_length=5)
    keywords_exact: list[str] = Field(default_factory=list, max_length=20)


class ResumeBulletRewrite(StrictContract):
    bullet: str
    citations: list[str] = Field(default_factory=list)


class FormFreeTextAnswer(StrictContract):
    length_words: int = Field(ge=0)
    body: str
    citations: list[str] = Field(default_factory=list)


class LlmRequest(StrictContract):
    specialist: SpecialistName
    system: str
    user: str
    privacy_level: PrivacyLevel = PrivacyLevel.PUBLIC
    requires_json: bool = False
    latency_budget_ms: int = Field(default=1000, gt=0)
    max_tokens: int = Field(default=256, gt=0, le=4096)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    quality_tier: str = "fast"


class LlmResponse(StrictContract):
    text: str
    provider: str
    model: str
    route_reasons: list[str] = Field(default_factory=list)
    schema_valid: bool | None = None


DECISION_ALIASES = {
    "approved": CanonicalDecision.PASS,
    "yes": CanonicalDecision.PASS,
    "rejected": CanonicalDecision.FAIL,
    "no": CanonicalDecision.FAIL,
    "human_reject": CanonicalDecision.HUMAN_FAIL,
    "duplicate_skip": CanonicalDecision.DUPLICATE,
}

SUBMISSION_ALIASES = {
    "success": SubmissionOutcome.SUBMITTED,
    "apply_clicked": SubmissionOutcome.SUBMITTED,
    "dry_run": SubmissionOutcome.NOT_SUBMITTED,
    "needs_manual_takeover": SubmissionOutcome.PARKED_ROBOT_DETECTION,
    "external_interstitial": SubmissionOutcome.PARKED_ROBOT_DETECTION,
    "manual_auth_required": SubmissionOutcome.PARKED_AUTH_REQUIRED,
    "manual_review_required": SubmissionOutcome.MANUAL_REVIEW_REQUIRED,
    "provider_backoff": SubmissionOutcome.PARKED_RATE_LIMIT,
    "expired": SubmissionOutcome.LIVENESS_EXPIRED,
    "needs_attention": SubmissionOutcome.FAILED,
}

DECISION_RANK = {
    CanonicalDecision.PASS: 100,
    CanonicalDecision.HUMAN_FAIL: 90,
    CanonicalDecision.FAIL: 80,
    CanonicalDecision.MANUAL_REVIEW_REQUIRED: 70,
    CanonicalDecision.MANUAL_AUTH_REQUIRED: 60,
    CanonicalDecision.BLOCKED_CREDENTIALS: 55,
    CanonicalDecision.PROVIDER_BACKOFF: 50,
    CanonicalDecision.FAILED_TRANSIENT: 45,
    CanonicalDecision.DEPENDENCY_MISSING: 40,
    CanonicalDecision.DEGRADED_MODE: 35,
    CanonicalDecision.DUPLICATE: 30,
    CanonicalDecision.SKIPPED: 20,
}

VALID_TRANSITIONS = {
    # Startup reconciliation may reset an in-flight row to pending while the
    # original worker is still unwinding after a manual approval. Accept terminal
    # writes from pending so a stale lifecycle row cannot crash an otherwise
    # verified dry-run result.
    ListingLifecycle.PENDING: {
        ListingLifecycle.IN_PROGRESS,
        ListingLifecycle.COMPLETED,
        ListingLifecycle.FAILED_TRANSIENT,
        ListingLifecycle.FAILED_PERMANENT,
        ListingLifecycle.SKIPPED,
        ListingLifecycle.BLOCKED,
    },
    ListingLifecycle.IN_PROGRESS: {
        ListingLifecycle.COMPLETED,
        ListingLifecycle.FAILED_TRANSIENT,
        ListingLifecycle.FAILED_PERMANENT,
        ListingLifecycle.SKIPPED,
        ListingLifecycle.BLOCKED,
    },
    ListingLifecycle.FAILED_TRANSIENT: {ListingLifecycle.IN_PROGRESS, ListingLifecycle.FAILED_PERMANENT, ListingLifecycle.SKIPPED},
    ListingLifecycle.BLOCKED: {ListingLifecycle.IN_PROGRESS, ListingLifecycle.SKIPPED},
    ListingLifecycle.COMPLETED: set(),
    ListingLifecycle.FAILED_PERMANENT: set(),
    ListingLifecycle.SKIPPED: set(),
}


def normalize_decision(value: str | None) -> CanonicalDecision:
    raw = (value or CanonicalDecision.SKIPPED).strip().lower()
    if raw in CanonicalDecision._value2member_map_:
        return CanonicalDecision(raw)
    if raw in DECISION_ALIASES:
        return DECISION_ALIASES[raw]
    return CanonicalDecision.SKIPPED


def normalize_submission_outcome(value: str | None) -> SubmissionOutcome:
    raw = (value or SubmissionOutcome.UNKNOWN).strip().lower()
    if raw in SubmissionOutcome._value2member_map_:
        return SubmissionOutcome(raw)
    if raw in SUBMISSION_ALIASES:
        return SUBMISSION_ALIASES[raw]
    return SubmissionOutcome.UNKNOWN


def decision_rank(value: str | None) -> int:
    return DECISION_RANK[normalize_decision(value)]


def can_transition_listing_state(current: str | None, target: str | None) -> bool:
    current_state = ListingLifecycle(current or ListingLifecycle.PENDING)
    target_state = ListingLifecycle(target or ListingLifecycle.PENDING)
    if current_state == target_state:
        return True
    return target_state in VALID_TRANSITIONS[current_state]


@dataclass(slots=True)
class UnknownAliasWarning:
    field: str
    raw_value: str
    normalized_to: str


def unknown_alias_warning(field: str, raw: str | None, normalized: str) -> UnknownAliasWarning | None:
    if raw is None:
        return None
    lowered = raw.strip().lower()
    if field == "decision" and lowered not in CanonicalDecision._value2member_map_ and lowered not in DECISION_ALIASES:
        return UnknownAliasWarning(field=field, raw_value=raw, normalized_to=normalized)
    if field == "submission_outcome" and lowered not in SubmissionOutcome._value2member_map_ and lowered not in SUBMISSION_ALIASES:
        return UnknownAliasWarning(field=field, raw_value=raw, normalized_to=normalized)
    return None


def merge_status_values(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    decision = normalize_decision(left.get("decision"))
    if decision_rank(right.get("decision")) > decision_rank(decision):
        decision = normalize_decision(right.get("decision"))
    submitted = bool(left.get("submitted")) or bool(right.get("submitted"))
    submission_outcome = normalize_submission_outcome(right.get("submission_outcome") or left.get("submission_outcome"))
    merged = dict(left)
    merged.update({k: v for k, v in right.items() if v not in (None, "", [], {})})
    merged["decision"] = decision.value
    merged["submitted"] = submitted
    merged["submission_outcome"] = submission_outcome.value
    return merged
