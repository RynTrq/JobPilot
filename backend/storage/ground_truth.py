"""Ground-truth schema and persistence helpers for candidate data."""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from backend.config import DATA_DIR

GROUND_TRUTH_PATH = DATA_DIR / "ground_truth.json"
BULLET_LIBRARY_PATH = DATA_DIR / "bullet_library.json"
MONTH_YEAR_RE = re.compile(r"^\d{4}-\d{2}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class StrictModel(BaseModel):
    """Base model that rejects extra fields to keep the schema exact."""

    model_config = ConfigDict(extra="forbid")


class Personal(StrictModel):
    full_name: str
    preferred_name: str
    email: str
    phone_e164: str
    location_city: str
    location_state: str | None = None
    location_country: str
    citizenship: str
    work_auth_us: str
    work_auth_eu: str
    linkedin_url: str
    github_url: str
    portfolio_url: str
    pronouns: str


class Education(StrictModel):
    institution: str
    degree: str
    field: str
    start_month_year: str
    end_month_year: str
    gpa: str | None = None
    honors: list[str] = Field(default_factory=list)
    relevant_courses: list[str] = Field(default_factory=list)


class Experience(StrictModel):
    id: str
    title: str
    company: str
    location: str
    start_month_year: str
    end_month_year: str
    summary_1line: str
    tech_stack: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)


class Project(StrictModel):
    id: str
    title: str
    summary_1line: str
    url: str
    tech_stack: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)


class Skills(StrictModel):
    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    ml: list[str] = Field(default_factory=list)
    soft: list[str] = Field(default_factory=list)


class Preferences(StrictModel):
    desired_roles: list[str] = Field(default_factory=list)
    desired_industries: list[str] = Field(default_factory=list)
    salary_min_usd_annual: int | None = None
    salary_expected_inr_lpa: int | None = None
    notice_period_days: int | None = None
    willing_to_relocate: bool
    earliest_start_date: str


class EEOC(StrictModel):
    gender: str
    race: str
    veteran: str
    disability: str
    hispanic_latino: str


class FreeformAnswers(StrictModel):
    why_this_role: str
    greatest_strength: str
    greatest_weakness: str
    five_year_goals: str


class GroundTruth(StrictModel):
    personal: Personal
    education: list[Education] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: Skills
    preferences: Preferences
    eeoc: EEOC
    freeform_answers: FreeformAnswers
    profile_statement: str
    custom: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> Self:
        """Load and validate ground truth from disk."""

        target = path or GROUND_TRUTH_PATH
        if not target.exists():
            raise FileNotFoundError(
                f"Ground truth file not found at {target}. Run `python scripts/seed_ground_truth.py` first."
            )
        data = json.loads(target.read_text(encoding="utf-8"))
        return cls.model_validate(data)

    def save(self, path: Path | None = None) -> Path:
        """Validate and persist ground truth, keeping the 20 newest backups."""

        target = path or GROUND_TRUTH_PATH
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.exists():
            backup_dir = target.parent / "ground_truth.backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).replace(microsecond=0).isoformat().replace(":", "-")
            backup_path = backup_dir / f"ground_truth_{stamp}.json"
            shutil.copy2(target, backup_path)
            backups = sorted(backup_dir.glob("ground_truth_*.json"))
            for old_backup in backups[:-20]:
                old_backup.unlink(missing_ok=True)

        payload = self.model_dump(mode="json")
        temp_path = target.with_suffix(f"{target.suffix}.tmp")
        temp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temp_path.replace(target)
        return target


class GroundTruthStore:
    """Compatibility wrapper used by the rest of the backend."""

    def __init__(self, path: Path | None = None):
        self.path = path or GROUND_TRUTH_PATH

    def _get_mongo(self):
        from backend.config import MONGO_URI, MONGO_DB
        if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("JOBPILOT_USE_REAL_MONGO") != "1":
            return None
        if MONGO_URI:
            from pymongo import MongoClient
            return MongoClient(MONGO_URI)[MONGO_DB]
        return None

    def read(self) -> dict[str, Any]:
        mongo = self._get_mongo()
        if mongo is not None:
            try:
                doc = mongo["candidate_profile"].find_one({"_id": "ground_truth"})
                if doc:
                    doc.pop("_id", None)
                    return doc
            except Exception:
                pass
        return GroundTruth.load(self.path).model_dump(mode="json")

    def read_if_exists(self) -> dict[str, Any]:
        mongo = self._get_mongo()
        if mongo is not None:
            try:
                doc = mongo["candidate_profile"].find_one({"_id": "ground_truth"})
                if doc:
                    doc.pop("_id", None)
                    return doc
            except Exception:
                pass
        if not self.path.exists():
            return {}
        return self.read()

    def write(self, data: dict[str, Any]) -> None:
        validated = GroundTruth.model_validate(data)
        payload = validated.model_dump(mode="json")
        mongo = self._get_mongo()
        if mongo is not None:
            try:
                mongo["candidate_profile"].replace_one({"_id": "ground_truth"}, payload, upsert=True)
            except Exception:
                pass
        validated.save(self.path)

    def fill_custom(self, question: str, answer: str) -> None:
        data = self.read_if_exists()
        if not data:
            data = empty_ground_truth().model_dump(mode="json")
        data.setdefault("custom", {})[normalize_question(question)] = answer
        self.write(data)


def normalize_question(question: str) -> str:
    """Normalize alarm questions into a stable dictionary key."""

    collapsed = re.sub(r"[^a-z0-9]+", "_", question.lower()).strip("_")
    return collapsed[:120]


def validate_month_year(value: str) -> str:
    """Validate a YYYY-MM string."""

    if not MONTH_YEAR_RE.fullmatch(value):
        raise ValueError("expected YYYY-MM")
    return value


def validate_date(value: str) -> str:
    """Validate a YYYY-MM-DD string."""

    if not DATE_RE.fullmatch(value):
        raise ValueError("expected YYYY-MM-DD")
    return value


def empty_ground_truth() -> GroundTruth:
    """Build an empty but schema-valid ground truth object."""

    return GroundTruth(
        personal=Personal(
            full_name="Md Raiyaan Tarique",
            preferred_name="Raiyaan",
            email="trqynzzz@gmail.com",
            phone_e164="+918799733317",
            location_city="New Delhi",
            location_state="Ghaffar Manzil Lane 1",
            location_country="India",
            citizenship="Indian",
            work_auth_us="I am not authorized to work in the US",
            work_auth_eu="I am not authorized to work in the EU",
            linkedin_url="https://www.linkedin.com/in/md-raiyaan-tarique-548956313/",
            github_url="https://github.com/RynTrq",
            portfolio_url="https://ryntrq.vercel.app",
            pronouns="he/him",
        ),
        education=[],
        experience=[],
        projects=[],
        skills=Skills(),
        preferences=Preferences(
            desired_roles=[],
            desired_industries=[],
            salary_min_usd_annual=None,
            salary_expected_inr_lpa=None,
            notice_period_days=0,
            willing_to_relocate=False,
            earliest_start_date="1970-01-01",
        ),
        eeoc=EEOC(
            gender="decline",
            race="decline",
            veteran="decline",
            disability="decline",
            hispanic_latino="decline",
        ),
        freeform_answers=FreeformAnswers(
            why_this_role="",
            greatest_strength="",
            greatest_weakness="",
            five_year_goals="",
        ),
        profile_statement="",
        custom={},
    )


def build_bullet_library_seed(ground_truth: GroundTruth) -> dict[str, list[dict[str, Any]]]:
    """Create seed bullet entries for every experience and project."""

    library: dict[str, list[dict[str, Any]]] = {}
    for entry in [*ground_truth.experience, *ground_truth.projects]:
        base_tags = list(dict.fromkeys([*entry.tech_stack, *entry.domains])) or ["seed"]
        stack_text = ", ".join(entry.tech_stack) if entry.tech_stack else "the available stack"
        domain_text = ", ".join(entry.domains) if entry.domains else "the available domain context"
        summary_text = entry.summary_1line.strip() or entry.title
        library[entry.id] = [
            {
                "id": f"{entry.id}_seed_{index}",
                "text": text,
                "tags": base_tags,
                "impact_type": "seed",
            }
            for index, text in enumerate(
                [
                    f"{entry.title}: {summary_text}",
                    f"Built {entry.title.lower()} work with {stack_text}.",
                    f"Applied {domain_text} to support {entry.title}.",
                    f"Focused on maintainable implementation and measurable outcomes for {entry.title}.",
                    f"Used {stack_text} to deliver {summary_text.lower()}.",
                    f"Documented and refined {entry.title} workflows for reuse.",
                ],
                start=1,
            )
        ]
    return library


def write_bullet_library_seed(
    ground_truth: GroundTruth,
    path: Path | None = None,
) -> Path:
    """Persist a seed bullet library."""

    target = path or BULLET_LIBRARY_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(build_bullet_library_seed(ground_truth), indent=2) + "\n",
        encoding="utf-8",
    )
    return target
