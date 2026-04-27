import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://job-boards.greenhouse.io/careem/jobs/8214380002?gh_jid=8214380002")
        await page.wait_for_selector("form", timeout=5000)
        
        script = """
        () => {
            const el = document.querySelector('input[role="combobox"]');
            if (!el) return 'No combobox';
            
            // Look for React internal properties
            const props = Object.keys(el).filter(k => k.startsWith('__reactProps'));
            if (props.length === 0) return 'No react props on input';
            
            // Try parent
            const parent = el.closest('.select-shell');
            const pProps = parent ? Object.keys(parent).filter(k => k.startsWith('__reactProps')) : [];
            
            return {
                inputProps: props,
                parentProps: pProps
            };
        }
        """
        res = await page.evaluate(script)
        print("RESULT:", res)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
