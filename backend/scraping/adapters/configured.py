from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
import structlog

from backend.form.answerer import FormField
from backend.scraping.adapters.base import JobListing, SubmitResult
from backend.scraping.adapters.browser_form import BrowserFormAdapter
from backend.scraping.adapters.generic import _finalize_description
from backend.scraping.adapters.platform_catalog import PlatformConfig, find_platform_config
from backend.scraping.adapters.session import PlatformSessionManager
from backend.scraping.job_list import discover_job_links, looks_like_direct_job_url, title_from_job_page
from backend.scraping.job_page import extract_text_from_page

log = structlog.get_logger()


class InactiveListingError(RuntimeError):
    pass


class ConfiguredPlatformAdapter(BrowserFormAdapter):
    """Config-backed dedicated adapter for regional boards and ATS tenants."""

    def __init__(self, platform: PlatformConfig) -> None:
        self.platform = platform
        self.apply_selectors = _dedupe_strings((*platform.apply_selectors, *BrowserFormAdapter.apply_selectors))
        self.session = PlatformSessionManager(
            platform.key,
            auth_required=platform.auth_required,
            username_env=platform.username_env,
            password_env=platform.password_env,
        )

    async def list_jobs(self, page, career_url: str) -> list[JobListing]:
        await self._goto(page, career_url)
        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")
        links = self._parse_listing_links(html, career_url)
        if links and not self._looks_like_direct_platform_job_url(career_url, soup, link_count=len(links)):
            return links
        title = title_from_job_page(html, career_url)
        if self._looks_like_direct_platform_job_url(career_url, soup, link_count=len(links)):
            return [
                JobListing(
                    url=career_url,
                    title_preview=title,
                    location_preview=_first_text(soup, ("[class*='location']", "[data-testid*='location']", "[data-automation-id*='location']")),
                    ext_id=_external_id(career_url),
                    company=self.platform.name,
                )
            ]
        fallback = discover_job_links(html, career_url)
        return self._filter_platform_links(fallback) or links

    async def is_active_listing(self, page, job_url: str) -> bool:
        await self._goto(page, job_url)
        html = await page.content()
        active, reason = listing_html_is_active(html, self.platform)
        if not active:
            log.info("inactive_listing_skipped", platform=self.platform.key, job_url=job_url, reason=reason)
        return active

    async def extract_description(self, page, job_url: str) -> str:
        if not await self.is_active_listing(page, job_url):
            raise InactiveListingError(f"{self.platform.name} listing is closed or inactive: {job_url}")
        description = await extract_text_from_page(page)
        return _finalize_description(description, job_url=job_url, adapter=self.__class__.__name__)

    async def open_application(self, page, job_url: str) -> None:
        await self._goto(page, job_url)
        if not await self.is_active_listing(page, job_url):
            raise InactiveListingError(f"{self.platform.name} listing is closed or inactive: {job_url}")
        await self.session.ensure_ready(page)
        await super().open_application(page, job_url)

    async def enumerate_fields(self, page) -> list[FormField]:
        return await super().enumerate_fields(page)

    async def fill_field(self, page, field: FormField, value) -> None:
        await super().fill_field(page, field, value)

    async def attach_resume(self, page, pdf_path: str | Path) -> None:
        await super().attach_resume(page, pdf_path)

    async def attach_cover_letter(self, page, pdf_path: str | Path) -> None:
        await super().attach_cover_letter(page, pdf_path)

    async def submit(self, page) -> SubmitResult:
        return await super().submit(page)

    def _parse_listing_links(self, html: str, base_url: str) -> list[JobListing]:
        soup = BeautifulSoup(html, "html.parser")
        listings: list[JobListing] = []
        for selector in self.platform.listing_selectors:
            for node in soup.select(selector):
                href = node.get("href")
                if not href:
                    continue
                url = urljoin(base_url, href)
                if not self._is_platform_url(url):
                    continue
                if not self._looks_like_listing_url(url):
                    continue
                title = " ".join(node.get_text(" ").split()) or None
                if not title:
                    title = _first_text(BeautifulSoup(str(node.parent or node), "html.parser"), ("h2", "h3", "[class*='title']", "[data-testid*='title']"))
                parent_text = " ".join((node.parent.get_text(" ") if node.parent else "").split())
                location = (parent_text.replace(title or "", "").strip()[:140] or None) if parent_text else None
                listings.append(
                    JobListing(
                        url=url,
                        title_preview=(title or title_from_job_page(str(node), url))[:180],
                        location_preview=location,
                        ext_id=_external_id(url),
                        company=self.platform.name,
                    )
                )
        return _dedupe_listings(listings)

    def _filter_platform_links(self, listings: list[JobListing]) -> list[JobListing]:
        return _dedupe_listings([listing for listing in listings if self._is_platform_url(listing.url)])

    def _is_platform_url(self, url: str) -> bool:
        configured = find_platform_config(url)
        return configured is not None and configured.key == self.platform.key

    def _looks_like_listing_url(self, url: str) -> bool:
        parsed = urlparse(url.lower())
        path = parsed.path
        query = parsed.query
        if any(marker in path for marker in self.platform.direct_path_markers):
            return True
        if any(key in parse_qs(query) for key in ("jobid", "job_id", "gh_jid", "jk", "reqid", "req_id", "posting_id")):
            return True
        return looks_like_direct_job_url(url)

    def _looks_like_direct_platform_job_url(self, url: str, soup: BeautifulSoup, *, link_count: int = 0) -> bool:
        if link_count > 2 and not looks_like_direct_job_url(url):
            return False
        if looks_like_direct_job_url(url):
            return True
        parsed = urlparse(url.lower())
        if any(marker in parsed.path for marker in self.platform.direct_path_markers) and link_count <= 2:
            return True
        if soup.select_one("form, input[type='file'], a[href*='apply']"):
            return True
        if any("apply" in " ".join(button.get_text(" ").split()).lower() for button in soup.select("button, [role='button']")):
            return True
        return False


_ADAPTER_TYPES: dict[str, type[ConfiguredPlatformAdapter]] = {}


def adapter_for_platform(platform: PlatformConfig) -> ConfiguredPlatformAdapter:
    cls = _ADAPTER_TYPES.get(platform.key)
    if cls is None:
        cls = type(platform.adapter_class_name, (ConfiguredPlatformAdapter,), {"__module__": __name__})
        _ADAPTER_TYPES[platform.key] = cls
    return cls(platform)


def adapter_for_url(url: str) -> ConfiguredPlatformAdapter | None:
    platform = find_platform_config(url)
    if platform is None:
        return None
    return adapter_for_platform(platform)


def listing_html_is_active(html: str, platform: PlatformConfig) -> tuple[bool, str | None]:
    soup = BeautifulSoup(html, "html.parser")
    text = " ".join(soup.get_text(" ").split()).lower()
    for marker in platform.inactive_markers:
        if marker in text:
            return False, marker
    valid_through = _json_ld_valid_through(soup)
    if valid_through and valid_through < datetime.now(timezone.utc):
        return False, f"validThrough={valid_through.isoformat()}"
    return True, None


def _json_ld_valid_through(soup: BeautifulSoup) -> datetime | None:
    dates: list[datetime] = []
    for script in soup.select("script[type='application/ld+json']"):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        for value in _walk_json(payload):
            if isinstance(value, dict) and str(value.get("@type", "")).lower() == "jobposting" and value.get("validThrough"):
                parsed = _parse_datetime(str(value["validThrough"]))
                if parsed:
                    dates.append(parsed)
    return min(dates) if dates else None


def _walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _parse_datetime(value: str) -> datetime | None:
    normalized = value.strip().replace("Z", "+00:00")
    date_only = "T" not in normalized
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(normalized.split("T", 1)[0])
            date_only = True
        except ValueError:
            return None
    if date_only:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _external_id(url: str) -> str | None:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("jobid", "job_id", "gh_jid", "jk", "reqid", "req_id", "posting_id"):
        if query.get(key):
            return query[key][0]
    parts = [part for part in parsed.path.split("/") if part]
    return parts[-1] if parts else None


def _first_text(soup: BeautifulSoup, selectors: tuple[str, ...]) -> str | None:
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            text = " ".join(node.get_text(" ").split())
            if text:
                return text[:180]
    return None


def _dedupe_listings(listings: list[JobListing]) -> list[JobListing]:
    out: dict[str, JobListing] = {}
    for listing in listings:
        out[listing.url] = listing
    return list(out.values())


def _dedupe_strings(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
