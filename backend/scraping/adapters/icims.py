from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from backend.scraping.adapters.base import JobListing
from backend.scraping.adapters.browser_form import BrowserFormAdapter


class IcimsAdapter(BrowserFormAdapter):
    async def list_jobs(self, page, career_url: str) -> list[JobListing]:
        await self._goto(page, career_url)
        soup = BeautifulSoup(await page.content(), "html.parser")
        if "/jobs/" in career_url and not career_url.rstrip("/").endswith("/jobs"):
            return [JobListing(url=career_url, title_preview=_first_text(soup, ["h1", ".iCIMS_Header"]))]
        listings = []
        for node in soup.select("a[href*='/jobs/'], a[href*='icims.com/jobs']"):
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
