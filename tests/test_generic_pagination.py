from backend.scraping.adapters.generic import _pagination_links
from backend.scraping.job_list import discover_job_links


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
