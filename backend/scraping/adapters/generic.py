from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urljoin
from urllib.parse import urlparse
from urllib.parse import urlunparse

from bs4 import BeautifulSoup
import structlog

from backend.scraping.adapters.base import AdapterBase
from backend.scraping.adapters.base import JobListing
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
MAX_PAGINATION_PAGES = 50
PAGINATION_QUERY_KEYS = {
    "from",
    "start",
    "offset",
    "page",
    "p",
    "pageNumber",
    "pageNo",
    "currentPage",
}
PAGINATION_TEXT_EXACT = {"next", "prev", "previous"}


class GenericAdapter(AdapterBase):
    async def list_jobs(self, page, career_url: str):
        await goto_with_pacing(page, career_url, timeout=30000)
        html = await page.content()
        if looks_like_direct_job_url(career_url):
            from backend.scraping.adapters.base import JobListing

            return [JobListing(url=career_url, title_preview=title_from_job_page(html, career_url))]
        await _exhaust_load_more(page, career_url)
        html = await page.content()
        listings = await _discover_paginated_job_links(page, career_url, html)
        if listings:
            return listings
        try:
            await page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            await page.wait_for_timeout(1000)
        await _exhaust_load_more(page, career_url)
        return await _discover_paginated_job_links(page, career_url, await page.content())

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


async def _discover_paginated_job_links(page, career_url: str, initial_html: str) -> list[JobListing]:
    listings_by_url: dict[str, JobListing] = {}
    visited = {_normalize_page_url(getattr(page, "url", None) or career_url)}
    pending = _pagination_links(
        initial_html,
        getattr(page, "url", None) or career_url,
        career_url,
        visited=visited,
    )

    _merge_listings(listings_by_url, discover_job_links(initial_html, career_url))

    while pending and len(visited) < MAX_PAGINATION_PAGES:
        next_url = pending.pop(0)
        normalized = _normalize_page_url(next_url)
        if normalized in visited:
            continue
        visited.add(normalized)
        try:
            await goto_with_pacing(page, next_url, timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:
                await page.wait_for_timeout(750)
            await _exhaust_load_more(page, next_url)
            html = await page.content()
        except Exception as exc:
            log.debug("pagination_page_fetch_failed", url=next_url, error=str(exc))
            continue

        before = len(listings_by_url)
        _merge_listings(listings_by_url, discover_job_links(html, next_url))
        discovered = len(listings_by_url) - before
        log.debug("pagination_page_scraped", url=next_url, discovered=discovered, total=len(listings_by_url))

        known_pages = visited | {_normalize_page_url(url) for url in pending}
        pending.extend(
            _pagination_links(
                html,
                getattr(page, "url", None) or next_url,
                career_url,
                visited=known_pages,
            )
        )

    return list(listings_by_url.values())


def _merge_listings(target: dict[str, JobListing], listings: list[JobListing]) -> None:
    for listing in listings:
        target.setdefault(listing.url, listing)


def _pagination_links(
    html: str,
    current_url: str,
    career_url: str,
    *,
    visited: set[str] | None = None,
) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    current = urlparse(current_url or career_url)
    career = urlparse(career_url)
    seen = set(visited or set())
    out: list[str] = []

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        target_url = urljoin(current_url or career_url, href)
        target = urlparse(target_url)
        if not _same_listing_page(target, current, career):
            continue
        if looks_like_direct_job_url(target_url):
            continue
        if not _looks_like_pagination_anchor(anchor, target):
            continue
        normalized = _normalize_page_url(target_url)
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(_strip_fragment(target_url))

    return [url for _, url in sorted(enumerate(out), key=lambda item: (_pagination_sort_key(item[1]), item[0]))]


def _same_listing_page(target, current, career) -> bool:
    expected_host = (career.netloc or current.netloc).lower()
    target_host = target.netloc.lower()
    if target_host and expected_host and target_host != expected_host:
        return False
    current_path = (current.path or career.path or "/").rstrip("/") or "/"
    target_path = (target.path or "/").rstrip("/") or "/"
    return target_path == current_path


def _looks_like_pagination_anchor(anchor, target) -> bool:
    text = _compact(anchor.get_text(" "))
    aria = _compact(str(anchor.get("aria-label") or ""))
    classes = _compact(" ".join(anchor.get("class") or []))
    if "disabled" in classes or "active" in classes or "selected" in classes:
        return False
    query = parse_qs(target.query)
    has_page_query = any(key in query for key in PAGINATION_QUERY_KEYS)
    has_page_text = (
        text.isdigit()
        or text in PAGINATION_TEXT_EXACT
        or aria.startswith("page ")
        or aria in PAGINATION_TEXT_EXACT
    )
    has_page_class = "pagination" in classes or "next previous" in classes or "next-previous" in classes
    return has_page_query and (has_page_text or has_page_class)


def _pagination_sort_key(url: str) -> int:
    query = parse_qs(urlparse(url).query)
    for key in ("from", "start", "offset"):
        value = _first_int(query.get(key))
        if value is not None:
            return value
    for key in ("page", "p", "pageNumber", "pageNo", "currentPage"):
        value = _first_int(query.get(key))
        if value is not None:
            return value * 1000
    return 10**9


def _first_int(values: list[str] | None) -> int | None:
    if not values:
        return None
    try:
        return int(values[0])
    except (TypeError, ValueError):
        return None


def _normalize_page_url(url: str) -> str:
    parsed = urlparse(_strip_fragment(url))
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/") or "/", "", query, ""))


def _strip_fragment(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, ""))


def _compact(text: str) -> str:
    return " ".join(text.lower().split())


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
