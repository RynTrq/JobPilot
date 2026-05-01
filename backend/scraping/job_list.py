from __future__ import annotations

import json
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
JOB_QUERY_KEYS = ("gh_jid", "jobid", "job_id", "job", "reqid", "req_id", "jk", "jid", "jobId", "jobRef", "ref")
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
    # additional common tech / fresher role terms
    "programmer",
    "coder",
    "sde",
    "swe",
    "devops",
    "sre",
    "qa",
    "tester",
    "trainee",
    "fresher",
    "graduate",
    "apprentice",
    "technician",
    "administrator",
    "coordinator",
    "strategist",
    "operator",
    "practitioner",
    "engineer-1",
    "eng",
    "dev",
    "data",
    "cloud",
    "mobile",
    "ios",
    "android",
    "ml",
    "ai",
    "nlp",
    "platform",
    "infrastructure",
    "systems",
    "support",
    "technical",
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
        # additional
        "programmer",
        "coder",
        "sde",
        "swe",
        "devops",
        "sre",
        "qa",
        "tester",
        "trainee",
        "fresher",
        "graduate",
        "apprentice",
        "technician",
        "administrator",
        "coordinator",
        "practitioner",
        "eng",
        "dev",
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
        # Resolve to full URL first so host-aware checks (e.g. Workable) work on relative hrefs
        url = urljoin(base_url, href)
        if not _looks_like_job_anchor(anchor, url, text):
            continue
        if url in seen:
            continue
        seen.add(url)
        # If anchor text is generic, pull a better title from the parent container
        title = text
        if not _contains_role_signal(text.lower()):
            parent = anchor.find_parent(["li", "article", "tr", "div"])
            if parent:
                # Look for heading or title element in parent
                heading = parent.find(["h1", "h2", "h3", "h4", "[class*='title']"])
                if heading:
                    candidate = " ".join(heading.get_text(" ").split())
                    if candidate and _contains_role_signal(candidate.lower()):
                        title = candidate
                if not _contains_role_signal(title.lower()):
                    parent_text = " ".join(parent.get_text(" ").split())[:200]
                    if _contains_role_signal(parent_text.lower()):
                        title = parent_text
        listings.append(JobListing(url=url, title_preview=title[:160]))
    for listing in _discover_phenom_job_records(html, base_url):
        if listing.url in seen:
            continue
        seen.add(listing.url)
        listings.append(listing)
    return listings


def discover_phenom_job_records(html: str, base_url: str) -> list[JobListing]:
    return _discover_phenom_job_records(html, base_url)


def phenom_result_count(html: str) -> int | None:
    ddo = _parse_phenom_ddo(html)
    if not ddo:
        return None
    data = (((ddo.get("eagerLoadRefineSearch") or {}).get("data")) or {})
    for key in ("totalJobs", "total", "count", "jobCount", "totalHits"):
        value = _to_int(data.get(key))
        if value is not None and value > 0:
            return value

    totals: list[int] = []
    for aggregation in data.get("aggregations") or []:
        if not isinstance(aggregation, dict):
            continue
        values = aggregation.get("value")
        if not isinstance(values, dict):
            continue
        total = sum(_to_int(value) or 0 for value in values.values())
        if total > 0:
            totals.append(total)
    return max(totals) if totals else None


def phenom_page_size(html: str) -> int | None:
    ddo = _parse_phenom_ddo(html)
    if not ddo:
        return None
    candidates = [
        (((ddo.get("siteConfig") or {}).get("data")) or {}),
        (((ddo.get("eagerLoadRefineSearch") or {}).get("data")) or {}),
    ]
    for source in candidates:
        for key in ("size", "pageSize", "limit"):
            value = _to_int(source.get(key))
            if value is not None and value > 0:
                return value
    jobs = (((ddo.get("eagerLoadRefineSearch") or {}).get("data") or {}).get("jobs") or [])
    return len(jobs) or None


def looks_like_direct_job_url(url: str) -> bool:
    parsed = urlparse(url.lower())
    path = parsed.path
    query = parsed.query
    parts = [part for part in path.split("/") if part]
    host = parsed.hostname or ""
    # Known ATS domains always host direct job URLs for deep paths
    if _looks_like_workable_job_path(host, parts):
        return True
    if _looks_like_known_ats_job_url(host, parts):
        return True
    if parts and parts[-1] in CATEGORY_SLUGS:
        return False
    if "jobs" in parts and len(parts) <= parts.index("jobs") + 2 and parts[-1:] and parts[-1] in CATEGORY_SLUGS:
        return False
    has_job_url_shape = any(marker in path for marker in JOB_PATH_MARKERS) or any(f"{key}=" in query for key in JOB_QUERY_KEYS)
    if not has_job_url_shape:
        return _looks_like_role_slug(parts[-1]) if parts else False
    # Has a job-path-marker URL — require either a role signal OR a job-ID-like last segment
    # (many ATS use numeric/UUID IDs that carry no role words)
    slug_text = path.replace("-", " ").replace("_", " ").replace("/", " ")
    last_part = parts[-1] if parts else ""
    return (
        _contains_role_signal(slug_text)
        or any(f"{key}=" in query for key in JOB_QUERY_KEYS)
        or _looks_like_job_id_segment(last_part)
    )


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
        text = ""
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

    if _contains_role_signal(compact_text):
        return True

    # URL slug itself may carry the role signal even when anchor text is generic
    # (e.g. href="/position/sde-fresher-2026" with text "Apply Now")
    url_path = href_lower.split("?")[0]
    url_slug = url_path.replace("-", " ").replace("_", " ").replace("/", " ")
    if _contains_role_signal(url_slug):
        return True

    # Anchor text may be generic ("View", "Apply", "Details") — check the
    # nearest list-item or parent container for a role title signal.
    parent = anchor.find_parent(["li", "article", "tr", "div"])
    if parent:
        parent_text = " ".join(parent.get_text(" ").split()).lower()[:300]
        if _contains_role_signal(parent_text):
            return True

    return False


def _discover_phenom_job_records(html: str, base_url: str) -> list[JobListing]:
    """Extract server-rendered Phenom job records from search result pages."""
    from backend.scraping.adapters.base import JobListing

    ddo = _parse_phenom_ddo(html)
    if not ddo:
        return []
    jobs = (((ddo.get("eagerLoadRefineSearch") or {}).get("data") or {}).get("jobs") or [])
    if not isinstance(jobs, list):
        return []

    listings: list[JobListing] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_id = _clean_text(job.get("jobId") or job.get("reqId"))
        title = _clean_text(job.get("title"))
        if not job_id or not title:
            continue
        slug = _slugify_job_title(title)
        url = urljoin(base_url, f"/global/en/job/{job_id}/{slug}")
        listings.append(
            JobListing(
                url=url,
                title_preview=title[:160],
                location_preview=_clean_text(job.get("location") or job.get("cityStateCountry") or job.get("cityState")),
                ext_id=_clean_text(job.get("jobSeqNo") or job_id),
                company=_clean_text(job.get("businessUnit") or job.get("brand")),
            )
        )
    return listings


def _parse_phenom_ddo(html: str) -> dict | None:
    match = re.search(r"phApp\.ddo\s*=\s*(\{.*?\})\s*;\s*phApp\.experimentData", html, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _slugify_job_title(title: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-") or "job"


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _to_int(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


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


# Known ATS platforms whose job URLs follow company/job-id patterns without
# standard /jobs/ path markers.
_ATS_JOB_DOMAINS = {
    "lever.co": 2,          # /company/uuid  — 2 segments
    "ashbyhq.com": 2,       # /company/role-id
    "greenhouse.io": 3,     # /company/jobs/id  (but /jobs/ path already catches this)
    "smartrecruiters.com": 2,
    "icims.com": 0,         # tenant-specific, skip
    "myworkdayjobs.com": 0, # caught by path markers
}


def _looks_like_known_ats_job_url(host: str, parts: list[str]) -> bool:
    """Return True when host is a known ATS with a deep enough path to be a single job."""
    for domain, min_parts in _ATS_JOB_DOMAINS.items():
        if min_parts < 1:
            continue
        if host == domain or host.endswith(f".{domain}"):
            if len(parts) >= min_parts:
                # Last segment must not be a category slug
                return parts[-1] not in CATEGORY_SLUGS
    return False


def _looks_like_job_id_segment(segment: str) -> bool:
    """Return True if the segment looks like a job ID (alphanumeric with digits, UUID, etc.)."""
    if not segment:
        return False
    if segment in CATEGORY_SLUGS:
        return False
    # Pure numeric — must be long enough to be a job ID, not a page number (e.g. "2", "10")
    if segment.isdigit():
        return len(segment) >= 5
    # Alphanumeric mix (typical ATS IDs like "abc123def", "R12345", "SWE001")
    stripped = segment.replace("-", "").replace("_", "")
    if stripped.isalnum() and any(c.isdigit() for c in stripped) and any(c.isalpha() for c in stripped):
        return len(stripped) >= 4
    return False


def _normalize_slug_text(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def _is_generic_title(text: str) -> bool:
    normalized = _normalize_slug_text(text)
    category_titles = {slug.replace("-", " ") for slug in CATEGORY_SLUGS}
    return normalized in NAV_TEXT_EXACT or normalized in category_titles or normalized in {"job", "job details", "career", "careers", "opening", "openings"}
