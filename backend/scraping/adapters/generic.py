from __future__ import annotations

from pathlib import Path

import structlog

from backend.scraping.adapters.base import AdapterBase
from backend.scraping.adapters.base import SubmitResult
from backend.scraping.browser import goto_with_pacing
from backend.scraping.job_list import discover_job_links
from backend.scraping.job_list import looks_like_direct_job_url
from backend.scraping.job_list import title_from_job_page
from backend.scraping.job_page import extract_text_from_page

log = structlog.get_logger()

LOAD_MORE_SELECTORS = (
    "button:has-text('Load more')",
    "a:has-text('Load more')",
    "button:has-text('Show more')",
    "a:has-text('Show more')",
    "button:has-text('More jobs')",
    "a:has-text('More jobs')",
    "button:has-text('View more')",
    "a:has-text('View more')",
)
MAX_LOAD_MORE_CLICKS = 30


class GenericAdapter(AdapterBase):
    async def list_jobs(self, page, career_url: str):
        await goto_with_pacing(page, career_url, timeout=30000)
        html = await page.content()
        if looks_like_direct_job_url(career_url):
            from backend.scraping.adapters.base import JobListing

            return [JobListing(url=career_url, title_preview=title_from_job_page(html, career_url))]
        await _exhaust_load_more(page, career_url)
        html = await page.content()
        listings = discover_job_links(html, career_url)
        if listings:
            return listings
        try:
            await page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            await page.wait_for_timeout(1000)
        await _exhaust_load_more(page, career_url)
        return discover_job_links(await page.content(), career_url)

    async def extract_description(self, page, job_url: str) -> str:
        await goto_with_pacing(page, job_url, timeout=30000)
        description = await extract_text_from_page(page)
        return _finalize_description(description, job_url=job_url, adapter=self.__class__.__name__)

    async def open_application(self, page, job_url: str) -> None:
        await self._browser_form().open_application(page, job_url)

    async def enumerate_fields(self, page) -> list:
        return await self._browser_form().enumerate_fields(page)

    async def fill_field(self, page, field, value) -> None:
        await self._browser_form().fill_field(page, field, value)

    async def attach_resume(self, page, pdf_path: str | Path) -> None:
        await self._browser_form().attach_resume(page, pdf_path)

    async def attach_cover_letter(self, page, pdf_path: str | Path) -> None:
        await self._browser_form().attach_cover_letter(page, pdf_path)

    async def submit(self, page) -> SubmitResult:
        return await self._browser_form().submit(page)

    def _browser_form(self):
        from backend.scraping.adapters.browser_form import BrowserFormAdapter

        return BrowserFormAdapter()


def _finalize_description(description: str, *, job_url: str, adapter: str) -> str:
    text = (description or "").strip()
    log.info("job_description_extracted", adapter=adapter, job_url=job_url, characters=len(text))
    if len(text) < 200:
        log.error("job_description_too_short", adapter=adapter, job_url=job_url, characters=len(text))
        raise RuntimeError(f"Extracted job description is too short ({len(text)} chars) for {job_url}")
    return text


async def _exhaust_load_more(page, career_url: str) -> None:
    for _attempt in range(MAX_LOAD_MORE_CLICKS):
        current_count = _listing_count(await page.content(), career_url)
        control = await _visible_load_more_control(page)
        if control is None:
            return
        try:
            await control.scroll_into_view_if_needed()
            await control.click()
        except Exception as exc:
            log.debug("load_more_click_failed", url=career_url, error=str(exc))
            return
        increased = await _wait_for_listing_growth(page, career_url, current_count)
        if not increased:
            if await _visible_load_more_control(page) is None:
                return
            log.debug("load_more_no_listing_growth", url=career_url, count=current_count)
            return


async def _visible_load_more_control(page):
    for selector in LOAD_MORE_SELECTORS:
        locator = page.locator(selector).last
        try:
            if await locator.count() and await locator.is_visible() and await locator.is_enabled():
                return locator
        except Exception as exc:
            log.debug("load_more_probe_failed", selector=selector, error=str(exc))
    return None


async def _wait_for_listing_growth(page, career_url: str, previous_count: int) -> bool:
    for _ in range(20):
        try:
            await page.wait_for_load_state("networkidle", timeout=500)
        except Exception:
            await page.wait_for_timeout(150)
        if _listing_count(await page.content(), career_url) > previous_count:
            return True
    return False


def _listing_count(html: str, career_url: str) -> int:
    return len({listing.url for listing in discover_job_links(html, career_url)})
