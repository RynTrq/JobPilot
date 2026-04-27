from __future__ import annotations

import re
from urllib.parse import urljoin
from urllib.parse import urlparse
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup

if TYPE_CHECKING:
    from backend.scraping.adapters.base import JobListing


JOB_PATH_MARKERS = (
    "/jobs/",
    "/job/",
    "/positions/",
    "/position/",
    "/openings/",
    "/opening/",
    "/careers/",
)
JOB_QUERY_KEYS = ("gh_jid", "jobid", "job_id", "job", "reqid", "req_id")
ROLE_WORDS = (
    "engineer",
    "developer",
    "artist",
    "software",
    "frontend",
    "front-end",
    "backend",
    "back-end",
    "full stack",
    "full-stack",
    "intern",
    "internship",
    "analyst",
    "scientist",
    "designer",
    "architect",
    "researcher",
    "associate",
    "consultant",
    "manager",
    "lead",
    "executive",
    "specialist",
    "supervisor",
    "copywriter",
    "director",
    "agent",
    "writer",
    "rigger",
    "president",
    "partner",
)
ROLE_TITLE_NOUNS = frozenset(
    {
        "agent",
        "analyst",
        "architect",
        "artist",
        "associate",
        "consultant",
        "designer",
        "developer",
        "director",
        "engineer",
        "executive",
        "frontend",
        "backend",
        "intern",
        "internship",
        "lead",
        "manager",
        "partner",
        "president",
        "researcher",
        "rigger",
        "scientist",
        "specialist",
        "supervisor",
        "writer",
    }
)
NAV_TEXT_EXACT = {
    "careers",
    "career",
    "jobs",
    "open positions",
    "positions",
    "openings",
    "data management",
    "bengaluru",
}
NAV_TEXT_PREFIXES = ("previous", "next", "back to", "view all", "all jobs", "all roles")
CATEGORY_SLUGS = {
    "business",
    "corporate",
    "corporate-engineering",
    "engineering",
    "graduate",
    "graduates",
    "internship",
    "internships",
    "product",
    "security-engineering",
    "students",
    "technology",
}


def discover_job_links(html: str, base_url: str) -> list[JobListing]:
    from backend.scraping.adapters.base import JobListing

    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    listings: list[JobListing] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        text = " ".join(anchor.get_text(" ").split())
        if not _looks_like_job_anchor(anchor, href, text):
            continue
        url = urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)
        listings.append(JobListing(url=url, title_preview=text[:160]))
    return listings


def looks_like_direct_job_url(url: str) -> bool:
    parsed = urlparse(url.lower())
    path = parsed.path
    query = parsed.query
    parts = [part for part in path.split("/") if part]
    if _looks_like_workable_job_path(parsed.hostname or "", parts):
        return True
    if parts and parts[-1] in CATEGORY_SLUGS:
        return False
    if "jobs" in parts and len(parts) <= parts.index("jobs") + 2 and parts[-1:] and parts[-1] in CATEGORY_SLUGS:
        return False
    has_job_url_shape = any(marker in path for marker in JOB_PATH_MARKERS) or any(f"{key}=" in query for key in JOB_QUERY_KEYS)
    if not has_job_url_shape:
        return _looks_like_role_slug(parts[-1]) if parts else False
    slug_text = path.replace("-", " ").replace("_", " ").replace("/", " ")
    return _contains_role_signal(slug_text) or any(f"{key}=" in query for key in JOB_QUERY_KEYS)


def title_from_job_page(html: str, fallback_url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    nodes = [*soup.select("h1"), *soup.select("meta[property='og:title']"), *soup.select("title")]
    for node in nodes:
        text = node.get("content") if node.name == "meta" else node.get_text(" ")
        text = " ".join((text or "").split())
        if text and not _is_generic_title(text):
            return text[:160]
    slug = urlparse(fallback_url).path.rstrip("/").split("/")[-1]
    return slug.replace("-", " ").replace("_", " ").title()[:160]


def _looks_like_job_anchor(anchor, href: str, text: str) -> bool:
    if not text:
        return False
    href_lower = href.lower().strip()
    if href_lower.startswith(("#", "mailto:", "tel:", "javascript:")):
        return False
    if anchor.find_parent(["nav", "header", "footer"]):
        return False

    text_lower = text.lower().strip()
    compact_text = " ".join(text_lower.split())
    if compact_text in NAV_TEXT_EXACT:
        return False
    if compact_text.startswith(NAV_TEXT_PREFIXES):
        return False
    if re.search(r"\b(previous|next)\b", compact_text):
        return False

    if not looks_like_direct_job_url(href_lower):
        return False
    return _contains_role_signal(compact_text)


def _contains_role_signal(text: str) -> bool:
    normalized = _normalize_slug_text(text)
    return any(re.search(rf"\b{re.escape(word)}\b", normalized) for word in ROLE_WORDS)


def _looks_like_role_slug(slug: str) -> bool:
    normalized = _normalize_slug_text(slug)
    tokens = normalized.split()
    if len(tokens) < 2:
        return False
    return any(token in ROLE_TITLE_NOUNS for token in tokens)


def _looks_like_workable_job_path(host: str, parts: list[str]) -> bool:
    if not host.endswith("workable.com"):
        return False
    if "j" not in parts:
        return False
    index = parts.index("j")
    return len(parts) > index + 1 and bool(parts[index + 1])


def _normalize_slug_text(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def _is_generic_title(text: str) -> bool:
    normalized = _normalize_slug_text(text)
    category_titles = {slug.replace("-", " ") for slug in CATEGORY_SLUGS}
    return normalized in NAV_TEXT_EXACT or normalized in category_titles or normalized in {"job", "job details", "career", "careers", "opening", "openings"}
