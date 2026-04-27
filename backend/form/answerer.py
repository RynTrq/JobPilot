from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.form.field_matcher import FieldMatcher, resolve_path
from backend.storage.ground_truth import GroundTruthStore


@dataclass
class FormField:
    label_text: str
    field_type: str
    selector: str | None = None
    required: bool = False
    options: list[str] | None = None
    option_selectors: dict[str, str] | None = None
    char_limit: int | None = None
    name: str | None = None
    accept: str | None = None
    tag: str | None = None
    role: str | None = None
    element_id: str | None = None
    placeholder: str | None = None
    aria_label: str | None = None
    visible: bool = True
    enabled: bool = True
    bounding_box: dict[str, float] | None = None


class Answerer:
    def __init__(self, encoder, generator, notifier, store: GroundTruthStore | None = None):
        self.encoder = encoder
        self.generator = generator
        self.notifier = notifier
        self.store = store or GroundTruthStore()
        self._load_matcher()

    def _load_matcher(self) -> None:
        self.ground_truth = self.store.read()
        self.matcher = FieldMatcher(self.encoder, self.ground_truth)

    async def answer(self, field: FormField, job_description: str) -> Any:
        direct = self._direct_answer(field.label_text)
        if direct is not None:
            return direct
        if field.field_type in {"text", "email", "tel", "url", "date"}:
            match = self.matcher.match(field.label_text)
            if match.score >= 0.75:
                return resolve_path(self.ground_truth, match.path)
            if match.score < 0.55:
                return await self._alarm(field)
        if field.field_type == "textarea":
            return await self.generator.complete(
                "Answer job application questions using only candidate ground-truth facts.",
                f"QUESTION: {field.label_text}\nJOB CONTEXT: {job_description[:1200]}\nGROUND TRUTH: {self.ground_truth}",
                max_tokens=220,
                temperature=0.2,
            )
        if field.field_type in {"select", "radio"}:
            value = self._match_option(field)
            if value is not None:
                return value
            return await self._alarm(field)
        if field.field_type == "checkbox":
            return self._checkbox_decision(field.label_text)
        return await self._alarm(field)

    async def _alarm(self, field: FormField) -> str:
        answer = await self.notifier.trigger(field.label_text, field.field_type, field.options)
        self.store.fill_custom(field.label_text, answer)
        self._load_matcher()
        return answer

    def _match_option(self, field: FormField) -> str | None:
        if not field.options:
            return None
        label = field.label_text.lower()
        options = {option.lower(): option for option in field.options}
        if "sponsor" in label or "authorized" in label:
            if "no" in options:
                return options["no"]
            if "yes" in options and "india" in label:
                return options["yes"]
        return None

    def _checkbox_decision(self, label: str) -> bool:
        text = label.lower()
        if "terms" in text or "privacy" in text or "background check" in text:
            return True
        if "marketing" in text or "newsletter" in text:
            return False
        return False

    def _direct_answer(self, label: str) -> Any:
        text = " ".join(label.lower().replace("*", " ").replace(":", " ").split())
        personal = self.ground_truth.get("personal", {})
        preferences = self.ground_truth.get("preferences", {})
        full_name = personal.get("full_name", "")
        parts = full_name.split()
        if "first name" in text or text == "first":
            return personal.get("preferred_name") or (parts[0] if parts else None)
        if "last name" in text or text == "last":
            return parts[-1] if parts else None
        if "full name" in text or "legal name" in text or text in {"name", "your name", "candidate name", "applicant name"}:
            return full_name
        if "email" in text:
            return personal.get("email")
        if "phone" in text or "mobile" in text or "telephone" in text:
            return personal.get("phone_e164")
        if "linkedin" in text:
            return personal.get("linkedin_url")
        if "github" in text:
            return personal.get("github_url")
        if "portfolio" in text or "website" in text:
            return personal.get("portfolio_url")
        if "city" in text:
            return personal.get("location_city")
        if "country" in text:
            return personal.get("location_country")
        if ("start" in text or "available" in text) and "programming" not in text and "language" not in text:
            return preferences.get("earliest_start_date")
        if "relocat" in text:
            return "Yes" if preferences.get("willing_to_relocate") else "No"
        if "sponsor" in text or "authorized" in text or "work authorization" in text:
            return personal.get("work_auth_us") or "No - would require sponsorship outside India"
        return None
