from __future__ import annotations

import asyncio
from contextlib import suppress
import os
import random
from pathlib import Path
import subprocess
import time
from urllib.parse import urlparse, urlsplit, urlunsplit

import structlog

from backend import config

log = structlog.get_logger()


USER_AGENTS = [
    # Chrome on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    # Chrome on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    # Chrome on Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Edge on Windows (common, appears legitimate)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    # Safari on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

# Realistic viewport dimensions to choose from
_VIEWPORTS = [
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1920, "height": 1080},
    {"width": 1280, "height": 800},
    {"width": 1360, "height": 768},
    {"width": 1600, "height": 900},
    {"width": 1280, "height": 720},
]

_DOMAIN_LAST_ACCESS: dict[str, float] = {}
_DOMAIN_LOCK = asyncio.Lock()


async def apply_stealth(target) -> None:
    """Apply playwright-stealth to a BrowserContext or Page, best-effort."""
    try:
        from playwright_stealth import Stealth

        await Stealth().apply_stealth_async(target)
    except Exception as exc:
        log.warning("playwright_stealth_apply_failed", error=str(exc))
    # Always inject our custom fingerprint spoofing on top of playwright-stealth
    await _inject_fingerprint_spoofing(target)


async def _inject_fingerprint_spoofing(target) -> None:
    """Inject JS overrides to mask automation fingerprints.

    Operates at stealth_level 1+ (basic navigator overrides) and 2
    (adds WebGL, canvas noise, plugin spoofing).
    """
    level = config.BROWSER_STEALTH_LEVEL
    if level < 1:
        return

    # Level 1: core navigator property overrides
    level1_script = """
    // Override navigator.webdriver
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // Override navigator.languages
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en', 'es'],
    });

    // Override navigator.platform to match UA
    Object.defineProperty(navigator, 'platform', {
        get: () => 'MacIntel',
    });

    // Override navigator.hardwareConcurrency
    Object.defineProperty(navigator, 'hardwareConcurrency', {
        get: () => 8,
    });

    // Override navigator.deviceMemory
    Object.defineProperty(navigator, 'deviceMemory', {
        get: () => 8,
    });

    // Override permissions query for notifications
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications' ?
        Promise.resolve({ state: Notification.permission }) :
        originalQuery(parameters)
    );
    """

    # Level 2: WebGL vendor/renderer spoofing + canvas noise
    level2_script = """
    // Spoof WebGL vendor and renderer
    const getParameterOrig = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(parameter) {
        if (parameter === 37445) return 'Intel Inc.';
        if (parameter === 37446) return 'Intel Iris OpenGL Engine';
        return getParameterOrig.call(this, parameter);
    };
    const getParameterOrig2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function(parameter) {
        if (parameter === 37445) return 'Intel Inc.';
        if (parameter === 37446) return 'Intel Iris OpenGL Engine';
        return getParameterOrig2.call(this, parameter);
    };

    // Add subtle canvas noise to defeat canvas fingerprinting
    const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(type) {
        if (type === 'image/png' || type === undefined) {
            const ctx = this.getContext('2d');
            if (ctx) {
                const imgData = ctx.getImageData(0, 0, this.width, this.height);
                for (let i = 0; i < imgData.data.length; i += 4) {
                    imgData.data[i] = imgData.data[i] ^ (Math.random() > 0.5 ? 1 : 0);
                }
                ctx.putImageData(imgData, 0, 0);
            }
        }
        return origToDataURL.apply(this, arguments);
    };

    // Spoof navigator.plugins to look like a normal browser
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const arr = [{
                name: 'Chrome PDF Plugin',
                description: 'Portable Document Format',
                filename: 'internal-pdf-viewer',
                length: 1,
            }, {
                name: 'Chrome PDF Viewer',
                description: '',
                filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai',
                length: 1,
            }, {
                name: 'Native Client',
                description: '',
                filename: 'internal-nacl-plugin',
                length: 2,
            }];
            arr.refresh = () => {};
            return arr;
        },
    });
    """

    script = level1_script
    if level >= 2:
        script += level2_script

    try:
        # BrowserContext has add_init_script; Page has it too
        if hasattr(target, 'add_init_script'):
            await target.add_init_script(script)
        else:
            log.debug("fingerprint_spoofing_no_init_script_method", target_type=type(target).__name__)
    except Exception as exc:
        log.warning("fingerprint_spoofing_inject_failed", error=str(exc))


async def human_delay(min_seconds: float | None = None, max_seconds: float | None = None) -> None:
    if _delay_disabled():
        return
    low = config.BROWSER_HUMAN_DELAY_MIN_SECONDS if min_seconds is None else min_seconds
    high = config.BROWSER_HUMAN_DELAY_MAX_SECONDS if max_seconds is None else max_seconds
    await asyncio.sleep(random.uniform(low, max(high, low)))


async def human_click(page, locator, *, timeout: int = 5000) -> None:
    """Click an element with human-like mouse movement using Bézier curves.

    Instead of instantly teleporting the mouse, this generates a smooth curved
    path to the target element with randomized speed, inspired by the
    Auto_job_applier_linkedIn smooth_scroll pattern.
    """
    try:
        box = await locator.bounding_box(timeout=timeout)
    except Exception:
        # Fallback to normal click if we can't get bounding box
        await locator.click(timeout=timeout)
        return

    if box is None:
        await locator.click(timeout=timeout)
        return

    # Target a random point within the element (not dead center)
    target_x = box["x"] + box["width"] * random.uniform(0.25, 0.75)
    target_y = box["y"] + box["height"] * random.uniform(0.25, 0.75)

    # Get current mouse position (assume center of viewport if unknown)
    try:
        vp = page.viewport_size or {"width": 1360, "height": 920}
        start_x = random.uniform(vp["width"] * 0.3, vp["width"] * 0.7)
        start_y = random.uniform(vp["height"] * 0.3, vp["height"] * 0.7)
    except Exception:
        start_x, start_y = 680.0, 460.0

    # Generate Bézier curve control points
    cp1_x = start_x + (target_x - start_x) * random.uniform(0.2, 0.5) + random.uniform(-50, 50)
    cp1_y = start_y + (target_y - start_y) * random.uniform(0.0, 0.3) + random.uniform(-30, 30)
    cp2_x = start_x + (target_x - start_x) * random.uniform(0.5, 0.8) + random.uniform(-30, 30)
    cp2_y = start_y + (target_y - start_y) * random.uniform(0.7, 1.0) + random.uniform(-20, 20)

    # Move along the curve in steps
    steps = random.randint(15, 30)
    for i in range(steps + 1):
        t = i / steps
        # Cubic Bézier formula
        x = (1 - t) ** 3 * start_x + 3 * (1 - t) ** 2 * t * cp1_x + 3 * (1 - t) * t ** 2 * cp2_x + t ** 3 * target_x
        y = (1 - t) ** 3 * start_y + 3 * (1 - t) ** 2 * t * cp1_y + 3 * (1 - t) * t ** 2 * cp2_y + t ** 3 * target_y
        await page.mouse.move(x, y)
        # Randomized delay between steps to simulate human speed variation
        await asyncio.sleep(random.uniform(0.005, 0.02))

    # Small pause before clicking (human reaction time)
    await asyncio.sleep(random.uniform(0.05, 0.15))
    await page.mouse.click(target_x, target_y)


async def human_type(page, locator, text: str, *, clear_first: bool = True) -> None:
    """Type text with randomized inter-key delays to simulate human typing."""
    if clear_first:
        try:
            await locator.fill("")
        except Exception:
            pass
    for char in text:
        await locator.press(char)
        # Variable typing speed: faster for common chars, slower for specials
        if char in " .,;:":
            await asyncio.sleep(random.uniform(0.03, 0.08))
        else:
            await asyncio.sleep(random.uniform(0.02, 0.06))


async def pace_domain(url: str) -> None:
    domain = urlparse(url).hostname or "unknown"
    min_seconds = _domain_min_seconds(domain)
    if min_seconds <= 0:
        return
    async with _DOMAIN_LOCK:
        now = time.monotonic()
        wait = min_seconds - (now - _DOMAIN_LAST_ACCESS.get(domain, 0.0))
        if wait > 0:
            await asyncio.sleep(wait)
        _DOMAIN_LAST_ACCESS[domain] = time.monotonic()


async def goto_with_pacing(page, url: str, *, timeout: int = 30000, wait_until: str = "domcontentloaded") -> None:
    current_url = _normalize_url_for_compare(getattr(page, "url", None))
    target_url = _normalize_url_for_compare(url)
    if current_url and target_url and current_url == target_url:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=min(timeout, 5000))
        except Exception:
            pass
        if config.BROWSER_POST_NAVIGATION_WAIT_MS and not config.BROWSER_TEST_MODE:
            await page.wait_for_timeout(500)
        return

    await pace_domain(url)
    response = None
    for attempt in range(1, config.MAX_RETRY_BUDGET + 1):
        try:
            response = await page.goto(url, wait_until=wait_until, timeout=timeout)
            break
        except Exception:
            if attempt >= config.MAX_RETRY_BUDGET:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=min(timeout, 15000))
                break
            await asyncio.sleep(min(0.5 * attempt, 2.0))
    await _honor_retry_after(response)
    if config.BROWSER_POST_NAVIGATION_WAIT_MS and not config.BROWSER_TEST_MODE:
        await page.wait_for_timeout(config.BROWSER_POST_NAVIGATION_WAIT_MS)


def _delay_disabled() -> bool:
    return bool(config.BROWSER_TEST_MODE or os.environ.get("PYTEST_CURRENT_TEST"))


class Browser:
    def __init__(self, playwright=None, browser=None, init_error: str | None = None):
        self.playwright = playwright
        self.browser = browser
        self.init_error = init_error
        # Ephemeral contexts we created for new_page; tracked so we can close them.
        # Persistent contexts (launch_persistent_context) are the "browser" itself and
        # don't need separate tracking.
        self._contexts: list = []
        self._last_page = None
        self._lock = asyncio.Lock()
        self._active_page = None
        self._last_healthcheck = 0.0

    @classmethod
    async def create(cls):
        try:
            if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("JOBPILOT_USE_REAL_BROWSER") != "1":
                return cls(init_error="browser startup skipped during tests")
            import sys
            import shutil
            if sys.platform != "win32":
                profile_dir = str(config.BROWSER_USER_DATA_DIR.absolute())
                try:
                    subprocess.run(["pkill", "-9", "-f", f"--user-data-dir={profile_dir}"], check=False, capture_output=True)
                except Exception:
                    pass
                try:
                    for f in Path(profile_dir).glob("Singleton*"):
                        if f.is_symlink() or f.is_file():
                            f.unlink(missing_ok=True)
                        elif f.is_dir():
                            shutil.rmtree(f, ignore_errors=True)
                except Exception:
                    pass

            from playwright.async_api import async_playwright

            playwright = await async_playwright().start()
            if config.BROWSER_PERSISTENT:
                config.BROWSER_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
                launch_options = build_launch_options(persistent=True)
                context = None
                chrome_channel = _preferred_chrome_channel()
                if chrome_channel is not None:
                    try:
                        context = await playwright.chromium.launch_persistent_context(
                            str(config.BROWSER_USER_DATA_DIR),
                            channel=chrome_channel,
                            **launch_options,
                        )
                    except Exception:
                        log.warning(
                            "browser_launch_persistent_channel_failed",
                            channel=chrome_channel,
                            user_data_dir=str(config.BROWSER_USER_DATA_DIR),
                        )
                        context = None
                if context is None:
                    context = await playwright.chromium.launch_persistent_context(
                        str(config.BROWSER_USER_DATA_DIR),
                        **launch_options,
                    )
                await apply_stealth(context)
                return cls(playwright, context)
            launch_options = build_launch_options(persistent=False)
            browser = None
            chrome_channel = _preferred_chrome_channel()
            if chrome_channel is not None:
                try:
                    browser = await playwright.chromium.launch(channel=chrome_channel, **launch_options)
                except Exception:
                    log.warning("browser_launch_channel_failed", channel=chrome_channel)
                    browser = None
            if browser is None:
                browser = await playwright.chromium.launch(**launch_options)
            return cls(playwright, browser)
        except Exception as exc:
            log.exception("browser_create_failed", error=str(exc))
            return cls(init_error=str(exc))

    @classmethod
    def lazy(cls):
        return cls()

    async def _ensure_started_locked(self) -> None:
        if self.browser is not None:
            return
        if self.init_error is not None:
            return
        started = await type(self).create()
        self.playwright = started.playwright
        self.browser = started.browser
        self.init_error = started.init_error
        self._contexts = started._contexts
        self._last_page = started._last_page
        self._active_page = started._active_page
        self._last_healthcheck = started._last_healthcheck

    async def new_page(self):
        async with self._lock:
            for attempt in range(2):
                await self._ensure_started_locked()
                if self.browser is None:
                    reason = self.init_error or "browser_not_initialized"
                    log.error("browser_new_page_unavailable", reason=reason)
                    raise RuntimeError(f"Browser is not available: {reason}")
                try:
                    return await self._new_page_from_started_browser_locked()
                except Exception as exc:
                    if attempt == 1 or not _is_browser_closed_error(exc):
                        raise
                    log.warning("browser_context_stale_restarting", error=str(exc))
                    await self._reset_started_locked()
            raise RuntimeError("Browser is not available: browser_restart_failed")

    async def _new_page_from_started_browser_locked(self):
        # Persistent context ("BrowserContext"): it has `new_page` directly.
        # Reuse a single visible page so the app does not accumulate tabs/windows
        # across retries and restarts.
        if hasattr(self.browser, "new_context"):
            viewport = _pick_viewport()
            context = await self.browser.new_context(
                user_agent=_pick_user_agent(),
                viewport=viewport,
                locale=config.BROWSER_LOCALE,
                timezone_id=config.BROWSER_TIMEZONE,
            )
            await apply_stealth(context)
            self._contexts.append(context)
            page = await context.new_page()
            await apply_stealth(page)
            self._last_page = page
            self._active_page = page
            return page
        page = await self._single_persistent_page()
        await apply_stealth(page)
        self._last_page = page
        self._active_page = page
        return page

    async def _reset_started_locked(self) -> None:
        for context in list(self._contexts):
            with suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(context.close(), timeout=5.0)
        self._contexts.clear()
        if self.browser is not None:
            with suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(self.browser.close(), timeout=5.0)
        if self.playwright is not None:
            with suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(self.playwright.stop(), timeout=5.0)
        self.playwright = None
        self.browser = None
        self.init_error = None
        self._last_page = None
        self._active_page = None

    async def focus_page(self, page=None):
        should_create_page = False
        async with self._lock:
            target = page or self._last_page
            if target is None and self.browser is not None and not hasattr(self.browser, "new_context"):
                try:
                    target = await self._single_persistent_page()
                except Exception:
                    target = None
            if target is None and self.browser is not None:
                should_create_page = True
        if target is None and should_create_page:
            try:
                target = await self.new_page()
            except Exception:
                target = None
        if target is None:
            return {"ok": False, "url": None}
        try:
            await target.bring_to_front()
        except Exception:
            return {"ok": False, "url": getattr(target, "url", None)}
        self._last_page = target
        self._active_page = target
        return {"ok": True, "url": getattr(target, "url", None)}

    async def ensure_healthy_page(self, page=None):
        """Ensure the active page is alive and responsive.

        Checks for closed pages, crashed pages (JS eval fails), and
        pages stuck in navigation. Returns a healthy page or creates a new one.
        """
        target = page or self._active_page or self._last_page
        now = time.monotonic()
        if target is None:
            return target
        if hasattr(target, "is_closed") and target.is_closed():
            log.warning("page_health_closed", url=getattr(target, "url", None))
            return await self.new_page()
        if (now - self._last_healthcheck) < config.BROWSER_HEARTBEAT_SECONDS:
            return target
        self._last_healthcheck = now
        try:
            # Crash detection: try a trivial JS evaluation
            await target.evaluate("1 + 1")
            return target
        except Exception as exc:
            log.warning("page_health_unresponsive", error=str(exc), url=getattr(target, "url", None))
            return await self.new_page()

    async def cleanup_stale_tabs(self) -> int:
        """Close all tabs/pages except the most recent active one.

        Prevents resource exhaustion from accumulated tabs across retries.
        Returns the number of tabs closed.
        """
        closed = 0
        try:
            pages = list(getattr(self.browser, "pages", []) or [])
        except Exception:
            return 0
        primary = self._active_page or self._last_page
        for page in pages:
            if page is primary:
                continue
            try:
                if not page.is_closed():
                    await page.close()
                    closed += 1
            except Exception:
                pass
        if closed:
            log.info("stale_tabs_cleaned", count=closed)
        return closed

    async def open_in_default_browser(self, url: str | None) -> dict:
        if not url:
            return {"ok": False, "url": None}
        try:
            subprocess.run(["open", url], check=False)
            return {"ok": True, "url": url}
        except Exception:
            return {"ok": False, "url": url}

    async def _single_persistent_page(self):
        try:
            pages = list(getattr(self.browser, "pages", []) or [])
        except Exception:
            pages = []
        live_pages = []
        for candidate in pages:
            try:
                if not candidate.is_closed():
                    live_pages.append(candidate)
            except Exception:
                live_pages.append(candidate)
        primary = _preferred_live_page(live_pages)
        extras = [page for page in live_pages if page is not primary] if primary is not None else []
        for extra in extras:
            with suppress(Exception):
                await extra.close()
        if primary is None:
            primary = await self.browser.new_page()
        return primary

    async def close(self) -> None:
        for context in list(self._contexts):
            with suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(context.close(), timeout=5.0)
        self._contexts.clear()
        self._active_page = None
        self._last_page = None
        if self.browser is not None:
            with suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(self.browser.close(), timeout=5.0)
            self.browser = None
        if self.playwright is not None:
            with suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(self.playwright.stop(), timeout=5.0)
            self.playwright = None


def _preferred_chrome_channel() -> str | None:
    """Prefer the installed Google Chrome app so Live mode matches the real browser window."""
    if Path("/Applications/Google Chrome.app").exists():
        return "chrome"
    return None


def _is_browser_closed_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "browser has been closed",
            "context has been closed",
            "target closed",
            "target page, context or browser has been closed",
        )
    )


def build_launch_options(*, persistent: bool) -> dict:
    """Build Chromium launch options with anti-detection args.

    Stealth arg list inspired by AIHawk's chrome_browser_options() and
    Job-Hunter's headless configuration.
    """
    stealth_args = [
        "--disable-blink-features=AutomationControlled",
    ]
    if config.BROWSER_STEALTH_LEVEL >= 1:
        stealth_args.extend([
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-translate",
            "--disable-popup-blocking",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-logging",
            "--disable-autofill",
        ])
    if config.BROWSER_STEALTH_LEVEL >= 2:
        stealth_args.extend([
            "--disable-extensions",
            "--disable-plugins",
            "--disable-cache",
            "--disable-dev-shm-usage",
            "--disable-animations",
            "--ignore-certificate-errors",
            "--allow-file-access-from-files",
        ])

    options = {
        "headless": config.BROWSER_HEADLESS,
        "ignore_default_args": ["--enable-automation"],
        "args": stealth_args,
    }
    if persistent:
        viewport = _pick_viewport()
        options.update(
            {
                "viewport": viewport,
                "user_agent": _pick_user_agent(),
                "locale": config.BROWSER_LOCALE,
                "timezone_id": config.BROWSER_TIMEZONE,
            }
        )
    return options


def _pick_viewport() -> dict:
    """Pick a realistic viewport, optionally with slight randomization."""
    base = random.choice(_VIEWPORTS)
    if not config.BROWSER_VIEWPORT_RANDOMIZE:
        return dict(base)
    return {
        "width": base["width"] + random.randint(-20, 20),
        "height": base["height"] + random.randint(-10, 10),
    }


def _pick_user_agent() -> str:
    if config.BROWSER_TEST_MODE:
        return USER_AGENTS[0]
    return random.choice(USER_AGENTS)


def _domain_min_seconds(domain: str) -> float:
    raw = getattr(config, "BROWSER_DOMAIN_PACING_OVERRIDES", "") or ""
    overrides: dict[str, float] = {}
    for item in raw.split(","):
        if ":" not in item:
            continue
        name, value = item.split(":", 1)
        try:
            overrides[name.strip().lower()] = float(value.strip())
        except ValueError:
            continue
    return overrides.get(domain.lower(), config.BROWSER_DOMAIN_MIN_SECONDS)


async def _honor_retry_after(response) -> None:
    if response is None:
        return
    headers = {}
    try:
        headers = await response.all_headers()
    except Exception:
        try:
            headers = response.headers or {}
        except Exception:
            headers = {}
    retry_after = headers.get("retry-after") or headers.get("Retry-After")
    if not retry_after:
        return
    try:
        delay = min(float(retry_after), 30.0)
    except ValueError:
        return
    if delay > 0:
        await asyncio.sleep(delay)


def _normalize_url_for_compare(url: str | None) -> str:
    if not url:
        return ""
    parts = urlsplit(url)
    normalized_path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme, parts.netloc, normalized_path, parts.query, ""))


def _preferred_live_page(pages: list) -> object | None:
    if not pages:
        return None
    non_blank = [page for page in pages if _page_url(page) not in {"", "about:blank"}]
    if non_blank:
        return non_blank[-1]
    return pages[-1]


def _page_url(page) -> str:
    return _normalize_url_for_compare(getattr(page, "url", None))
