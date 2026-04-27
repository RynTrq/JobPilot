from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from backend.form.answerer import FormField
from backend.scraping.adapters.base import JobListing, SubmitResult
from backend.scraping.adapters.browser_form import BrowserFormAdapter
from backend.scraping.adapters.generic import GenericAdapter, _finalize_description
from backend.scraping.browser import goto_with_pacing
from backend.scraping.browser import human_delay
from backend.scraping.job_page import extract_text_from_page


SELECTORS = {
    "job_links": (
        "div.opening a[href]",
        "a[href*='/jobs/']",
        "a[href*='gh_jid=']",
        "a[href*='job_id=']",
        "a[href*='job-boards.greenhouse.io']",
    ),
    "direct_job_form": "#application_form, form[action*='job_applications'], form[action*='greenhouse']",
    "title": "h1, .app-title, .job__title, [class*='title']",
    "company": ".company-name, [class*='company']",
    "apply_buttons": (
        "#apply_button",
        "a[href*='#app']",
        "a[href*='application']",
        "button:has-text('Apply')",
        "a:has-text('Apply')",
    ),
    "form_ready": "form, input, textarea, select",
    "submit_buttons": "button[type='submit'], input[type='submit'], button:has-text('Submit'), button:has-text('Apply')",
    "confirmation": "text=/thank|submitted|received|confirmation/i",
}

class GreenhouseAdapter(GenericAdapter):
    async def list_jobs(self, page, career_url: str) -> list[JobListing]:
        await goto_with_pacing(page, career_url, timeout=30000)
        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")
        if _is_direct_greenhouse_job_url(career_url) or soup.select_one(SELECTORS["direct_job_form"]):
            title = _first_text(soup, SELECTORS["title"].split(", "))
            company = _first_text(soup, SELECTORS["company"].split(", "))
            return [
                JobListing(
                    url=career_url,
                    title_preview=title,
                    ext_id=external_job_id(career_url),
                    company=company,
                )
            ]
        listings = _parse_greenhouse_listings(html, career_url)
        if not listings:
            return await super().list_jobs(page, career_url)
        return listings

    async def extract_description(self, page, job_url: str) -> str:
        await goto_with_pacing(page, job_url, timeout=30000)
        description = await extract_text_from_page(page)
        return _finalize_description(description, job_url=job_url, adapter=self.__class__.__name__)

    async def open_application(self, page, job_url: str) -> None:
        await goto_with_pacing(page, job_url, timeout=30000)
        browser_form = BrowserFormAdapter()
        if await browser_form._form_context(page):
            return
        for selector in SELECTORS["apply_buttons"]:
            try:
                element = page.locator(selector).first
                if await element.count() and await element.is_visible():
                    await human_delay()
                    await element.click()
                    if not browser_form._timing_disabled():
                        await page.wait_for_timeout(250)
                    break
            except Exception:
                continue
        try:
            await page.wait_for_selector(SELECTORS["form_ready"], timeout=3000)
        except Exception:
            pass
        if await browser_form._form_context(page):
            return
        raise NotImplementedError("No supported Greenhouse application form was found on this site.")

    async def enumerate_fields(self, page) -> list[FormField]:
        return await BrowserFormAdapter().enumerate_fields(page)

    async def fill_field(self, page, field: FormField, value) -> None:
        await BrowserFormAdapter().fill_field(page, field, value)

    async def attach_resume(self, page, pdf_path: str | Path) -> None:
        await BrowserFormAdapter().attach_resume(page, pdf_path)

    async def attach_cover_letter(self, page, pdf_path: str | Path) -> None:
        await BrowserFormAdapter().attach_cover_letter(page, pdf_path)

    async def submit(self, page) -> SubmitResult:
        return await BrowserFormAdapter().submit(page)


def _parse_greenhouse_listings(html: str, career_url: str) -> list[JobListing]:
    soup = BeautifulSoup(html, "html.parser")
    listings: list[JobListing] = []
    for selector in SELECTORS["job_links"]:
        for element in soup.select(selector):
            href = element.get("href")
            if not href:
                continue
            url = urljoin(career_url, href)
            if not _is_greenhouse_job_url(url):
                continue
            title = " ".join(element.get_text(" ").split()) or None
            if not title or _is_noise_title(title):
                continue
            parent_text = " ".join((element.parent.get_text(" ") if element.parent else "").split())
            location = parent_text.replace(title, "").strip()[:120] or None
            listings.append(
                JobListing(
                    url=url,
                    title_preview=title[:180],
                    location_preview=location,
                    ext_id=external_job_id(url),
                    company=_company_from_url(url),
                )
            )
    deduped: dict[str, JobListing] = {}
    for listing in listings:
        deduped[listing.url] = listing
    return list(deduped.values())


def external_job_id(url: str) -> str | None:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if "gh_jid" in query:
        return query["gh_jid"][0]
    parts = [part for part in parsed.path.split("/") if part]
    return parts[-1] if parts else None


def _is_direct_greenhouse_job_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    return "job_app" in path or "/jobs/" in path or "gh_jid=" in parsed.query.lower() or "job_id=" in parsed.query.lower()


def _is_greenhouse_job_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    path = parsed.path.lower()
    query = parsed.query.lower()
    if "greenhouse.io" not in host:
        return False
    return "/jobs/" in path or "gh_jid=" in query or "job_id=" in query


def _company_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if "job-boards.greenhouse.io" in (parsed.hostname or "") and parts:
        return parts[0]
    if "boards.greenhouse.io" in (parsed.hostname or "") and parts:
        return parts[0]
    return None


def _is_noise_title(title: str) -> bool:
    compact = " ".join(title.lower().split())
    if compact in {"careers", "jobs", "open positions", "apply", "view job"}:
        return True
    return compact.startswith(("previous", "next", "back to"))


def _first_text(soup: BeautifulSoup, selectors: list[str]) -> str | None:
    for selector in selectors:
        element = soup.select_one(selector)
        if element:
            text = " ".join(element.get_text(" ").split())
            if text:
                return text[:180]
    return None
