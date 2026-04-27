from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
import json
import re
from pathlib import Path
from typing import Any

import yaml


LEXICON_PATH = Path(__file__).with_name("expertise_lexicon.json")
DEFAULT_EXPERTISE_AREAS = ["sde", "full_stack_web_dev", "ai_engineering", "automation"]
YEAR_RANGE_RE = re.compile(
    r"\b(\d+)\s*(?:-|–|—|to)\s*(\d+)\s*(?:years?|yrs?)\b",
    re.IGNORECASE,
)
UP_TO_YEAR_RE = re.compile(r"\b(?:up\s+to|0\s*(?:-|–|—|to))\s*(\d+)\s*(?:years?|yrs?)\b", re.IGNORECASE)
YEAR_PATTERNS = [
    re.compile(r"\bminimum\s+of\s+(\d+)\s*(?:years?|yrs?)\b", re.IGNORECASE),
    re.compile(r"\bat\s+least\s+(\d+)\s*(?:years?|yrs?)\b", re.IGNORECASE),
    re.compile(r"\b(\d+)\s*\+?\s*(?:years?|yrs?)\b", re.IGNORECASE),
]
SENIORITY_RE = re.compile(
    r"senior|sr\.|staff|principal|lead\b|manager|director|architect|head of|vp\b|cto\b|cxo|"
    r"founding\s+(engineer|swe)|(ii|iii|iv|3|4|5)\s*$",
    re.IGNORECASE,
)
NEW_GRAD_EXCLUSION_RE = re.compile(
    r"new grads? not eligible|not an entry[- ]level role|entry[- ]level candidates? (?:are )?not eligible|"
    r"must have prior industry experience|prior industry experience required",
    re.IGNORECASE,
)
SECTION_HEADERS = (
    "responsibilit",
    "what you'll do",
    "what you will do",
    "requirement",
    "qualification",
    "must have",
    "nice to have",
    "about you",
    "who you are",
    "skills",
)


@dataclass(slots=True)
class CandidateFitFacts:
    experience_years_professional_product: float
    expertise_areas: list[str]
    latest_degree: str = ""
    latest_field: str = ""
    citizenship: str = ""
    authorized_countries: list[str] = field(default_factory=list)
    minimum_salary: float | None = None
    expected_salary: str | None = None
    relocation_window: str | None = None
    missing_structured_fields: list[str] = field(default_factory=list)
    fact_sources: dict[str, str] = field(default_factory=dict)


def load_expertise_lexicon(path: Path = LEXICON_PATH) -> dict[str, list[str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("expertise lexicon must be a mapping")
    normalized: dict[str, list[str]] = {}
    for area, keywords in payload.items():
        if not isinstance(area, str) or not isinstance(keywords, list) or not all(isinstance(item, str) for item in keywords):
            raise ValueError(f"invalid expertise lexicon entry: {area!r}")
        normalized[area] = keywords
    return normalized


def load_candidate_fit_facts(*, ground_truth_path: Path, profile_path: Path) -> CandidateFitFacts:
    ground_truth = _read_json(ground_truth_path)
    profile = _read_yaml(profile_path)
    missing: list[str] = []
    sources: dict[str, str] = {}

    experience_value = _first_present(
        profile,
        ("preferences", "experience_years_professional_product"),
        ("job_preferences", "experience_years_professional_product"),
        ("automation_rules", "role_matching", "experience_years_professional_product"),
    )
    if experience_value is None:
        experience_years = _derive_product_experience_years(profile)
        missing.append("preferences.experience_years_professional_product")
        sources["experience_years_professional_product"] = "derived_from_profile_experience"
    else:
        experience_years = _safe_float(experience_value, default=0.0)
        sources["experience_years_professional_product"] = "candidate_profile"

    expertise_areas = _first_present(profile, ("preferences", "expertise_areas"), ("job_preferences", "expertise_areas"))
    if not isinstance(expertise_areas, list) or not expertise_areas:
        expertise_areas = list(DEFAULT_EXPERTISE_AREAS)
        missing.append("preferences.expertise_areas")
        sources["expertise_areas"] = "run_default_pending_profile_diff"
    else:
        expertise_areas = [str(item) for item in expertise_areas]
        sources["expertise_areas"] = "candidate_profile"

    latest_education = _latest_education(profile, ground_truth)
    work_auth = profile.get("work_authorization") if isinstance(profile.get("work_authorization"), dict) else {}
    personal = ground_truth.get("personal") if isinstance(ground_truth.get("personal"), dict) else {}
    preferences = ground_truth.get("preferences") if isinstance(ground_truth.get("preferences"), dict) else {}

    return CandidateFitFacts(
        experience_years_professional_product=float(experience_years),
        expertise_areas=expertise_areas,
        latest_degree=str(latest_education.get("degree") or ""),
        latest_field=str(latest_education.get("field") or latest_education.get("major") or ""),
        citizenship=str(personal.get("citizenship") or ""),
        authorized_countries=[str(item) for item in work_auth.get("authorized_countries", [])],
        minimum_salary=_minimum_salary(profile, ground_truth),
        expected_salary=_string_or_none(preferences.get("expected_salary") or _first_present(profile, ("preferences", "expected_salary"))),
        relocation_window=_string_or_none(preferences.get("relocation_window") or _first_present(profile, ("preferences", "relocation_window"))),
        missing_structured_fields=missing,
        fact_sources=sources,
    )


def proposed_profile_diff(facts: CandidateFitFacts) -> str:
    additions: dict[str, Any] = {}
    if "preferences.experience_years_professional_product" in facts.missing_structured_fields:
        additions["experience_years_professional_product"] = facts.experience_years_professional_product
    if "preferences.expertise_areas" in facts.missing_structured_fields:
        additions["expertise_areas"] = facts.expertise_areas
    if not additions:
        return ""
    lines = [
        "--- proposed addition to candidate_profile.yaml ---",
        "preferences:",
    ]
    for key, value in additions.items():
        if isinstance(value, list):
            lines.append(f"  {key}:")
            lines.extend(f"    - {item}" for item in value)
        else:
            lines.append(f"  {key}: {value:g}" if isinstance(value, float) else f"  {key}: {value}")
    lines.append("--- end proposed addition ---")
    return "\n".join(lines) + "\n"


def decide_fit(
    *,
    title: str,
    jd_text: str,
    facts: CandidateFitFacts,
    lexicon: dict[str, list[str]] | None = None,
    top_requirements: list[str] | None = None,
    keywords_exact: list[str] | None = None,
) -> dict[str, Any]:
    lexicon = lexicon or load_expertise_lexicon()
    title = title or ""
    jd_text = jd_text or ""
    top_requirements = top_requirements or []
    keywords_exact = keywords_exact or []
    year_hits = _year_hits(jd_text, top_requirements)
    jd_min_years = max((hit["years"] for hit in year_hits), default=0)
    seniority_match = SENIORITY_RE.search(f"{title}\n{jd_text[:200]}")
    excludes_new_grads = bool(NEW_GRAD_EXCLUSION_RE.search(jd_text))
    rule_a_reasons: list[str] = []
    if jd_min_years > 0:
        rule_a_reasons.append(f"requires_min_years_{jd_min_years:g}")
    if seniority_match:
        rule_a_reasons.append("seniority_in_title")
    if excludes_new_grads:
        rule_a_reasons.append("excludes_new_grads")

    expertise_text = " ".join([title, *top_requirements, *keywords_exact]).lower().strip()
    if not expertise_text:
        expertise_text = f"{title} {jd_text}".lower()
    lexicon_hits = _lexicon_hits(expertise_text, facts.expertise_areas, lexicon)
    matched_area = next((area for area in facts.expertise_areas if lexicon_hits.get(area)), None)

    hard_fail_reasons = _hard_fail_reasons(jd_text, facts)
    if hard_fail_reasons:
        outcome = f"filtered_hard_fail_{hard_fail_reasons[0]}"
        fit = False
    elif rule_a_reasons:
        outcome = "filtered_seniority"
        fit = False
    elif not matched_area:
        outcome = "filtered_no_expertise_overlap"
        fit = False
    else:
        outcome = "dry_run_complete"
        fit = True

    return {
        "fit": fit,
        "submission_outcome": outcome,
        "jd_min_years": jd_min_years,
        "experience_years_professional_product": facts.experience_years_professional_product,
        "rule_a": {
            "passed": not rule_a_reasons,
            "reasons": rule_a_reasons,
            "year_hits": year_hits,
            "seniority_match": seniority_match.group(0) if seniority_match else None,
            "excludes_new_grads": excludes_new_grads,
        },
        "rule_b": {
            "passed": matched_area is not None,
            "matched_expertise_area": matched_area,
            "lexicon_hits": lexicon_hits,
        },
        "rule_c": {
            "passed": not hard_fail_reasons,
            "hard_fail_reasons": hard_fail_reasons,
        },
        "fact_sources": facts.fact_sources,
        "missing_structured_fields": facts.missing_structured_fields,
    }


def assess_parsing_coverage(*, visible_text: str, parsed_text: str) -> dict[str, Any]:
    visible = _normalize_text(visible_text)
    parsed = _normalize_text(parsed_text)
    ratio = _matching_character_coverage(parsed, visible)
    headers_in_visible = [header for header in SECTION_HEADERS if header in visible]
    missing_headers = [header for header in headers_in_visible if header not in parsed]
    return {
        "parsing_coverage_ratio": ratio,
        "headers_in_visible": headers_in_visible,
        "missing_headers": missing_headers,
        "passed": ratio >= 0.85 and not missing_headers,
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _first_present(root: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        current: Any = root
        for part in path:
            if not isinstance(current, dict) or part not in current:
                current = None
                break
            current = current[part]
        if current not in (None, ""):
            return current
    return None


def _latest_education(profile: dict[str, Any], ground_truth: dict[str, Any]) -> dict[str, Any]:
    education = profile.get("education")
    if isinstance(education, list) and education:
        latest = education[-1]
        return latest if isinstance(latest, dict) else {}
    education = ground_truth.get("education")
    if isinstance(education, list) and education:
        latest = education[-1]
        return latest if isinstance(latest, dict) else {}
    return {}


def _derive_product_experience_years(profile: dict[str, Any]) -> float:
    experience = profile.get("experience")
    if not isinstance(experience, list):
        return 0.0
    total = 0.0
    for item in experience:
        if not isinstance(item, dict):
            continue
        employment_type = str(item.get("employment_type") or item.get("type") or "").lower()
        environment = " ".join(str(value) for value in [item.get("environment"), item.get("domains"), item.get("summary_1line")] if value).lower()
        if "full" in employment_type and "product" in environment:
            total += _duration_years(item)
    return round(total, 2)


def _duration_years(item: dict[str, Any]) -> float:
    start = str(item.get("start_month_year") or item.get("start_date") or "")
    end = str(item.get("end_month_year") or item.get("end_date") or "")
    try:
        start_year, start_month = [int(part) for part in start[:7].split("-")]
        end_year, end_month = [int(part) for part in end[:7].split("-")]
    except Exception:
        return 0.0
    return max(0.0, ((end_year - start_year) * 12 + (end_month - start_month) + 1) / 12)


def _safe_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _minimum_salary(profile: dict[str, Any], ground_truth: dict[str, Any]) -> float | None:
    value = _first_present(profile, ("preferences", "minimum_salary"), ("compensation_policy", "minimum_salary"))
    if value is None:
        preferences = ground_truth.get("preferences") if isinstance(ground_truth.get("preferences"), dict) else {}
        value = preferences.get("minimum_salary")
    if value in (None, ""):
        return None
    return _safe_float(value, default=0.0)


def _string_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _year_hits(jd_text: str, top_requirements: list[str]) -> list[dict[str, Any]]:
    haystack = "\n".join([jd_text, *top_requirements])
    hits: list[dict[str, Any]] = []
    seen_spans: list[tuple[int, int]] = []
    for match in YEAR_RANGE_RE.finditer(haystack):
        lower = int(match.group(1))
        upper = int(match.group(2))
        years = min(lower, upper)
        span = match.span()
        seen_spans.append(span)
        hits.append({"years": years, "text": match.group(0), "start": match.start(), "upper_years": max(lower, upper)})
    for match in UP_TO_YEAR_RE.finditer(haystack):
        span = match.span()
        if any(span[0] >= old[0] and span[1] <= old[1] for old in seen_spans):
            continue
        seen_spans.append(span)
        hits.append({"years": 0, "text": match.group(0), "start": match.start(), "upper_years": int(match.group(1))})
    for pattern in YEAR_PATTERNS:
        for match in pattern.finditer(haystack):
            years = int(match.group(1))
            span = match.span()
            if any(span[0] >= old[0] and span[1] <= old[1] for old in seen_spans):
                continue
            seen_spans.append(span)
            hits.append({"years": years, "text": match.group(0), "start": match.start()})
    return hits


def _lexicon_hits(text: str, areas: list[str], lexicon: dict[str, list[str]]) -> dict[str, list[str]]:
    hits: dict[str, list[str]] = {}
    for area in areas:
        keywords = lexicon.get(area, [])
        area_hits = [keyword for keyword in keywords if keyword.lower() in text]
        hits[area] = area_hits
    return hits


def _hard_fail_reasons(jd_text: str, facts: CandidateFitFacts) -> list[str]:
    text = jd_text.lower()
    reasons: list[str] = []
    if _degree_hard_fail(text, facts.latest_degree):
        reasons.append("degree_requirement")
    if re.search(r"\b(ts/sci|top secret|secret clearance|polygraph|dv clearance|nato clearance)\b", text):
        reasons.append("clearance")
    if _citizenship_hard_fail(text, facts):
        reasons.append("citizenship")
    if _salary_hard_fail(text, facts.minimum_salary):
        reasons.append("salary_floor")
    return reasons


def _degree_hard_fail(text: str, latest_degree: str) -> bool:
    degree = latest_degree.lower()
    has_bachelors = "bachelor" in degree or "b.tech" in degree or "technology" in degree
    if re.search(r"\bph\.?d\b|\bdoctorate\b", text) and re.search(r"\brequired\b|\bmust have\b", text):
        return True
    if has_bachelors:
        masters_required = re.search(r"\b(master'?s?|m\.s\.|msc)\b.{0,80}\b(required|must have)\b", text)
        required_masters = re.search(r"\b(required|must have)\b.{0,80}\b(master'?s?|m\.s\.|msc)\b", text)
        return bool(masters_required or required_masters)
    return False


def _citizenship_hard_fail(text: str, facts: CandidateFitFacts) -> bool:
    citizen = facts.citizenship.lower()
    if re.search(r"\bus citizen only\b|\bu\.s\. citizen only\b|\bmust be (?:a )?u\.?s\.? citizen\b", text):
        return not any(token in citizen for token in ("united states", "u.s.", "us citizen", "american"))
    if re.search(r"\beu citizen only\b|\bmust be (?:an? )?eu citizen\b|\bpermanent resident required\b", text):
        return True
    return False


def _salary_hard_fail(text: str, minimum_salary: float | None) -> bool:
    if minimum_salary is None:
        return False
    salaries = [float(match.group(1).replace(",", "")) for match in re.finditer(r"(?:salary|compensation|pay|base)[^\n]{0,40}?(\d[\d,]{3,})", text)]
    return bool(salaries and max(salaries) < minimum_salary)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _matching_character_coverage(parsed: str, visible: str) -> float:
    if not visible:
        return 1.0 if not parsed else 0.0
    matcher = SequenceMatcher(None, parsed, visible, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return round(matched / len(visible), 4)
