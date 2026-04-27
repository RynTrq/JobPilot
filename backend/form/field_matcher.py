from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import structlog


INDEX_PHRASES = {
    "personal.full_name": "full legal name name your name candidate name applicant name name as it appears on documents",
    "personal.preferred_name": "first name preferred name given name",
    "personal.email": "email address contact email",
    "personal.phone_e164": "phone number mobile telephone",
    "personal.linkedin_url": "linkedin profile url",
    "personal.github_url": "github profile url",
    "personal.portfolio_url": "portfolio website personal website",
    "personal.location_city": "city current location",
    "personal.location_country": "country current country",
    "personal.citizenship": "citizenship nationality",
    "personal.work_auth_us": "authorized to work united states visa sponsorship",
    "preferences.earliest_start_date": "start date availability notice period when can you start",
    "preferences.notice_period_days": "notice period days",
    "preferences.willing_to_relocate": "willing to relocate relocation",
    "preferences.salary_expected_inr_lpa": "expected salary compensation ctc",
    "eeoc.gender": "gender identity",
    "eeoc.race": "race ethnicity",
    "eeoc.veteran": "veteran protected veteran",
    "eeoc.disability": "disability status",
    "eeoc.hispanic_latino": "hispanic latino",
    "freeform_answers.why_this_role": "why are you interested in this role",
    "freeform_answers.greatest_strength": "greatest strength strengths",
    "freeform_answers.greatest_weakness": "greatest weakness weaknesses",
    "freeform_answers.five_year_goals": "five year goals career goals",
}


@dataclass
class MatchResult:
    path: str
    score: float
    alternates: list[tuple[str, float]]


log = structlog.get_logger()


class FieldMatcher:
    def __init__(self, encoder, ground_truth: dict[str, Any]):
        self.encoder = encoder
        self.ground_truth = ground_truth
        self.index = {path: phrase for path, phrase in INDEX_PHRASES.items() if resolve_path(ground_truth, path) is not None}
        self.paths = list(self.index)
        self.phrase_embs = encoder.encode_batch([self.index[path] for path in self.paths]) if self.paths else np.zeros((0, 384))

    def match(self, label: str) -> MatchResult:
        if not self.paths:
            return MatchResult("", 0.0, [])
        q = self.encoder.encode(label.lower().strip())
        sims = self.phrase_embs @ q
        top_idx = np.argsort(sims)[::-1][:3]
        result = MatchResult(
            path=self.paths[int(top_idx[0])],
            score=float(sims[int(top_idx[0])]),
            alternates=[(self.paths[int(i)], float(sims[int(i)])) for i in top_idx[1:]],
        )
        log.debug("field_match_scored", label=label, path=result.path, score=result.score, alternates=result.alternates)
        return result


def resolve_path(data: dict[str, Any], path: str) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur
