import asyncio
import json
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
            
            let current = el;
            while (current) {
                const keys = Object.keys(current).filter(k => k.startsWith('__reactProps'));
                if (keys.length > 0) {
                    const props = current[keys[0]];
                    // Try to find options in props
                    try {
                        let found = [];
                        JSON.stringify(props, (key, value) => {
                            if (value && Array.isArray(value) && value.length > 0 && typeof value[0] === 'object' && 'label' in value[0] && 'value' in value[0]) {
                                found.push(value.map(v => v.label));
                            }
                            return value;
                        });
                        if (found.length > 0) return found;
                    } catch(e) {}
                }
                current = current.parentElement;
            }
            return 'No options found';
        }
        """
        res = await page.evaluate(script)
        print("RESULT:", res)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
