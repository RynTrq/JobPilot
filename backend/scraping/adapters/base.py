from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class JobListing:
    url: str
    title_preview: str | None = None
    location_preview: str | None = None
    ext_id: str | None = None
    company: str | None = None


@dataclass
class SubmitResult:
    ok: bool
    confirmation_text: str | None = None
    error: str | None = None
    screenshot_path: str | None = None


class AdapterBase(ABC):
    @abstractmethod
    async def list_jobs(self, page, career_url: str) -> list[JobListing]: ...

    @abstractmethod
    async def extract_description(self, page, job_url: str) -> str: ...

    async def open_application(self, page, job_url: str) -> None:
        raise NotImplementedError

    async def enumerate_fields(self, page) -> list:
        raise NotImplementedError

    async def fill_field(self, page, field, value) -> None:
        raise NotImplementedError

    async def attach_resume(self, page, pdf_path: str | Path) -> None:
        raise NotImplementedError

    async def attach_cover_letter(self, page, pdf_path: str | Path) -> None:
        raise NotImplementedError

    async def submit(self, page) -> SubmitResult:
        raise NotImplementedError
