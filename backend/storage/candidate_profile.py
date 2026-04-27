from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
import yaml

from backend.config import ROOT_DIR


PROFILE_PATH = ROOT_DIR / "data" / "My_Ground-info" / "profile" / "candidate_profile.yaml"
log = structlog.get_logger()


class CandidateProfileStore:
    def __init__(self, path: Path | None = None):
        self.path = path or PROFILE_PATH

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            raise FileNotFoundError(f"missing candidate profile file at {self.path}")
        data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise RuntimeError(f"candidate profile file {self.path} must contain a mapping at the top level")
        return data

    def read_if_exists(self) -> dict[str, Any]:
        try:
            return self.read()
        except Exception as exc:
            log.exception("candidate_profile_read_failed", path=str(self.path), error=str(exc))
            return {}
