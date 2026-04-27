import asyncio
from playwright.async_api import async_playwright
from backend.scraping.adapters.browser_form import BrowserFormAdapter

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://job-boards.greenhouse.io/careem/jobs/8214380002?gh_jid=8214380002")
        adapter = BrowserFormAdapter()
        # Wait for form
        await page.wait_for_selector("form", timeout=5000)
        fields = await adapter.enumerate_fields(page)
        for f in fields:
            print(f"LABEL: {f.label_text} | TYPE: {f.field_type} | TAG: {f.tag} | ROLE: {f.role} | OPTIONS: {f.options}")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
