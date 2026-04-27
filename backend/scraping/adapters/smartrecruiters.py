from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from backend.scraping.adapters.base import JobListing
from backend.scraping.adapters.browser_form import BrowserFormAdapter


class SmartRecruitersAdapter(BrowserFormAdapter):
    apply_selectors = (
        "button:has-text(\"I'm interested\")",
        "a:has-text(\"I'm interested\")",
        "button:has-text('Apply')",
        "a:has-text('Apply')",
        *BrowserFormAdapter.apply_selectors,
    )

    async def list_jobs(self, page, career_url: str) -> list[JobListing]:
        await self._goto(page, career_url)
        soup = BeautifulSoup(await page.content(), "html.parser")
        if "jobs.smartrecruiters.com" in career_url and len([p for p in career_url.split("/") if p]) >= 4:
            return [JobListing(url=career_url, title_preview=_first_text(soup, ["h1", "[data-testid='job-title']"]))]
        listings = []
        for node in soup.select("a[href*='jobs.smartrecruiters.com'], a[href*='/jobs/']"):
            href = node.get("href")
            if not href:
                continue
            title = " ".join(node.get_text(" ").split()) or None
            listings.append(JobListing(url=urljoin(career_url, href), title_preview=title))
        return _dedupe(listings) or await super().list_jobs(page, career_url)

    async def open_application(self, page, job_url: str) -> None:
        try:
            await super().open_application(page, job_url)
        except NotImplementedError:
            if "oneclick-ui" in page.url:
                try:
                    body_text = (await page.locator("body").inner_text()).strip()
                except Exception:
                    body_text = ""
                if not body_text:
                    raise NotImplementedError("SmartRecruiters blocked the one-click application page in the browser session.")
            raise
        if "oneclick-ui" in page.url:
            try:
                body_text = (await page.locator("body").inner_text()).strip()
            except Exception:
                body_text = ""
            if not body_text:
                raise NotImplementedError("SmartRecruiters blocked the one-click application page in the browser session.")


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
