from __future__ import annotations

from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from backend.scraping.adapters.base import JobListing
from backend.scraping.adapters.browser_form import BrowserFormAdapter


class LeverAdapter(BrowserFormAdapter):
    async def list_jobs(self, page, career_url: str) -> list[JobListing]:
        await self._goto(page, career_url)
        soup = BeautifulSoup(await page.content(), "html.parser")
        if _is_closed_posting(soup):
            raise RuntimeError("Lever job posting is closed or removed.")
        parts = [part for part in urlparse(career_url).path.split("/") if part]
        if len(parts) >= 2 or "/jobs/" in career_url or soup.select_one("form, input[name='name'], input[name='email']"):
            return [JobListing(url=career_url, title_preview=_first_text(soup, ["h2", "h1", ".posting-headline h2"]))]
        listings = []
        for node in soup.select(".posting a, a[href*='/jobs/']"):
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


def _is_closed_posting(soup: BeautifulSoup) -> bool:
    text = " ".join((soup.get_text(" ") or "").split()).lower()
    return "sorry, we couldn't find anything here" in text or "job posting you're looking for might have closed" in text
