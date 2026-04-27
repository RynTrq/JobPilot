from __future__ import annotations

from backend import config
from backend.scraping.adapters.base import SubmitResult


class Submitter:
    async def submit(self, adapter, page):
        if config.DRY_RUN:
            return SubmitResult(ok=False, error="dry run: final submit skipped")
        return await adapter.submit(page)
