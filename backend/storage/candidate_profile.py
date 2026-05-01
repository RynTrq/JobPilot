from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
import yaml

from backend.config import DATA_DIR, GROUND_TRUTH_DIR


PROFILE_PATH = GROUND_TRUTH_DIR / "candidate_profile.yaml"
LEGACY_PROFILE_PATH = DATA_DIR / "My_Ground-info" / "profile" / "candidate_profile.yaml"
log = structlog.get_logger()


def resolve_profile_path(path: Path | None = None) -> Path:
    """Return the canonical candidate profile, falling back to the legacy path."""

    if path is not None:
        return path
    if PROFILE_PATH.exists() or not LEGACY_PROFILE_PATH.exists():
        return PROFILE_PATH
    return LEGACY_PROFILE_PATH


class CandidateProfileStore:
    def __init__(self, path: Path | None = None):
        self.path = path

    def read(self) -> dict[str, Any]:
        path = resolve_profile_path(self.path)
        if not path.exists():
            raise FileNotFoundError(f"missing candidate profile file at {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise RuntimeError(f"candidate profile file {path} must contain a mapping at the top level")
        return data

    def read_if_exists(self) -> dict[str, Any]:
        try:
            return self.read()
        except Exception as exc:
            log.exception("candidate_profile_read_failed", path=str(self.path), error=str(exc))
            return {}
