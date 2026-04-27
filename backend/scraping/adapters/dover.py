from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup
import structlog

from backend import config
from backend.form.answerer import FormField
from backend.scraping.adapters.base import AdapterBase, JobListing, SubmitResult

log = structlog.get_logger()


class DoverAdapter(AdapterBase):
    api_base = "https://app.dover.com/api/v1"

    def __init__(self) -> None:
        self.job: dict | None = None
        self.values: dict[str, object] = {}
        self.resume_path: Path | None = None
        self.cover_letter_path: Path | None = None
        self.referrer_source: str | None = None

    async def list_jobs(self, page, career_url: str) -> list[JobListing]:
        job = await self._fetch_job(career_url)
        return [
            JobListing(
                url=career_url,
                title_preview=job.get("title"),
                location_preview=_location_text(job),
                ext_id=job.get("id"),
                company=job.get("client_name"),
            )
        ]

    async def extract_description(self, page, job_url: str) -> str:
        job = await self._fetch_job(job_url)
        soup = BeautifulSoup(job.get("user_provided_description") or "", "html.parser")
        body = " ".join(soup.get_text(" ").split())
        parts = [
            f"{job.get('title') or ''} at {job.get('client_name') or ''}".strip(),
            _location_text(job) or "",
            body,
        ]
        description = "\n\n".join(part for part in parts if part).strip()
        log.info("job_description_extracted", adapter=self.__class__.__name__, job_url=job_url, characters=len(description))
        if len(description) < 200:
            log.error("job_description_too_short", adapter=self.__class__.__name__, job_url=job_url, characters=len(description))
            raise RuntimeError(f"Extracted job description is too short ({len(description)} chars) for {job_url}")
        return description

    async def open_application(self, page, job_url: str) -> None:
        await self._fetch_job(job_url)

    async def enumerate_fields(self, page) -> list[FormField]:
        job = self._require_job()
        fields = [
            FormField("First Name", "text", name="firstName", required=True),
            FormField("Last Name", "text", name="lastName", required=True),
            FormField("Email", "email", name="email", required=True),
        ]
        for question in job.get("application_questions") or []:
            if question.get("hidden"):
                continue
            question_type = question.get("question_type")
            input_type = question.get("input_type")
            if question_type == "RESUME":
                field_type = "file"
            elif question_type == "PHONE_NUMBER":
                field_type = "tel"
            elif question_type == "LINKEDIN_URL":
                field_type = "url"
            elif input_type == "LONG_ANSWER":
                field_type = "textarea"
            elif input_type == "MULTIPLE_CHOICE":
                field_type = "radio"
            else:
                field_type = "text"
            fields.append(
                FormField(
                    label_text=question.get("question") or question_type or "Question",
                    field_type=field_type,
                    required=bool(question.get("required")),
                    options=question.get("multiple_choice_options"),
                    name=question.get("id"),
                )
            )
        return fields

    async def fill_field(self, page, field: FormField, value) -> None:
        if not field.name or field.field_type == "file":
            return
        self.values[field.name] = value

    async def attach_resume(self, page, pdf_path: str | Path) -> None:
        self.resume_path = Path(pdf_path)

    async def attach_cover_letter(self, page, pdf_path: str | Path) -> None:
        self.cover_letter_path = Path(pdf_path)

    async def submit(self, page) -> SubmitResult:
        if config.DRY_RUN:
            missing = self._missing_required()
            if missing:
                return SubmitResult(ok=False, error=f"dry run: missing required fields: {', '.join(missing)}")
            return SubmitResult(ok=False, error="dry run: final submit skipped")

        job = self._require_job()
        custom_answers = []
        for question in job.get("application_questions") or []:
            if question.get("question_type") != "CUSTOM" or question.get("hidden"):
                continue
            custom_answers.append(
                {
                    "question": question.get("question"),
                    "answer": self.values.get(question.get("id"), ""),
                    "id": question.get("id"),
                }
            )

        data = {
            "job_id": job["id"],
            "first_name": str(self.values.get("firstName", "")),
            "last_name": str(self.values.get("lastName", "")),
            "email": str(self.values.get("email", "")),
            "opted_in_to_talent_network": "null",
            "application_questions": json.dumps(custom_answers),
        }
        if self.referrer_source:
            data["referrer_source"] = self.referrer_source
        if self.values.get("d2c66806-01be-4884-9e95-1d2fac5a2f50"):
            data["linkedin_url"] = str(self.values["d2c66806-01be-4884-9e95-1d2fac5a2f50"])
        if self.values.get("2dc89800-4499-4d50-beaa-dad54900b4bf"):
            data["phone_number"] = str(self.values["2dc89800-4499-4d50-beaa-dad54900b4bf"])

        files = {}
        if self.resume_path:
            files["resume"] = (self.resume_path.name, self.resume_path.read_bytes(), "application/pdf")
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(
                f"{self.api_base}/inbound/application-portal-inbound-application",
                data=data,
                files=files or None,
            )
        if response.is_success:
            return SubmitResult(ok=True, confirmation_text=response.text[:1000])
        return SubmitResult(ok=False, error=f"Dover submit failed: HTTP {response.status_code} {response.text[:500]}")

    async def _fetch_job(self, url: str) -> dict:
        job_id = _job_id(url)
        self.referrer_source = _referrer_source(url)
        async with httpx.AsyncClient(timeout=30, headers={"Accept": "application/json"}) as client:
            response = await client.get(f"{self.api_base}/inbound/application-portal-job/{job_id}")
            response.raise_for_status()
        self.job = response.json()
        return self.job

    def _require_job(self) -> dict:
        if self.job is None:
            raise RuntimeError("Dover job has not been loaded")
        return self.job

    def _missing_required(self) -> list[str]:
        missing = []
        for field in self._required_field_names():
            if field == "resume":
                if not self.resume_path:
                    missing.append("Resume upload")
            elif not self.values.get(field):
                missing.append(field)
        return missing

    def _required_field_names(self) -> list[str]:
        job = self._require_job()
        names = ["firstName", "lastName", "email"]
        for question in job.get("application_questions") or []:
            if question.get("hidden") or not question.get("required"):
                continue
            if question.get("question_type") == "RESUME":
                names.append("resume")
            elif question.get("question_type") in {"LINKEDIN_URL", "PHONE_NUMBER"}:
                names.append(question.get("id"))
            elif question.get("question_type") == "CUSTOM":
                names.append(question.get("id"))
        return [name for name in names if name]


def _job_id(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) < 3 or parts[0] != "apply":
        raise ValueError(f"not a Dover application URL: {url}")
    return parts[2]


def _referrer_source(url: str) -> str | None:
    query = parse_qs(urlparse(url).query)
    values = query.get("rs") or query.get("referrerSource")
    return values[0] if values else None


def _location_text(job: dict) -> str | None:
    locations = job.get("locations") or []
    names = [location.get("name") for location in locations if location.get("name")]
    return ", ".join(names) or None
