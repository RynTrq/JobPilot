from __future__ import annotations

import os
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
OUTPUT_DIR = DATA_DIR / "outputs"
TEMPLATE_DIR = ROOT_DIR / "templates"


class ConfigBounds(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    mongo_uri: str = ""
    mongo_db: str = "jobpilot"
    classifier_threshold: float = Field(default=0.65, ge=0.0, le=1.0)
    generator_disabled: bool = True
    browser_domain_min_seconds: float = Field(default=4.0, ge=0.0, le=300.0)
    browser_human_delay_min_seconds: float = Field(default=0.15, ge=0.0, le=60.0)
    browser_human_delay_max_seconds: float = Field(default=0.45, ge=0.0, le=60.0)
    model_workers: int = Field(default=2, ge=1, le=32)
    model_request_timeout_seconds: float = Field(default=90.0, gt=0.0, le=600.0)
    form_debug_snapshots: bool = False
    approval_heartbeat_seconds: float = Field(default=15.0, gt=0.0, le=300.0)
    stage_timeout_default_seconds: float = Field(default=120.0, gt=1.0, le=1800.0)
    stage_timeout_extract_description_seconds: float = Field(default=90.0, gt=1.0, le=1800.0)
    stage_timeout_browser_action_seconds: float = Field(default=45.0, gt=1.0, le=600.0)
    max_retry_budget: int = Field(default=3, ge=1, le=10)
    dedup_threshold: float = Field(default=0.88, ge=0.0, le=1.0)
    analytics_lookback_days: int = Field(default=30, ge=1, le=3650)
    approval_timeout_seconds: float = Field(default=1800.0, gt=1.0, le=7200.0)
    manual_takeover_timeout_seconds: float = Field(default=3600.0, gt=1.0, le=14400.0)
    backup_before_migration: bool = False
    retention_pending_days: int = Field(default=14, ge=1, le=365)
    browser_test_mode: bool = False
    browser_heartbeat_seconds: float = Field(default=300.0, gt=1.0, le=3600.0)
    browser_post_navigation_wait_ms: int = Field(default=150, ge=0, le=10000)
    browser_domain_pacing_overrides: str = ""
    browser_stealth_level: int = Field(default=2, ge=0, le=2)
    browser_viewport_randomize: bool = True
    browser_locale: str = "en-US"
    browser_timezone: str = "America/New_York"
    deferred_questions_mode: bool = False
    retain_local_artifacts: bool = False

    @model_validator(mode="after")
    def _validate_relationships(self) -> "ConfigBounds":
        if self.browser_human_delay_min_seconds > self.browser_human_delay_max_seconds:
            raise ValueError("BROWSER_HUMAN_DELAY_MIN_SECONDS must be <= BROWSER_HUMAN_DELAY_MAX_SECONDS")
        return self


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default) != "0"


def _load_bounds() -> ConfigBounds:
    return ConfigBounds(
        host=os.getenv("JOBPILOT_HOST", "127.0.0.1"),
        port=int(os.getenv("JOBPILOT_PORT", "8765")),
        mongo_uri=os.getenv("MONGO_URI", ""),
        mongo_db=os.getenv("MONGO_DB", "jobpilot"),
        classifier_threshold=float(os.getenv("CLASSIFIER_THRESHOLD", "0.65")),
        generator_disabled=_flag("GENERATOR_DISABLED", "1"),
        browser_domain_min_seconds=float(os.getenv("BROWSER_DOMAIN_MIN_SECONDS", "4")),
        browser_human_delay_min_seconds=float(os.getenv("BROWSER_HUMAN_DELAY_MIN_SECONDS", "0.15")),
        browser_human_delay_max_seconds=float(os.getenv("BROWSER_HUMAN_DELAY_MAX_SECONDS", "0.45")),
        model_workers=max(1, int(os.getenv("JOBPILOT_MODEL_WORKERS", "2"))),
        model_request_timeout_seconds=float(os.getenv("JOBPILOT_MODEL_REQUEST_TIMEOUT_SECONDS", "90")),
        form_debug_snapshots=os.getenv("JOBPILOT_FORM_DEBUG_SNAPSHOTS", "0") == "1",
        approval_heartbeat_seconds=float(os.getenv("JOBPILOT_APPROVAL_HEARTBEAT_SECONDS", "15")),
        stage_timeout_default_seconds=float(os.getenv("JOBPILOT_STAGE_TIMEOUT_DEFAULT_SECONDS", "120")),
        stage_timeout_extract_description_seconds=float(os.getenv("JOBPILOT_STAGE_TIMEOUT_EXTRACT_DESCRIPTION_SECONDS", "90")),
        stage_timeout_browser_action_seconds=float(os.getenv("JOBPILOT_STAGE_TIMEOUT_BROWSER_ACTION_SECONDS", "45")),
        max_retry_budget=int(os.getenv("JOBPILOT_MAX_RETRY_BUDGET", "3")),
        dedup_threshold=float(os.getenv("JOBPILOT_DEDUP_THRESHOLD", "0.88")),
        analytics_lookback_days=int(os.getenv("JOBPILOT_ANALYTICS_LOOKBACK_DAYS", "30")),
        approval_timeout_seconds=float(os.getenv("JOBPILOT_APPROVAL_TIMEOUT_SECONDS", "1800")),
        manual_takeover_timeout_seconds=float(os.getenv("JOBPILOT_MANUAL_TAKEOVER_TIMEOUT_SECONDS", "3600")),
        backup_before_migration=os.getenv("JOBPILOT_BACKUP_BEFORE_MIGRATION", "0") != "0",
        retention_pending_days=int(os.getenv("JOBPILOT_RETENTION_PENDING_DAYS", "14")),
        browser_test_mode=os.getenv("JOBPILOT_BROWSER_TEST_MODE", "0") == "1",
        browser_heartbeat_seconds=float(os.getenv("JOBPILOT_BROWSER_HEARTBEAT_SECONDS", "300")),
        browser_post_navigation_wait_ms=int(os.getenv("JOBPILOT_BROWSER_POST_NAVIGATION_WAIT_MS", "150")),
        browser_domain_pacing_overrides=os.getenv("JOBPILOT_BROWSER_DOMAIN_PACING_OVERRIDES", ""),
        browser_stealth_level=int(os.getenv("JOBPILOT_BROWSER_STEALTH_LEVEL", "2")),
        browser_viewport_randomize=os.getenv("JOBPILOT_BROWSER_VIEWPORT_RANDOMIZE", "1") != "0",
        browser_locale=os.getenv("JOBPILOT_BROWSER_LOCALE", "en-US"),
        browser_timezone=os.getenv("JOBPILOT_BROWSER_TIMEZONE", "America/New_York"),
        deferred_questions_mode=os.getenv("DEFERRED_QUESTIONS_MODE", "0") == "1",
        retain_local_artifacts=os.getenv("JOBPILOT_RETAIN_LOCAL_ARTIFACTS", "0") == "1",
    )


BOUNDS = _load_bounds()


@dataclass(frozen=True)
class RuntimeSettings:
    dry_run: bool
    auto_submit_without_approval: bool
    classifier_auto_pass_when_above_threshold: bool
    live_mode: bool
    browser_headless: bool
    browser_persistent: bool
    browser_user_data_dir: Path


class RuntimeSettingsStore:
    def __init__(self, initial: RuntimeSettings):
        self._settings = initial
        self._lock = threading.RLock()

    def snapshot(self) -> RuntimeSettings:
        with self._lock:
            return self._settings

    def update(self, **changes: Any) -> RuntimeSettings:
        with self._lock:
            next_settings = replace(self._settings, **changes)
            if "live_mode" in changes:
                next_settings = replace(next_settings, browser_headless=not next_settings.live_mode)
            elif next_settings.live_mode:
                next_settings = replace(next_settings, browser_headless=False)
            self._settings = next_settings
            return next_settings

    def payload(self) -> dict[str, Any]:
        current = self.snapshot()
        payload = asdict(current)
        payload["browser_user_data_dir"] = str(current.browser_user_data_dir)
        payload["live_submit_enabled"] = not current.dry_run
        payload["final_review_required"] = not current.auto_submit_without_approval
        payload["dr_y_run_precedence"] = "dry_run_disables_final_submit_only"
        return payload


_INITIAL_LIVE_MODE = os.getenv("LIVE_MODE", "0") == "1"
_BROWSER_HEADLESS_ENV = os.getenv("BROWSER_HEADLESS")

RUNTIME_SETTINGS = RuntimeSettingsStore(
    RuntimeSettings(
        dry_run=os.getenv("DRY_RUN", "1") != "0",
        auto_submit_without_approval=os.getenv("AUTO_SUBMIT_WITHOUT_APPROVAL", "0") == "1",
        classifier_auto_pass_when_above_threshold=os.getenv("CLASSIFIER_AUTO_PASS", "0") == "1",
        live_mode=_INITIAL_LIVE_MODE,
        browser_headless=(_BROWSER_HEADLESS_ENV != "0") if _BROWSER_HEADLESS_ENV is not None else not _INITIAL_LIVE_MODE,
        browser_persistent=os.getenv("BROWSER_PERSISTENT", "1") != "0",
        browser_user_data_dir=Path(os.getenv("BROWSER_USER_DATA_DIR", str(DATA_DIR / "browser-profile"))),
    )
)


HOST = BOUNDS.host
PORT = BOUNDS.port
MONGO_URI = BOUNDS.mongo_uri
MONGO_DB = BOUNDS.mongo_db
CLASSIFIER_THRESHOLD = BOUNDS.classifier_threshold
GENERATOR_DISABLED = BOUNDS.generator_disabled
BROWSER_DOMAIN_MIN_SECONDS = BOUNDS.browser_domain_min_seconds
BROWSER_HUMAN_DELAY_MIN_SECONDS = BOUNDS.browser_human_delay_min_seconds
BROWSER_HUMAN_DELAY_MAX_SECONDS = BOUNDS.browser_human_delay_max_seconds
MODEL_WORKERS = BOUNDS.model_workers
MODEL_REQUEST_TIMEOUT_SECONDS = BOUNDS.model_request_timeout_seconds
FORM_DEBUG_SNAPSHOTS = BOUNDS.form_debug_snapshots
APPROVAL_HEARTBEAT_SECONDS = BOUNDS.approval_heartbeat_seconds
STAGE_TIMEOUT_DEFAULT_SECONDS = BOUNDS.stage_timeout_default_seconds
STAGE_TIMEOUT_EXTRACT_DESCRIPTION_SECONDS = BOUNDS.stage_timeout_extract_description_seconds
STAGE_TIMEOUT_BROWSER_ACTION_SECONDS = BOUNDS.stage_timeout_browser_action_seconds
MAX_RETRY_BUDGET = BOUNDS.max_retry_budget
DEDUP_THRESHOLD = BOUNDS.dedup_threshold
ANALYTICS_LOOKBACK_DAYS = BOUNDS.analytics_lookback_days
APPROVAL_TIMEOUT_SECONDS = BOUNDS.approval_timeout_seconds
MANUAL_TAKEOVER_TIMEOUT_SECONDS = BOUNDS.manual_takeover_timeout_seconds
BACKUP_BEFORE_MIGRATION = BOUNDS.backup_before_migration
RETENTION_PENDING_DAYS = BOUNDS.retention_pending_days
BROWSER_TEST_MODE = BOUNDS.browser_test_mode
BROWSER_HEARTBEAT_SECONDS = BOUNDS.browser_heartbeat_seconds
BROWSER_POST_NAVIGATION_WAIT_MS = BOUNDS.browser_post_navigation_wait_ms
BROWSER_DOMAIN_PACING_OVERRIDES = BOUNDS.browser_domain_pacing_overrides
BROWSER_STEALTH_LEVEL = BOUNDS.browser_stealth_level
BROWSER_VIEWPORT_RANDOMIZE = BOUNDS.browser_viewport_randomize
BROWSER_LOCALE = BOUNDS.browser_locale
BROWSER_TIMEZONE = BOUNDS.browser_timezone
DEFERRED_QUESTIONS_MODE = BOUNDS.deferred_questions_mode
RETAIN_LOCAL_ARTIFACTS = BOUNDS.retain_local_artifacts


def runtime_settings() -> RuntimeSettings:
    return RUNTIME_SETTINGS.snapshot()


def runtime_settings_payload() -> dict[str, Any]:
    return RUNTIME_SETTINGS.payload()


def update_runtime_settings(**changes: Any) -> RuntimeSettings:
    return RUNTIME_SETTINGS.update(**changes)


def live_submit_enabled() -> bool:
    if "DRY_RUN" in globals():
        return not bool(globals()["DRY_RUN"])
    return not runtime_settings().dry_run


def set_live_submit_enabled(enabled: bool) -> RuntimeSettings:
    return update_runtime_settings(dry_run=not enabled)


def auto_submit_without_approval_enabled() -> bool:
    if "AUTO_SUBMIT_WITHOUT_APPROVAL" in globals():
        return bool(globals()["AUTO_SUBMIT_WITHOUT_APPROVAL"])
    return runtime_settings().auto_submit_without_approval


def classifier_auto_pass_enabled() -> bool:
    return runtime_settings().classifier_auto_pass_when_above_threshold


def set_classifier_auto_pass_enabled(enabled: bool) -> RuntimeSettings:
    return update_runtime_settings(classifier_auto_pass_when_above_threshold=enabled)


def set_auto_submit_without_approval(enabled: bool) -> RuntimeSettings:
    return update_runtime_settings(auto_submit_without_approval=enabled)


def live_mode_enabled() -> bool:
    if "LIVE_MODE" in globals():
        return bool(globals()["LIVE_MODE"])
    return runtime_settings().live_mode


def set_live_mode_enabled(enabled: bool) -> RuntimeSettings:
    return update_runtime_settings(live_mode=enabled)


def browser_headless_enabled() -> bool:
    return runtime_settings().browser_headless


def browser_persistent_enabled() -> bool:
    return runtime_settings().browser_persistent


def browser_user_data_dir() -> Path:
    return runtime_settings().browser_user_data_dir


def validate_config() -> dict[str, Any]:
    payload = BOUNDS.model_dump()
    payload["runtime"] = runtime_settings_payload()
    return payload


def __getattr__(name: str) -> Any:
    current = runtime_settings()
    legacy = {
        "DRY_RUN": current.dry_run,
        "AUTO_SUBMIT_WITHOUT_APPROVAL": auto_submit_without_approval_enabled(),
        "LIVE_MODE": current.live_mode,
        "BROWSER_HEADLESS": current.browser_headless,
        "BROWSER_PERSISTENT": current.browser_persistent,
        "BROWSER_USER_DATA_DIR": current.browser_user_data_dir,
    }
    if name in legacy:
        return legacy[name]
    raise AttributeError(name)
