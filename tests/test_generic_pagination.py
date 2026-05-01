import json

import pytest

from backend.scraping.adapters.generic import _discover_phenom_listing_pages
from backend.scraping.adapters.generic import _has_pagination_controls
from backend.scraping.adapters.generic import _pagination_links
from backend.scraping.adapters.generic import _pagination_offset_url
from backend.scraping.job_list import discover_job_links
from backend.scraping.job_list import discover_phenom_job_records
from backend.scraping.job_list import phenom_page_size
from backend.scraping.job_list import phenom_result_count


def test_pagination_links_detects_g42_numbered_pages_and_next() -> None:
    html = """
    <section aria-label="Search results">
      <a href="/global/en/job/1726/Senior-Robotics-Engineer">Senior Robotics Engineer</a>
      <nav>
        <a class="phw-g-next-previous-button phw-g-pagination-block-link"
           aria-label="Previous">Prev</a>
        <a class="_selected-page-number active phw-g-pagination-block-link"
           href="https://careers.g42.ai/global/en/search-results?s=1"
           aria-label="Page 1">1</a>
        <a class="phw-g-pagination-block-link"
           href="https://careers.g42.ai/global/en/search-results?from=10&amp;s=1"
           aria-label="Page 2">2</a>
        <a class="phw-g-pagination-block-link"
           href="https://careers.g42.ai/global/en/search-results?from=20&amp;s=1"
           aria-label="Page 3">3</a>
        <a class="phw-g-next-previous-button phw-g-pagination-block-link"
           href="https://careers.g42.ai/global/en/search-results?from=10&amp;s=1"
           aria-label="Next">Next</a>
      </nav>
    </section>
    """

    links = _pagination_links(
        html,
        "https://careers.g42.ai/global/en/search-results",
        "https://careers.g42.ai/global/en/search-results",
        visited={"https://careers.g42.ai/global/en/search-results"},
    )

    assert links == [
        "https://careers.g42.ai/global/en/search-results?from=10&s=1",
        "https://careers.g42.ai/global/en/search-results?from=20&s=1",
    ]


def test_pagination_links_ignores_job_and_apply_links() -> None:
    html = """
    <a href="https://careers.g42.ai/global/en/job/1726/Senior-Robotics-Engineer">
      Senior Robotics Engineer
    </a>
    <a href="https://careers.g42.ai/global/en/apply?jobSeqNo=OGWOGJGLOBAL1726EXTERNALENGLOBAL">
      Apply Now
    </a>
    <a href="https://careers.g42.ai/global/en/search-results?from=10&amp;s=1"
       aria-label="Page 2">2</a>
    """

    links = _pagination_links(
        html,
        "https://careers.g42.ai/global/en/search-results",
        "https://careers.g42.ai/global/en/search-results",
    )

    assert links == ["https://careers.g42.ai/global/en/search-results?from=10&s=1"]


def test_discover_job_links_accepts_g42_role_titles() -> None:
    html = """
    <a href="https://careers.g42.ai/global/en/job/2702/Compliance-Intelligence-Agent">
      Compliance Intelligence Agent
    </a>
    <a href="https://careers.g42.ai/global/en/job/2562/Director-Solution-Engineering">
      Director - Solution Engineering
    </a>
    <a href="https://careers.g42.ai/global/en/job/2471/Senior-Technical-Writer-CPX">
      Senior Technical Writer (CPX)
    </a>
    <a href="https://careers.g42.ai/global/en/job/2414/3D-Artist">
      3D Artist
    </a>
    <a href="https://careers.g42.ai/global/en/job/950/Vice-President-Satellite-Operations">
      Vice President - Satellite Operations
    </a>
    """

    links = discover_job_links(html, "https://careers.g42.ai/global/en/search-results")

    assert [item.title_preview for item in links] == [
        "Compliance Intelligence Agent",
        "Director - Solution Engineering",
        "Senior Technical Writer (CPX)",
        "3D Artist",
        "Vice President - Satellite Operations",
    ]


def test_phenom_metadata_derives_total_and_page_size() -> None:
    html = """
    <script>
    phApp.ddo = {
      "siteConfig": {"data": {"size": "10"}},
      "eagerLoadRefineSearch": {
        "data": {
          "jobs": [
            {"jobId": "2707", "title": "Strategic Sourcing Intelligence Agent"}
          ],
          "aggregations": [
            {"field": "country", "value": {"United Arab Emirates": 87, "United States": 3, "France": 1, "Ireland": 1}},
            {"field": "businessUnit", "value": {"CPX": 24, "Inception": 24, "Analog": 22, "Corporate": 6, "Space42": 6, "CORE42": 5, "Presight": 4, "AIQ": 1}}
          ]
        }
      }
    }; phApp.experimentData = {};
    </script>
    """

    assert phenom_result_count(html) == 92
    assert phenom_page_size(html) == 10
    listings = discover_phenom_job_records(html, "https://careers.g42.ai/global/en/search-results?s=1")
    assert listings[0].url == "https://careers.g42.ai/global/en/job/2707/Strategic-Sourcing-Intelligence-Agent"


def test_pagination_offset_url_preserves_filters_and_replaces_from() -> None:
    url = "https://careers.g42.ai/global/en/search-results?from=60&s=1&category=Data"

    assert _pagination_offset_url(url, 0) == "https://careers.g42.ai/global/en/search-results?category=Data&s=1"
    assert _pagination_offset_url(url, 80) == "https://careers.g42.ai/global/en/search-results?category=Data&from=80&s=1"


def test_pagination_controls_stop_infinite_scroll_probe() -> None:
    html = """
    <nav>
      <a href="https://careers.g42.ai/global/en/search-results?from=50&amp;s=1"
         aria-label="Previous">Prev</a>
      <a href="https://careers.g42.ai/global/en/search-results?from=70&amp;s=1"
         aria-label="Next">Next</a>
    </nav>
    """

    assert _has_pagination_controls(
        html,
        "https://careers.g42.ai/global/en/search-results?from=60&s=1",
        "https://careers.g42.ai/global/en/search-results",
    )


@pytest.mark.asyncio
async def test_phenom_fast_path_fetches_remaining_offsets() -> None:
    career_url = "https://careers.g42.ai/global/en/search-results?s=1"
    next_url = "https://careers.g42.ai/global/en/search-results?from=10&s=1"
    initial_html = _phenom_html([("1001", "Senior Data Engineer")], total=12, size=10)
    next_html = _phenom_html([("1002", "ML Scientist"), ("1003", "Cloud Architect")], total=12, size=10)
    request = _FakeRequest({next_url: next_html})
    page = _FakePage(career_url, request)

    listings = await _discover_phenom_listing_pages(page, career_url, initial_html)

    assert request.urls == [next_url]
    assert [listing.title_preview for listing in listings] == [
        "Senior Data Engineer",
        "ML Scientist",
        "Cloud Architect",
    ]


def _phenom_html(jobs: list[tuple[str, str]], *, total: int, size: int) -> str:
    payload = {
        "siteConfig": {"data": {"size": str(size)}},
        "eagerLoadRefineSearch": {
            "data": {
                "jobs": [{"jobId": job_id, "title": title} for job_id, title in jobs],
                "aggregations": [{"field": "country", "value": {"United Arab Emirates": total}}],
            }
        },
    }
    return f"<script>phApp.ddo = {json.dumps(payload)}; phApp.experimentData = {{}};</script>"


class _FakeResponse:
    ok = True
    status = 200

    def __init__(self, html: str) -> None:
        self._html = html

    async def text(self) -> str:
        return self._html


class _FakeRequest:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.urls: list[str] = []

    async def get(self, url: str, timeout: int):
        self.urls.append(url)
        return _FakeResponse(self.pages[url])


class _FakeContext:
    def __init__(self, request: _FakeRequest) -> None:
        self.request = request


class _FakePage:
    def __init__(self, url: str, request: _FakeRequest) -> None:
        self.url = url
        self.context = _FakeContext(request)
