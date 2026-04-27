from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from backend.scraping.adapters.base import JobListing
from backend.scraping.adapters.browser_form import BrowserFormAdapter


class AshbyAdapter(BrowserFormAdapter):
    async def list_jobs(self, page, career_url: str) -> list[JobListing]:
        await self._goto(page, career_url)
        html = await page.content()
        app_data = _extract_app_data(html)
        app_data_listings = _listings_from_app_data(app_data, career_url)
        if app_data_listings is not None:
            return app_data_listings
        soup = BeautifulSoup(html, "html.parser")
        parts = [part for part in urlparse(career_url).path.split("/") if part]
        if len(parts) >= 2:
            return [JobListing(url=career_url, title_preview=_first_text(soup, ["h1", "[class*='title']"]), ext_id=parts[-1])]
        listings = []
        for node in soup.select("a[href*='jobs.ashbyhq.com'], a[href*='/jobs/']"):
            href = node.get("href")
            if not href:
                continue
            title = " ".join(node.get_text(" ").split()) or None
            listings.append(JobListing(url=urljoin(career_url, href), title_preview=title))
        return _dedupe(listings) or await super().list_jobs(page, career_url)


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


def _extract_app_data(html: str) -> dict | None:
    match = re.search(r"window\.__appData\s*=\s*(\{.*?\});", html, flags=re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _listings_from_app_data(app_data: dict | None, career_url: str) -> list[JobListing] | None:
    if app_data is None:
        return None
    organization = app_data.get("organization") if isinstance(app_data.get("organization"), dict) else {}
    posting = app_data.get("posting") if isinstance(app_data.get("posting"), dict) else None
    job_board = app_data.get("jobBoard") if isinstance(app_data.get("jobBoard"), dict) else None
    if posting:
        return [
            JobListing(
                url=career_url,
                title_preview=str(posting.get("title") or ""),
                ext_id=str(posting.get("id") or ""),
                company=str(organization.get("name") or "") or None,
                location_preview=str(posting.get("locationName") or "") or None,
            )
        ]
    if job_board is None:
        return []
    postings = job_board.get("jobPostings")
    if not isinstance(postings, list):
        return []
    base = career_url.rstrip("/") + "/"
    listings: list[JobListing] = []
    for item in postings:
        if not isinstance(item, dict):
            continue
        posting_id = str(item.get("id") or "")
        title = str(item.get("title") or "").strip()
        if not posting_id or not title:
            continue
        listings.append(
            JobListing(
                url=urljoin(base, posting_id),
                title_preview=title,
                ext_id=posting_id,
                company=str(organization.get("name") or "") or None,
                location_preview=str(item.get("locationName") or "") or None,
            )
        )
    return listings
