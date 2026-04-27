from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from backend.scraping.adapters.base import JobListing
from backend.scraping.adapters.browser_form import BrowserFormAdapter
from backend.scraping.adapters.generic import _finalize_description
from backend.scraping.browser import goto_with_pacing
from backend.scraping.job_page import extract_text_from_page


WORKDAY_DESCRIPTION_SELECTORS = (
    "[data-automation-id='jobPostingDescription']",
    "[data-automation-id='jobPostingDescriptionText']",
    "[data-automation-id='jobPostingDescriptionContainer']",
    "[data-automation-id='jobDetails']",
    "[data-automation-id='jobPostingHeader']",
)


class WorkdayAdapter(BrowserFormAdapter):
    apply_selectors = (
        "button:has-text('Apply')",
        "a:has-text('Apply')",
        "button[data-automation-id*='apply']",
        "[data-automation-id='adventureButton']",
        *BrowserFormAdapter.apply_selectors,
    )

    async def list_jobs(self, page, career_url: str) -> list[JobListing]:
        await self._goto(page, career_url)
        soup = BeautifulSoup(await page.content(), "html.parser")
        if "/job/" in career_url:
            return [JobListing(url=career_url, title_preview=_first_text(soup, ["h1", "[data-automation-id='jobPostingHeader']"]))]
        listings = []
        for node in soup.select("a[href*='/job/'], a[data-automation-id*='jobTitle']"):
            href = node.get("href")
            if not href:
                continue
            title = " ".join(node.get_text(" ").split()) or None
            listings.append(JobListing(url=urljoin(career_url, href), title_preview=title))
        return _dedupe(listings) or await super().list_jobs(page, career_url)

    async def extract_description(self, page, job_url: str) -> str:
        await goto_with_pacing(page, job_url, timeout=30000)
        description = await extract_text_from_page(page, ready_selectors=WORKDAY_DESCRIPTION_SELECTORS)
        return _finalize_description(description, job_url=job_url, adapter=self.__class__.__name__)

    async def open_application(self, page, job_url: str) -> None:
        await super().open_application(page, job_url)
        if await self._form_context(page, require_application_markers=True):
            return
        try:
            body_text = " ".join(((await page.locator("body").inner_text()) or "").lower().split())
        except Exception:
            body_text = ""
        if any(marker in body_text for marker in ("create account", "sign in", "sign-in", "log in", "login")):
            raise NotImplementedError("Workday requires account creation or sign-in before the application form is exposed.")


def _first_text(soup: BeautifulSoup, selectors: list[str]) -> str | None:
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            text = " ".join(node.get_text(" ").split())
            if text:
                return text
    return None


def _dedupe(items: list[JobListing]) -> list[JobListing]:
    out = {}
    for item in items:
        out[item.url] = item
    return list(out.values())
