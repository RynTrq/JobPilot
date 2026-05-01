from __future__ import annotations

from urllib.parse import urlparse

from backend.scraping.adapters.ashby import AshbyAdapter
from backend.scraping.adapters.browser_form import BrowserFormAdapter as BrowserFormAdapter
from backend.scraping.adapters.configured import ConfiguredPlatformAdapter as ConfiguredPlatformAdapter
from backend.scraping.adapters.configured import adapter_for_platform
from backend.scraping.adapters.dover import DoverAdapter
from backend.scraping.adapters.greenhouse import GreenhouseAdapter
from backend.scraping.adapters.icims import IcimsAdapter
from backend.scraping.adapters.lever import LeverAdapter
from backend.scraping.adapters.platform_catalog import find_platform_config
from backend.scraping.adapters.platform_catalog import platform_count as platform_count
from backend.scraping.adapters.smartrecruiters import SmartRecruitersAdapter
from backend.scraping.adapters.stakefish import StakefishAdapter
from backend.scraping.adapters.workable import WorkableAdapter
from backend.scraping.adapters.workday import WorkdayAdapter


class NoAdapterFoundError(LookupError):
    def __init__(self, url: str) -> None:
        self.url = url
        host = urlparse(url).hostname or url
        super().__init__(
            f"No adapter found for {host}. Add a site-specific adapter or platform catalog entry before running this URL."
        )


def dispatch_adapter(url: str):
    host = urlparse(url).hostname or ""
    if "app.dover.com" in host:
        return DoverAdapter()
    if "greenhouse.io" in host:
        return GreenhouseAdapter()
    if "lever.co" in host:
        return LeverAdapter()
    if "ashbyhq.com" in host:
        return AshbyAdapter()
    if "smartrecruiters.com" in host:
        return SmartRecruitersAdapter()
    if "icims.com" in host:
        return IcimsAdapter()
    if "myworkdayjobs.com" in host or "workdayjobs.com" in host:
        return WorkdayAdapter()
    if "workable.com" in host:
        return WorkableAdapter()
    if "stake.fish" in host:
        return StakefishAdapter()
    platform = find_platform_config(url)
    if platform is not None:
        return adapter_for_platform(platform)
    raise NoAdapterFoundError(url)
