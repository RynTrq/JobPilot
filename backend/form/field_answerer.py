from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import numpy as np
import structlog

from backend.config import DATA_DIR, GROUND_TRUTH_DIR
from backend.specialists.base import SpecialistContext
from backend.specialists.form_freetext import FormFreeTextSpecialist
from backend.specialists.translator import Translator
from backend.storage.learned_answers import lookup_learned_answer, store_learned_answer, store_pending_question
from backend.storage.candidate_profile import CandidateProfileStore
from backend.storage.ground_truth import GroundTruthStore

log = structlog.get_logger()

YES_VARIANTS = ["yes", "y", "true", "1", "absolutely", "correct", "affirmative", "i am", "i do", "i have", "i will"]
NO_VARIANTS = ["no", "n", "false", "0", "no i am not", "i do not", "i am not", "i have not", "i will not"]
DEGREE_KEYWORDS = {
    "bachelor": ["bachelor", "b.s.", "b.a.", "bs", "ba", "undergraduate", "4-year"],
    "master": ["master", "m.s.", "m.a.", "ms", "ma", "graduate", "postgraduate"],
    "phd": ["phd", "ph.d", "doctorate", "doctoral"],
    "associate": ["associate", "a.s.", "a.a."],
    "high school": ["high school", "secondary", "ged", "diploma"],
}
GENERATIVE_FIELD_INDICATORS = [
    "why do you want",
    "why are you interested",
    "tell us about yourself",
    "describe a time",
    "describe a challenge",
    "what motivates you",
    "what are your strengths",
    "what are your weaknesses",
    "how would you",
    "what is your experience with",
    "what do you know about",
    "what makes you a good fit",
    "additional information",
    "anything else",
    "cover letter",
    "personal statement",
    "objective",
    "summary",
]


def _callable_name(name: str) -> str:
    return f"callable:{name}"


FIELD_LOOKUP_TABLE: dict[tuple[str, ...], Any] = {
    ("first name", "given name", "first_name"): "candidate.personal.first_name",
    ("last name", "family name", "surname", "last_name"): "candidate.personal.last_name",
    ("full name", "your name", "legal name", "name"): "candidate.personal.full_name",
    ("email", "email address", "e-mail"): "candidate.personal.email",
    ("phone", "phone number", "mobile", "telephone", "cell"): "candidate.personal.phone",
    ("linkedin", "linkedin profile", "linkedin url", "linkedin link"): "candidate.personal.linkedin_url",
    ("github", "github profile", "github url"): "candidate.personal.github_url",
    ("portfolio", "personal website", "personal site", "website url"): "candidate.personal.portfolio_url",
    # "Other links" fields (Workable and others) expect one URL — use the portfolio/website
    # URL, since LinkedIn and GitHub have their own dedicated fields on those forms.
    ("other links", "other link", "additional links", "additional link", "other websites", "other urls", "other url"): "candidate.personal.portfolio_url",
    ("address", "street address", "home address"): "candidate.personal.address",
    ("city",): "candidate.personal.city",
    ("state", "province", "region"): "candidate.personal.state",
    ("zip", "postal code", "zip code", "postcode"): "candidate.personal.zip_code",
    ("country",): "candidate.personal.country",
    ("authorized to work", "work authorization", "legally authorized", "eligible to work", "right to work", "work permit"): "candidate.work_authorization.authorized_to_work",
    ("require sponsorship", "visa sponsorship", "need sponsorship", "require visa", "work visa"): "candidate.work_authorization.requires_sponsorship",
    ("visa status", "immigration status", "current visa"): "candidate.work_authorization.visa_status",
    ("citizenship", "citizen", "national"): "candidate.work_authorization.citizenship",
    ("university", "college", "school name", "institution", "school attended"): "candidate.education.latest.institution",
    ("degree", "degree type", "level of education", "education level", "highest degree", "highest education"): "candidate.education.latest.degree",
    ("major", "field of study", "area of study", "concentration", "discipline"): "candidate.education.latest.major",
    ("minor",): "candidate.education.latest.minor",
    ("gpa", "grade point average", "cumulative gpa"): "candidate.education.latest.gpa",
    ("graduation date", "graduation year", "expected graduation", "degree completion date", "year of graduation"): "candidate.education.latest.graduation_date",
    ("education start date", "school start date", "university start date"): "candidate.education.latest.start_date",
    ("current employer", "current company", "current organization", "where do you work", "employer name"): "candidate.experience.current.company",
    ("current title", "current position", "current job title", "your title", "your role"): "candidate.experience.current.title",
    ("years of experience", "total experience", "how many years", "years in field"): _callable_name("years_of_experience"),
    ("notice period", "availability", "when can you start", "start date availability", "earliest start"): "candidate.preferences.notice_period",
    ("salary expectation", "expected salary", "desired salary", "compensation expectation", "salary requirement"): "candidate.preferences.expected_salary",
    ("current salary", "current compensation", "current ctc"): "candidate.preferences.current_salary",
    ("gender", "sex"): "candidate.eeoc.gender",
    ("race", "ethnicity", "racial"): "candidate.eeoc.race",
    ("veteran", "veteran status", "military service", "armed forces"): "candidate.eeoc.veteran_status",
    ("disability", "disabled", "disability status"): "candidate.eeoc.disability_status",
    ("pronouns",): "candidate.eeoc.pronouns",
    ("how did you hear", "how did you find", "referral source", "heard about", "source of application", "how did you learn about"): "candidate.preferences.how_did_you_hear",
    ("agree", "terms", "conditions", "policy", "consent", "acknowledge", "certify", "confirm that"): True,
}


@dataclass
class AnswerResult:
    answer: str | None
    tier: int | None


def normalize_label(text: str) -> str:
    lowered = re.sub(r"[^a-z0-9 ]+", " ", str(text or "").lower())
    return " ".join(lowered.split()).strip()


def resolve_data_path(data: dict[str, Any], path_string: str) -> Any:
    cur: Any = data
    for part in path_string.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def tier1_lookup(normalized_label: str, field_type: str, candidate_data: dict) -> str | None:
    normalized = normalize_label(normalized_label)
    for patterns, data_key_path in FIELD_LOOKUP_TABLE.items():
        for pattern in patterns:
            if not _pattern_matches_label(pattern, normalized):
                continue
            if data_key_path is True:
                return "Yes" if field_type == "checkbox" else "True"
            if isinstance(data_key_path, str) and data_key_path.startswith("callable:"):
                value = _resolve_callable_value(data_key_path, candidate_data)
            else:
                value = resolve_data_path(candidate_data, str(data_key_path))
            if value is not None and str(value).strip():
                return str(value)
    return None


def find_best_option_match(intended: str, options: list[str]) -> str | None:
    intended_lower = intended.lower().strip()
    if intended_lower in YES_VARIANTS:
        for option in options:
            if "yes" in option.lower():
                return option
    if intended_lower in NO_VARIANTS:
        for option in options:
            if re.fullmatch(r".*\bno\b.*", option.lower()):
                return option

    for degree_name, keywords in DEGREE_KEYWORDS.items():
        if degree_name in intended_lower or any(keyword in intended_lower for keyword in keywords):
            for option in options:
                option_lower = option.lower()
                if degree_name in option_lower or any(keyword in option_lower for keyword in keywords):
                    return option

    for opt in options:
        if opt.lower().strip() == intended_lower:
            return opt
    if re.fullmatch(r"[a-z0-9 ]+", intended_lower):
        word_pattern = re.compile(rf"(^|\W){re.escape(intended_lower)}(\W|$)")
        for opt in options:
            if word_pattern.search(opt.lower()):
                return opt
    for opt in options:
        if intended_lower in opt.lower():
            return opt
    for opt in options:
        if opt.lower().strip() in intended_lower:
            return opt
    intended_words = set(intended_lower.split())
    best_score = 0
    best_opt = None
    for opt in options:
        opt_words = set(opt.lower().split())
        score = len(intended_words & opt_words)
        if score > best_score:
            best_score = score
            best_opt = opt
    if best_score > 0:
        return best_opt

    # Levenshtein distance fallback: find the closest option by edit distance.
    # This handles cases where options use slightly different phrasing or abbreviations
    # that substring and word-overlap matching miss entirely.
    best_dist = float("inf")
    best_lev_opt = None
    for opt in options:
        dist = _levenshtein_distance(intended_lower, opt.lower().strip())
        if dist < best_dist:
            best_dist = dist
            best_lev_opt = opt
    # Only accept if the distance is reasonable (less than 40% of the longer string)
    if best_lev_opt is not None:
        max_len = max(len(intended_lower), len(best_lev_opt))
        if max_len > 0 and best_dist / max_len < 0.4:
            return best_lev_opt
    return None


def _match_original_option(intended: str, original_options: list[str], lookup_options: list[str]) -> str | None:
    if not original_options:
        return None
    match = find_best_option_match(intended, lookup_options or original_options)
    if match is None:
        return None
    if lookup_options and match in lookup_options:
        idx = lookup_options.index(match)
        if idx < len(original_options):
            return original_options[idx]
    return match


def _match_original_options(intended: str, original_options: list[str], lookup_options: list[str]) -> str | None:
    if not original_options or not intended:
        return None
    matched: list[str] = []
    for part in re.split(r"[,;/]|\band\b|\n", str(intended), flags=re.IGNORECASE):
        cleaned = part.strip()
        if not cleaned:
            continue
        match = _match_original_option(cleaned, original_options, lookup_options)
        if match and match not in matched:
            matched.append(match)
    if matched:
        return ", ".join(matched)
    return _match_original_option(intended, original_options, lookup_options)


PHONE_COUNTRY_CODE_TERMS = ("phone", "telephone", "mobile", "cell", "tel", "dial", "calling")

DIAL_CODES_BY_COUNTRY = {
    "india": "+91",
    "in": "+91",
    "united states": "+1",
    "united states of america": "+1",
    "usa": "+1",
    "us": "+1",
    "canada": "+1",
    "united arab emirates": "+971",
    "uae": "+971",
    "united kingdom": "+44",
    "uk": "+44",
    "great britain": "+44",
    "singapore": "+65",
    "germany": "+49",
    "france": "+33",
    "netherlands": "+31",
    "australia": "+61",
}


def _phone_country_code_answer(normalized_label: str, options: list[str], candidate_data: dict[str, Any]) -> str | None:
    if not _looks_like_phone_country_code_label(normalized_label):
        return None
    dial_code = _candidate_dial_code(candidate_data, options)
    if not dial_code:
        return None
    if options:
        if not _looks_like_country_code_options(options):
            return None
        return _match_dial_code_option(dial_code, options)
    return dial_code


def _looks_like_phone_country_code_label(normalized_label: str) -> bool:
    if "country" not in normalized_label:
        return False
    return any(term in normalized_label for term in PHONE_COUNTRY_CODE_TERMS)


def _looks_like_country_code_options(options: list[str]) -> bool:
    with_plus_code = sum(1 for option in options if re.search(r"\+\s*\d{1,4}\b", str(option)))
    return with_plus_code >= max(1, min(3, len(options) // 8))


def _match_dial_code_option(dial_code: str, options: list[str]) -> str:
    pattern = re.compile(rf"(^|[^\d])\+?\s*{re.escape(dial_code.lstrip('+'))}\b")
    for option in options:
        if pattern.search(str(option)):
            return option
    return dial_code


def _candidate_dial_code(candidate_data: dict[str, Any], options: list[str]) -> str | None:
    personal = candidate_data.get("candidate", {}).get("personal", {})
    phone = str(personal.get("phone", "") or "")
    country_code = _dial_code_for_country(
        str(personal.get("country") or personal.get("location_country") or personal.get("citizenship") or "")
    )
    if country_code:
        return country_code
    if not phone.startswith("+"):
        return None
    phone_digits = re.sub(r"\D+", "", phone)
    if not phone_digits:
        return None
    option_codes = sorted(
        {
            match.group(1)
            for option in options
            for match in re.finditer(r"\+\s*(\d{1,4})\b", str(option))
        },
        key=len,
        reverse=True,
    )
    for code in option_codes:
        if phone_digits.startswith(code):
            return f"+{code}"
    known_codes = sorted({code.lstrip("+") for code in DIAL_CODES_BY_COUNTRY.values()}, key=len, reverse=True)
    for code in known_codes:
        if phone_digits.startswith(code):
            return f"+{code}"
    return None


def _dial_code_for_country(country: str) -> str | None:
    normalized = re.sub(r"\s+", " ", country).strip().lower()
    if not normalized:
        return None
    return DIAL_CODES_BY_COUNTRY.get(normalized)


def _levenshtein_distance(s1: str, s2: str) -> int:
    """Compute the Levenshtein edit distance between two strings.

    Pure Python implementation to avoid adding a dependency. For the
    short strings typical in form option labels (<100 chars), performance
    is more than adequate.
    """
    if len(s1) < len(s2):
        return _levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = prev_row[j + 1] + 1
            deletions = curr_row[j] + 1
            substitutions = prev_row[j] + (c1 != c2)
            curr_row.append(min(insertions, deletions, substitutions))
        prev_row = curr_row
    return prev_row[-1]


def build_candidate_corpus(candidate_data: dict[str, Any]) -> list[tuple[str, str]]:
    latest_education = candidate_data.get("candidate", {}).get("education", {}).get("latest", {})
    current_experience = candidate_data.get("candidate", {}).get("experience", {}).get("current", {})
    work_auth = candidate_data.get("candidate", {}).get("work_authorization", {})
    preferences = candidate_data.get("candidate", {}).get("preferences", {})
    personal = candidate_data.get("candidate", {}).get("personal", {})
    eeoc = candidate_data.get("candidate", {}).get("eeoc", {})
    corpus = [
        ("the applicant's first name", personal.get("first_name", "")),
        ("the applicant's last name", personal.get("last_name", "")),
        ("the applicant's full legal name", personal.get("full_name", "")),
        ("the applicant's email address", personal.get("email", "")),
        ("the applicant's phone number", personal.get("phone", "")),
        ("the applicant's linkedin profile url", personal.get("linkedin_url", "")),
        ("the applicant's github profile url", personal.get("github_url", "")),
        ("the applicant's portfolio website", personal.get("portfolio_url", "")),
        ("the city where the applicant lives", personal.get("city", "")),
        ("the country where the applicant lives", personal.get("country", "")),
        ("the university or college the applicant attended", latest_education.get("institution", "")),
        ("the applicant's degree type such as bachelor or master", latest_education.get("degree", "")),
        ("the applicant's area of study or major at university", latest_education.get("major", "")),
        ("the applicant's graduation date", latest_education.get("graduation_date", "")),
        ("the applicant's current employer", current_experience.get("company", "")),
        ("the applicant's current role title", current_experience.get("title", "")),
        ("how many years of professional experience the applicant has", _resolve_callable_value(_callable_name("years_of_experience"), candidate_data)),
        ("whether the applicant is authorized to work in this country", work_auth.get("authorized_to_work", "")),
        ("whether the applicant needs visa sponsorship", work_auth.get("requires_sponsorship", "")),
        ("the applicant's visa status", work_auth.get("visa_status", "")),
        ("the applicant's salary expectations", preferences.get("expected_salary", "")),
        ("when the applicant can start working or their notice period", preferences.get("notice_period", "")),
        ("the applicant's gender disclosure", eeoc.get("gender", "")),
        ("the applicant's race or ethnicity disclosure", eeoc.get("race", "")),
        ("the applicant's veteran status", eeoc.get("veteran_status", "")),
        ("the applicant's disability status", eeoc.get("disability_status", "")),
    ]
    return [(description, str(value)) for description, value in corpus if str(value or "").strip()]


def precompute_corpus_embeddings(encoder, candidate_data: dict[str, Any]) -> tuple[np.ndarray, list[tuple[str, str]]]:
    corpus = build_candidate_corpus(candidate_data)
    if not corpus:
        return np.zeros((0, 384), dtype=np.float32), []
    embeddings = np.asarray(encoder.encode_batch([item[0] for item in corpus]), dtype=np.float32)
    return embeddings, corpus


def tier2_semantic_match(field_label: str, encoder, corpus_embeddings, corpus, threshold: float = 0.75) -> str | None:
    if corpus_embeddings is None or len(corpus) == 0:
        return None
    query_embedding = np.asarray(encoder.encode(field_label), dtype=np.float32)
    similarities = np.asarray(corpus_embeddings @ query_embedding, dtype=np.float32)
    best_idx = int(np.argmax(similarities))
    best_score = float(similarities[best_idx])
    if best_score >= threshold:
        log.debug("tier2_match", label=field_label, matched_description=corpus[best_idx][0], score=best_score)
        return corpus[best_idx][1]
    log.debug("tier2_no_match", label=field_label, best_score=best_score)
    return None


async def generate_answer_with_ai(field_label: str, field_type: str, job_context: dict, candidate_data: dict, generator) -> str | None:
    candidate = candidate_data.get("candidate", {})
    relevant_projects = _select_relevant_projects(candidate_data, job_context.get("jd", ""))
    source_blocks = {
        "job_description_text": job_context.get("jd", ""),
        "job_metadata": {"title": job_context.get("title", ""), "company": job_context.get("company", ""), "field_type": field_type},
        "candidate_profile": candidate,
        "relevant_projects": relevant_projects,
    }
    try:
        if hasattr(generator, "grounded_answer"):
            specialist = FormFreeTextSpecialist(generator)
            result = await asyncio.wait_for(
                specialist.run(
                    {
                        "question": field_label,
                        "source_blocks": source_blocks,
                        "min_confidence": 0.95 if _is_legal_or_eligibility(field_label) else 0.9,
                    },
                    SpecialistContext(
                        correlation_id=str(job_context.get("correlation_id") or "field-answerer"),
                        job_url=job_context.get("url"),
                    ),
                ),
                timeout=60.0,
            )
            answer_payload = result.payload
            trace_payload = result.provenance
        else:
            completion = generator.complete(
                "Answer only from the provided source blocks.",
                json.dumps({"question": field_label, "source_blocks": source_blocks}, sort_keys=True),
                max_tokens=120,
                temperature=0.0,
            )
            if inspect.isawaitable(completion):
                text = await asyncio.wait_for(completion, timeout=60.0)
            else:
                text = completion
            result = {
                "body": text,
                "citations": [],
            }
            answer_payload = result
            trace_payload = {"confidence": 0.0, "fallback_reason": "legacy_generator_fallback"}
        cleaned = str(answer_payload.get("body") or "").strip()
        if cleaned.upper() in {"NULL", "NULL.", "N/A", "NONE", "UNKNOWN", "NOT APPLICABLE"}:
            return None
        if len(cleaned.split()) < 3:
            return None
        job_context.setdefault("generation_trace", {})[field_label] = {
            "source_keys_used": answer_payload.get("citations", []),
            "confidence": trace_payload.get("confidence", 0.0),
            "fallback_reason": trace_payload.get("fallback_reason", ""),
        }
        return cleaned
    except concurrent.futures.TimeoutError:
        log.error("generator_timeout", field=field_label)
        return None
    except Exception as exc:
        log.warning("tier3_generation_failed", label=field_label, error=str(exc))
        return None


def _is_legal_or_eligibility(field_label: str) -> bool:
    lowered = field_label.lower()
    return any(token in lowered for token in ("visa", "sponsorship", "authorized", "eligibility", "citizenship", "work permit"))


# ---------------------------------------------------------------------------
# Section-aware question routing
# ---------------------------------------------------------------------------
# Maps question categories to keywords. When a question's label matches a
# category, we can short-circuit by pulling the answer directly from the
# matching source block rather than running full AI generation.

QUESTION_SECTIONS: dict[str, list[str]] = {
    "personal_information": ["name", "email", "phone", "address", "linkedin", "github", "portfolio", "website"],
    "legal_authorization": ["authorized", "visa", "sponsor", "citizen", "permit", "eligib", "right to work", "legally"],
    "work_preferences": ["remote", "onsite", "hybrid", "relocate", "travel", "work arrangement", "willing to", "available to"],
    "experience": ["years", "experience with", "proficiency", "familiar", "do you have experience"],
    "salary": ["salary", "compensation", "pay", "rate", "expected", "ctc", "current salary"],
    "education": ["degree", "university", "college", "gpa", "graduation", "school", "major", "field of study"],
    "eeoc": ["gender", "race", "ethnicity", "veteran", "disability", "demographic", "pronoun"],
}


def classify_question_section(label: str) -> str | None:
    """Classify a question label into a known section for targeted routing."""
    lowered = label.lower()
    for section, keywords in QUESTION_SECTIONS.items():
        if any(keyword in lowered for keyword in keywords):
            return section
    return None


# ---------------------------------------------------------------------------
# AI answer validation & repair
# ---------------------------------------------------------------------------

def validate_and_repair_answer(answer: str, field_type: str, field_label: str, field_options: list[str] | None = None) -> str | None:
    """Validate and repair AI-generated answers based on field type constraints.

    Ensures numeric questions get numeric answers, yes/no questions get boolean
    answers, and option-based fields get valid selections.
    """
    if not answer or not answer.strip():
        return None

    cleaned = answer.strip()
    label_lower = field_label.lower()

    # Numeric fields: extract the first number from the answer
    if field_type in ("number", "tel") or any(kw in label_lower for kw in ("how many", "years of", "number of", "salary", "compensation")):
        nums = re.findall(r"[\d.]+", cleaned)
        if nums:
            return nums[0]

    # Yes/No radio/select: normalize to match options
    if field_type in ("radio", "select", "dropdown") and field_options:
        option_texts = [opt.lower().strip() for opt in field_options]
        if all(opt in ("yes", "no", "true", "false") for opt in option_texts):
            for yes_word in YES_VARIANTS:
                if yes_word in cleaned.lower():
                    return find_best_option_match("Yes", field_options) or "Yes"
            for no_word in NO_VARIANTS:
                if no_word in cleaned.lower():
                    return find_best_option_match("No", field_options) or "No"

    # Checkbox groups should return concrete option labels, not a generic truthy
    # value, so the filler can tick the same choices the real form exposes.
    if field_type == "checkbox" and field_options:
        matched = _match_original_options(cleaned, field_options, field_options)
        if matched:
            return matched

    # Single checkbox fields should return truthy values
    if field_type == "checkbox":
        for yes_word in YES_VARIANTS:
            if yes_word in cleaned.lower():
                return "Yes"
        return "Yes"  # Default to checking the box

    return cleaned


async def get_answer_for_field(
    field_label: str,
    field_type: str,
    field_options: list[str],
    job_context: dict,
    candidate_data: dict,
    encoder,
    corpus_embeddings,
    corpus: list,
    generator,
) -> str | None:
    result = await get_answer_for_field_with_metadata(
        field_label=field_label,
        field_type=field_type,
        field_options=field_options,
        job_context=job_context,
        candidate_data=candidate_data,
        encoder=encoder,
        corpus_embeddings=corpus_embeddings,
        corpus=corpus,
        generator=generator,
    )
    return result.answer


async def get_answer_for_field_with_metadata(
    *,
    field_label: str,
    field_type: str,
    field_options: list[str],
    job_context: dict,
    candidate_data: dict,
    encoder,
    corpus_embeddings,
    corpus: list,
    generator,
) -> AnswerResult:
    translator = job_context.get("translator") if isinstance(job_context.get("translator"), Translator) else Translator(job_context.get("sqlite"))
    lookup_label = field_label
    lookup_options = list(field_options)
    detected_language = translator.detect(field_label)
    if detected_language not in ("en", "und"):
        lookup_label = translator.translate(field_label, detected_language, "en")
        lookup_options = translator.translate_options(field_options, detected_language, "en")
        job_context.setdefault("translation_trace", {})[field_label] = {
            "applied": True,
            "from_lang": detected_language,
            "to_lang": "en",
            "field_label_en": lookup_label,
            "options_en": lookup_options,
            "translator": translator.translator_name,
        }
    normalized_label = normalize_label(lookup_label)
    phone_country_code_answer = _phone_country_code_answer(normalized_label, lookup_options, candidate_data)
    if phone_country_code_answer is not None:
        return AnswerResult(answer=phone_country_code_answer, tier=1)
    learned_answers_enabled = bool(job_context.get("enable_learned_answers"))
    if learned_answers_enabled:
        learned_answer = lookup_learned_answer(field_label, field_type)
        if learned_answer is None and lookup_label != field_label:
            learned_answer = lookup_learned_answer(lookup_label, field_type)
        if learned_answer is not None:
            log.info("answered_from_learned_answers", label=field_label[:60])
            if field_type in ("dropdown", "radio", "select") and field_options:
                learned_answer = _match_original_option(learned_answer, field_options, lookup_options) or learned_answer
            if field_type == "checkbox" and field_options:
                learned_answer = _match_original_options(learned_answer, field_options, lookup_options) or learned_answer
            return AnswerResult(answer=learned_answer, tier=1)

    custom_answer = _handle_custom_question(normalized_label, field_type, lookup_options, job_context, candidate_data)
    if custom_answer is not None:
        log.debug("answered_custom_rule", label=field_label[:60])
        if field_type in ("dropdown", "radio", "select") and field_options:
            custom_answer = _match_original_option(custom_answer, field_options, lookup_options) or custom_answer
        if field_type == "checkbox" and field_options:
            custom_answer = _match_original_options(custom_answer, field_options, lookup_options) or custom_answer
        return AnswerResult(answer=custom_answer, tier=1)

    answer = tier1_lookup(normalized_label, field_type, candidate_data)
    if answer is not None:
        log.debug("answered_tier1", label=field_label[:60])
        if field_type in ("dropdown", "radio", "select") and field_options:
            answer = _match_original_option(answer, field_options, lookup_options) or answer
        if field_type == "checkbox" and field_options:
            answer = _match_original_options(answer, field_options, lookup_options) or answer
        return AnswerResult(answer=answer, tier=1)

    answer = tier2_semantic_match(lookup_label, encoder, corpus_embeddings, corpus)
    if answer is not None:
        log.debug("answered_tier2", label=field_label[:60])
        if field_type in ("dropdown", "radio", "select") and field_options:
            answer = _match_original_option(answer, field_options, lookup_options) or answer
        if field_type == "checkbox" and field_options:
            answer = _match_original_options(answer, field_options, lookup_options) or answer
        return AnswerResult(answer=answer, tier=2)

    # Section-aware routing: try to resolve from the appropriate source block
    # before falling back to full AI generation
    section = classify_question_section(field_label)
    if section and section not in ("eeoc",):  # EEOC already handled by tier1
        log.debug("section_aware_routing", label=field_label[:60], section=section)

    if any(indicator in normalized_label for indicator in GENERATIVE_FIELD_INDICATORS):
        log.info("answered_tier3_ai", label=field_label[:60])
        generation_context = dict(job_context)
        if detected_language not in ("en", "und"):
            generation_context["source_language"] = detected_language
        answer = await generate_answer_with_ai(lookup_label, field_type, generation_context, candidate_data, generator)
        if answer:
            # Validate and repair AI answer before accepting
            repaired = validate_and_repair_answer(answer, field_type, field_label, field_options)
            if repaired:
                answer = repaired
            if field_type in ("dropdown", "radio", "select") and field_options:
                answer = _match_original_option(answer, field_options, lookup_options) or answer
            if field_type == "checkbox" and field_options:
                answer = _match_original_options(answer, field_options, lookup_options) or answer
            if learned_answers_enabled:
                store_learned_answer(field_label, field_type, answer)
            return AnswerResult(answer=answer, tier=3)
        if learned_answers_enabled:
            store_pending_question(
                label=field_label,
                classification=field_type,
                job_id=str(job_context.get("id", "")),
                job_title=str(job_context.get("title", "")),
                company=str(job_context.get("company", "")),
            )
        return AnswerResult(answer=None, tier=None)

    log.info("no_answer_found", label=field_label[:60])
    if learned_answers_enabled:
        store_pending_question(
            label=field_label,
            classification=field_type,
            job_id=str(job_context.get("id", "")),
            job_title=str(job_context.get("title", "")),
            company=str(job_context.get("company", "")),
        )
    return AnswerResult(answer=None, tier=None)


class FieldAnswerer:
    def __init__(self, *, encoder, generator, candidate_data: dict[str, Any], corpus_embeddings, corpus):
        self.encoder = encoder
        self.generator = generator
        self.candidate_data = candidate_data
        self.corpus_embeddings = corpus_embeddings
        self.corpus = corpus

    async def answer(self, *, field_label: str, field_type: str, field_options: list[str] | None, job_context: dict) -> AnswerResult:
        return await get_answer_for_field_with_metadata(
            field_label=field_label,
            field_type=field_type,
            field_options=field_options or [],
            job_context=job_context,
            candidate_data=self.candidate_data,
            encoder=self.encoder,
            corpus_embeddings=self.corpus_embeddings,
            corpus=self.corpus,
            generator=self.generator,
        )


def load_candidate_data() -> dict[str, Any]:
    ground_truth = GroundTruthStore().read_if_exists()
    candidate_profile = CandidateProfileStore().read_if_exists()
    return build_candidate_data(ground_truth, candidate_profile)


def build_candidate_data(ground_truth: dict[str, Any], candidate_profile: dict[str, Any]) -> dict[str, Any]:
    identity = candidate_profile.get("identity", {}) if isinstance(candidate_profile, dict) else {}
    profile_education = candidate_profile.get("education", []) if isinstance(candidate_profile, dict) else []
    gt_personal = ground_truth.get("personal", {}) if isinstance(ground_truth, dict) else {}
    gt_education = ground_truth.get("education", []) if isinstance(ground_truth, dict) else []
    gt_experience = ground_truth.get("experience", []) if isinstance(ground_truth, dict) else []
    profile_work_auth = candidate_profile.get("work_authorization", {}) if isinstance(candidate_profile, dict) else {}
    profile_job_preferences = candidate_profile.get("job_preferences", {}) if isinstance(candidate_profile, dict) else {}
    compensation = candidate_profile.get("compensation_policy", {}) if isinstance(candidate_profile, dict) else {}
    disclosures = candidate_profile.get("application_defaults", {}).get("disclosures", {}) if isinstance(candidate_profile, dict) else {}

    first_name = identity.get("first_name") or (gt_personal.get("preferred_name") or str(gt_personal.get("full_name", "")).split(" ")[0])
    last_name = identity.get("last_name") or (str(gt_personal.get("full_name", "")).split(" ")[-1] if gt_personal.get("full_name") else "")
    full_name = gt_personal.get("full_name") or " ".join(part for part in [first_name, last_name] if part)
    phone = gt_personal.get("phone_e164") or _profile_phone(identity)
    education_latest = profile_education[0] if profile_education else (gt_education[0] if gt_education else {})
    current_experience = gt_experience[0] if gt_experience else {}
    requires_sponsorship = _requires_sponsorship(profile_work_auth)
    authorized_to_work = "Yes" if not requires_sponsorship else "No"
    candidate = {
        "personal": {
            "first_name": first_name,
            "last_name": last_name,
            "full_name": full_name,
            "email": gt_personal.get("email") or identity.get("email", {}).get("primary", ""),
            "phone": phone,
            "linkedin_url": gt_personal.get("linkedin_url") or identity.get("links", {}).get("linkedin", ""),
            "github_url": gt_personal.get("github_url") or identity.get("links", {}).get("github", ""),
            "portfolio_url": gt_personal.get("portfolio_url") or identity.get("links", {}).get("portfolio", ""),
            "address": gt_personal.get("address", ""),
            "city": gt_personal.get("location_city") or identity.get("location", {}).get("city", ""),
            "state": gt_personal.get("location_state", ""),
            "zip_code": gt_personal.get("zip_code", ""),
            "country": gt_personal.get("location_country") or identity.get("location", {}).get("country", ""),
        },
        "work_authorization": {
            "authorized_to_work": authorized_to_work,
            "requires_sponsorship": "Yes" if requires_sponsorship else "No",
            "visa_status": profile_work_auth.get("notes", [""])[0] if profile_work_auth.get("notes") else "",
            "citizenship": gt_personal.get("citizenship", ""),
        },
        "education": {
            "latest": {
                "institution": education_latest.get("institution", ""),
                "degree": education_latest.get("degree", ""),
                "major": education_latest.get("major") or education_latest.get("field", ""),
                "minor": education_latest.get("minor", ""),
                "gpa": _education_gpa(education_latest),
                "graduation_date": education_latest.get("graduation_date") or education_latest.get("end_month_year", ""),
                "start_date": education_latest.get("start_date") or education_latest.get("start_month_year", ""),
            }
        },
        "experience": {
            "current": {
                "company": current_experience.get("company", ""),
                "title": current_experience.get("title", ""),
            },
            "all": gt_experience,
        },
        "preferences": {
            "notice_period": profile_job_preferences.get("notice_period") or ground_truth.get("preferences", {}).get("notice_period_days", "Immediately"),
            "expected_salary": _expected_salary(compensation, ground_truth.get("preferences", {})),
            "current_salary": "",
            "how_did_you_hear": "Company website",
            "remote_preference": ",".join(profile_job_preferences.get("locations", {}).get("modes", [])),
            "locations_willing_to_work": profile_job_preferences.get("locations", {}).get("scope", ""),
        },
        "eeoc": {
            "gender": disclosures.get("gender", "Prefer not to say"),
            "race": disclosures.get("race_ethnicity", "Prefer not to say"),
            "veteran_status": disclosures.get("veteran_status", "I am not a veteran"),
            "disability_status": disclosures.get("disability", "I do not have a disability"),
            "pronouns": candidate_profile.get("application_defaults", {}).get("pronouns", "Prefer not to say"),
        },
        "projects": ground_truth.get("projects", []),
    }
    return {"ground_truth": ground_truth, "candidate_profile": candidate_profile, "candidate": candidate}


def _resolve_callable_value(name: str, candidate_data: dict[str, Any]) -> str:
    if name == _callable_name("years_of_experience"):
        years = _compute_years_of_experience(candidate_data.get("ground_truth", {}).get("experience", []))
        return f"{years:.1f}".rstrip("0").rstrip(".")
    return ""


def _compute_years_of_experience(experience_items: list[dict[str, Any]]) -> float:
    total_months = 0
    for item in experience_items or []:
        start = item.get("start_month_year", "")
        end = item.get("end_month_year", "")
        start_date = _parse_month_year(start)
        end_date = _parse_month_year(end) if str(end).lower() != "present" else date.today()
        if start_date and end_date and end_date >= start_date:
            total_months += max(1, (end_date.year - start_date.year) * 12 + end_date.month - start_date.month + 1)
    return total_months / 12.0


def _parse_month_year(value: str) -> date | None:
    text = str(value or "")
    for fmt in ("%Y-%m", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            return date(parsed.year, parsed.month, 1)
        except ValueError:
            continue
    return None


def _profile_phone(identity: dict[str, Any]) -> str:
    phone = identity.get("phone", {}) if isinstance(identity, dict) else {}
    country_code = phone.get("country_code", "")
    number = phone.get("number", "")
    return f"{country_code}{number}" if country_code or number else ""


def _education_gpa(item: dict[str, Any]) -> str:
    gpa = item.get("gpa")
    if isinstance(gpa, dict):
        value = gpa.get("value")
        scale = gpa.get("scale")
        if value is not None and scale is not None:
            return f"{value}/{scale}"
        if value is not None:
            return str(value)
    return str(gpa or "")


def _requires_sponsorship(work_auth: dict[str, Any]) -> bool:
    """Return True if sponsorship is needed for non-authorized-country applications.

    When the profile explicitly lists authorized_countries (e.g. only India), we
    use the default_outside_* policy as the global baseline, since most applications
    will be to non-authorized countries.  When no authorized_countries are listed
    we fall back to the original per-country check so that existing profiles
    (e.g. US citizens with US policy = no sponsorship) continue to work.
    """
    if not isinstance(work_auth, dict):
        return False
    sponsorship = work_auth.get("sponsorship", {})
    if not isinstance(sponsorship, dict) or not sponsorship:
        return False
    authorized_countries = work_auth.get("authorized_countries")
    # When an explicit authorized-country list is present, the candidate is
    # restricted to those countries and needs the default policy for everywhere else.
    if isinstance(authorized_countries, list) and authorized_countries:
        default_policy = (
            sponsorship.get("default_outside_authorized_countries")
            or sponsorship.get("default_outside_india")
            or sponsorship.get("default")
        )
        if isinstance(default_policy, dict):
            return bool(default_policy.get("now_requires", False))
    # No authorized_countries key: fall back to original behaviour — if any
    # explicitly-named country says no sponsorship required, trust that.
    for country_key, policy in sponsorship.items():
        if str(country_key).startswith("default_") or not isinstance(policy, dict):
            continue
        if policy.get("now_requires") is False:
            return False
    default_policy = (
        sponsorship.get("default_outside_authorized_countries")
        or sponsorship.get("default_outside_india")
        or sponsorship.get("default")
        or {}
    )
    return bool(default_policy.get("now_requires", False)) if isinstance(default_policy, dict) else False


def _expected_salary(compensation: dict[str, Any], preferences: dict[str, Any]) -> str:
    if not isinstance(compensation, dict):
        compensation = {}
    if not isinstance(preferences, dict):
        preferences = {}
    target = compensation.get("target_salary")
    if isinstance(target, dict):
        amount = target.get("amount")
        currency = target.get("currency")
        if amount is not None:
            return " ".join(str(part) for part in (amount, currency) if part)
    for key in ("expected_salary", "minimum_salary", "target_amount", "rounded_answer"):
        value = compensation.get(key)
        if value not in (None, ""):
            return str(value)
    for value in compensation.values():
        if not isinstance(value, dict):
            continue
        for key in ("expected_salary", "minimum_salary", "target_amount", "rounded_answer"):
            nested = value.get(key)
            if nested not in (None, ""):
                currency = value.get("currency")
                return " ".join(str(part) for part in (nested, currency) if part)
    usd = preferences.get("salary_min_usd_annual")
    if usd not in (None, ""):
        return f"{usd} USD"
    inr = preferences.get("salary_expected_inr_lpa")
    if inr not in (None, ""):
        return f"{inr} LPA"
    return ""


def _handle_custom_question(normalized_label: str, field_type: str, field_options: list[str], job_context: dict, candidate_data: dict[str, Any]) -> str | None:
    preferences = candidate_data.get("candidate", {}).get("preferences", {})
    personal = candidate_data.get("candidate", {}).get("personal", {})
    candidate_country = normalize_label(str(personal.get("country", "")))
    candidate_city = normalize_label(str(personal.get("city", "")))

    if field_type in {"dropdown", "radio", "select"} and field_options:
        if "closest to you" in normalized_label or ("which location" in normalized_label and "closest" in normalized_label):
            preferred = candidate_city or candidate_country or str(preferences.get("locations_willing_to_work", ""))
            match_option = find_best_option_match(preferred, field_options)
            if match_option:
                return match_option
        if "how did you hear about this role" in normalized_label or "how did you hear about this job" in normalized_label:
            configured = str(preferences.get("how_did_you_hear", ""))
            match_option = find_best_option_match(configured, field_options)
            if match_option:
                return match_option
            if any("linkedin" in str(option).lower() for option in field_options):
                return find_best_option_match("LinkedIn", field_options)

    if ("based in" in normalized_label or "currently based in" in normalized_label) and ("yes" in [opt.lower() for opt in field_options] or field_type in {"radio", "select", "dropdown"}):
        target_location = re.search(r"based in ([a-zA-Z .-]+)", normalized_label)
        if target_location:
            place = normalize_label(target_location.group(1))
            return "Yes" if place and (place in candidate_country or place in candidate_city) else "No"

    if ("ready to work full time" in normalized_label or "available to work full time" in normalized_label or "work full time" in normalized_label) and ("yes" in [opt.lower() for opt in field_options] or field_type in {"radio", "select", "dropdown"}):
        return "Yes"

    if ("would this arrangement work for you" in normalized_label or "would this work arrangement work for you" in normalized_label or "would this arrangement be okay" in normalized_label):
        remote_preference = str(preferences.get("remote_preference", "")).lower()
        willing_scope = str(preferences.get("locations_willing_to_work", "")).lower()
        if "office" in normalized_label or "onsite" in normalized_label or "on site" in normalized_label:
            return "Yes" if ("onsite" in remote_preference or "hybrid" in remote_preference or willing_scope in {"worldwide", "global", "anywhere"}) else "No"

    if any(term in normalized_label for term in ("on site", "onsite", "remote", "hybrid")) and ("available" in normalized_label or "work" in normalized_label):
        remote_preference = str(preferences.get("remote_preference", "")).lower()
        if "remote" in normalized_label:
            return "Yes" if "remote" in remote_preference else "No"
        if "hybrid" in normalized_label:
            return "Yes" if "hybrid" in remote_preference else "No"
        if "on site" in normalized_label or "onsite" in normalized_label:
            return "Yes" if "onsite" in remote_preference else "No"

    match = re.search(r"(\d+)\+?\s+years?.*?\b([a-zA-Z0-9.+#/ -]+)\b", normalized_label)
    if match and ("do you have" in normalized_label or "experience" in normalized_label):
        required_years = float(match.group(1))
        skill = _normalize_skill_phrase(match.group(2))
        available_years = _estimate_skill_years(skill, candidate_data)
        return "Yes" if available_years >= required_years else "No"

    if field_type in {"dropdown", "radio", "select"} and field_options:
        if "work arrangement" in normalized_label or "preferred work" in normalized_label:
            remote_preference = str(preferences.get("remote_preference", ""))
            match_option = find_best_option_match(remote_preference, field_options)
            if match_option:
                return match_option
    if "experience with ai tools" in normalized_label or "experience with ai" in normalized_label:
        return _summarize_ai_tool_experience(candidate_data)
    return None


def _summarize_ai_tool_experience(candidate_data: dict[str, Any]) -> str | None:
    skills = []
    for project in candidate_data.get("ground_truth", {}).get("projects", []):
        text = " ".join(
            [
                str(project.get("title", "")),
                str(project.get("summary_1line", "")),
                " ".join(project.get("tech_stack", [])),
                " ".join(project.get("domains", [])),
            ]
        ).lower()
        if "ai" in text or "agent" in text or "workflow" in text:
            skills.append(project)
    if not skills:
        return None
    names = ", ".join(str(project.get("title", "Project")).strip() for project in skills[:2])
    return (
        f"I've used AI-assisted workflows in projects like {names}, mainly for iteration, "
        "automation, and developer productivity while building full-stack and backend systems."
    )


def _estimate_skill_years(skill: str, candidate_data: dict[str, Any]) -> float:
    skill_lower = normalize_label(skill)
    months = 0
    for experience in candidate_data.get("ground_truth", {}).get("experience", []):
        haystack = " ".join(
            [
                experience.get("title", ""),
                experience.get("summary_1line", ""),
                " ".join(experience.get("tech_stack", [])),
                " ".join(experience.get("domains", [])),
            ]
        ).lower()
        if skill_lower in normalize_label(haystack):
            start_date = _parse_month_year(experience.get("start_month_year", ""))
            end_date = _parse_month_year(experience.get("end_month_year", "")) or date.today()
            if start_date and end_date >= start_date:
                months += max(1, (end_date.year - start_date.year) * 12 + end_date.month - start_date.month + 1)
    for project in candidate_data.get("ground_truth", {}).get("projects", []):
        haystack = " ".join(
            [
                project.get("title", ""),
                project.get("summary_1line", ""),
                " ".join(project.get("tech_stack", [])),
                " ".join(project.get("domains", [])),
            ]
        ).lower()
        if skill_lower in normalize_label(haystack):
            months += 6
    return months / 12.0


def _normalize_skill_phrase(skill: str) -> str:
    normalized = normalize_label(skill)
    normalized = re.sub(r"^(in|of|with|using|for)\s+", "", normalized).strip()
    normalized = re.sub(r"\s+(experience|skills?)$", "", normalized).strip()
    return normalized


def _pattern_matches_label(pattern: str, normalized_label: str) -> bool:
    normalized_pattern = normalize_label(pattern)
    if " " in normalized_pattern or "_" in pattern:
        return normalized_pattern in normalized_label
    if normalized_pattern not in {"name", "city", "state", "country", "address", "degree", "major", "minor", "gender", "race"}:
        return normalized_pattern in normalized_label
    return re.search(rf"(?<![a-z0-9]){re.escape(normalized_pattern)}(?![a-z0-9])", normalized_label) is not None


def _load_projects_library_safe() -> list[dict[str, Any]]:
    path = _user_file_path("projects_library.json")
    if not path.exists():
        log.warning("projects_library_not_built", path=str(path))
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log.warning("projects_library_invalid_json", path=str(path), error=str(exc))
        return []
    if not isinstance(payload, dict):
        log.warning("projects_library_invalid_shape", path=str(path))
        return []
    projects = payload.get("projects", [])
    if not isinstance(projects, list):
        log.warning("projects_library_projects_not_list", path=str(path))
        return []
    return [project for project in projects if isinstance(project, dict)]


def _user_file_path(name: str) -> Any:
    canonical = GROUND_TRUTH_DIR / name
    legacy = DATA_DIR / name
    if canonical.exists() or not legacy.exists():
        return canonical
    return legacy


def _select_relevant_projects(candidate_data: dict[str, Any], job_description: str, limit: int = 3) -> list[dict[str, Any]]:
    jd = normalize_label(job_description)
    library_projects = _load_projects_library_safe()
    ground_truth_projects = candidate_data.get("ground_truth", {}).get("projects", [])
    all_projects = library_projects or [project for project in ground_truth_projects if isinstance(project, dict)]
    scored: list[tuple[int, dict[str, Any]]] = []
    for project in all_projects:
        haystack = normalize_label(
            " ".join(
                [
                    str(project.get("title", "")),
                    str(project.get("summary_1line", "")),
                    " ".join(project.get("tech_stack", []) or []),
                    " ".join(project.get("domains", []) or []),
                    " ".join(project.get("keywords", []) or []),
                ]
            )
        )
        score = 0
        if jd:
            project_words = set(haystack.split())
            jd_words = set(jd.split())
            score = len(project_words & jd_words)
        scored.append((score, project))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [project for _score, project in scored[:limit] if project]


def _format_projects_for_prompt(projects: list[dict[str, Any]]) -> str:
    if not projects:
        return "No ranked project library entries were available."
    lines = []
    for project in projects:
        lines.append(
            "- {title}: {summary} | tech={tech}".format(
                title=project.get("title", ""),
                summary=project.get("summary_1line", ""),
                tech=", ".join(project.get("tech_stack", []) or []),
            )
        )
    return "\n".join(lines)
