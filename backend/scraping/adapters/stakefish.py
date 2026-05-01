from __future__ import annotations

from backend.scraping.adapters.base import JobListing
from backend.scraping.adapters.configured import ConfiguredPlatformAdapter
from backend.scraping.adapters.platform_catalog import find_platform_config

_STAKEFISH_WORKABLE_URL = "https://stakefish.workable.com"


class StakefishAdapter(ConfiguredPlatformAdapter):
    """Adapter for stake.fish careers (https://stake.fish/company/jobs).

    stake.fish lists jobs on stakefish.workable.com.  list_jobs redirects there;
    the Workable-backed ConfiguredPlatformAdapter handles open_application and submit.
    """

    def __init__(self) -> None:
        workable = find_platform_config(_STAKEFISH_WORKABLE_URL)
        if workable is None:
            raise RuntimeError("Workable platform config missing; cannot instantiate StakefishAdapter")
        super().__init__(workable)

    async def list_jobs(self, page, career_url: str) -> list[JobListing]:
        return await super().list_jobs(page, _STAKEFISH_WORKABLE_URL)
