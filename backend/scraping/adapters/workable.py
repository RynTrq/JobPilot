from __future__ import annotations

import asyncio
import time

import structlog

from backend.scraping.adapters.base import SubmitResult
from backend.scraping.adapters.configured import ConfiguredPlatformAdapter
from backend.scraping.adapters.platform_catalog import find_platform_config

log = structlog.get_logger()


# Cloudflare Turnstile site key embedded in apply.workable.com pages
# (extracted from `window.careers.config.turnstileWidgetSiteKey`).  The form
# rejects the submit POST when no token is present in the
# `cf-turnstile-response` hidden input, even when
# `wjb_acp_turnstile_captcha_rejection_enabled` reports `false`.
_WORKABLE_TURNSTILE_SITE_KEY = "0x4AAAAAAAVY8hH3nz6RxaK0"

# Phrases used by Workable's UI when the apply request is rejected.
_WORKABLE_BOT_BANNER_TOKENS = (
    "verification failed",
    "something went wrong",
    "we are working on this",
    "please try again later",
)

# Phrases that indicate the application has been received successfully.
_WORKABLE_CONFIRMATION_TOKENS = (
    "thank you for applying",
    "application submitted",
    "we received your application",
    "your application has been sent",
    "thanks for applying",
)

# Field keywords (label / name / placeholder / id) that identify the URL-style
# inputs we want to hard-clear before re-typing.  Workable's React form keeps
# stale values in these inputs across re-fills, which would otherwise cause
# `.type()` to append and produce concatenated URLs in the rendered value.
_WORKABLE_URL_FIELD_TOKENS = (
    "linkedin",
    "github",
    "twitter",
    "x.com",
    "portfolio",
    "website",
    "personal site",
    "personal website",
    "other link",
    "additional link",
    "url",
    "profile",
)


class WorkableAdapter(ConfiguredPlatformAdapter):
    """Adapter for Workable-hosted job boards (apply.workable.com/<company>/).

    Workable is a React SPA hosted behind Cloudflare with the following
    bot-detection layers we have to interoperate with:

    * **Cloudflare bot management** — an invisible iframe at
      ``/cdn-cgi/challenge-platform/scripts/jsd/main.js`` runs JS-based
      fingerprinting on every page load.  Failing this challenge silently
      rejects the submit POST.
    * **Cloudflare Turnstile** (always-on as of late-2024) — every apply page
      embeds a managed-mode Turnstile widget whose token must be present in
      ``input[name="cf-turnstile-response"]`` before the submit POST will
      succeed.  Turnstile renders invisibly when the bot-management challenge
      passed, but flips to a visible "Verification failed" badge when it
      didn't, after which submit yields a generic
      "Something went wrong. We are working on this, please try again later."
      banner with no validation errors.
    * **GDPR cookie consent modal** — a backdrop intercepts pointer events
      until it is dismissed (we always *decline* to avoid extra tracking
      cookies that further harden the bot challenge).

    The adapter overrides three things on top of
    :class:`ConfiguredPlatformAdapter`:

    1. ``_goto`` — wait for ``networkidle`` so the React form mounts, then
       dismiss the cookie consent modal so subsequent clicks aren't swallowed.
    2. ``fill_field`` — for URL-style fields, hard-clear the underlying
       React-controlled ``HTMLInputElement`` via the native value setter and
       a synthetic ``input``/``change`` event *before* delegating to the
       generic browser-form fill, so retries cannot accumulate concatenated
       values like ``https://github.com/Xhttps://example.com``.
    3. ``submit`` — block the submit until the Turnstile token has been
       minted, then translate the "verification failed / something went
       wrong" banner into an actionable :class:`SubmitResult` error so the
       orchestrator hands off to a human instead of marking the job as done.
    """

    TURNSTILE_SITE_KEY = _WORKABLE_TURNSTILE_SITE_KEY

    def __init__(self) -> None:
        platform = find_platform_config("https://workable.com/")
        if platform is None:
            raise RuntimeError("Workable platform config missing; cannot instantiate WorkableAdapter")
        super().__init__(platform)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    async def _goto(self, page, url: str) -> None:
        await super()._goto(page, url)
        try:
            await page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            try:
                await page.wait_for_timeout(1500)
            except Exception:
                pass
        log.debug("workable_goto_settled", url=url)
        await self._dismiss_cookie_consent(page)

    async def _dismiss_cookie_consent(self, page) -> None:
        """Dismiss Workable's cookie-consent modal if present.

        The modal + backdrop intercept ALL pointer events on the page,
        blocking GDPR checkbox clicks, submit button clicks, etc.
        We prefer to decline cookies so no unnecessary tracking is accepted.
        """
        for selector in (
            "[data-ui='cookie-consent-decline']",
            "[data-ui='cookie-consent-accept']",
            # Fallback: any visible button inside the consent dialog
            "[data-ui='cookie-consent'] button",
        ):
            try:
                btn = page.locator(selector).first
                if await btn.count() and await btn.is_visible(timeout=2000):
                    await btn.click(timeout=5000)
                    log.debug(
                        "workable_cookie_consent_dismissed",
                        selector=selector,
                        url=getattr(page, "url", ""),
                    )
                    try:
                        await page.locator("[data-ui='cookie-consent']").wait_for(state="hidden", timeout=3000)
                    except Exception:
                        pass
                    return
            except Exception:
                continue

    # ------------------------------------------------------------------
    # Field fill — hard-clear URL inputs before delegating
    # ------------------------------------------------------------------

    async def fill_field(self, page, field, value) -> None:
        if self._looks_like_url_field(field):
            await self._hard_clear_input(page, field)
        await super().fill_field(page, field, value)

    @staticmethod
    def _looks_like_url_field(field) -> bool:
        ftype = (getattr(field, "field_type", "") or "").lower()
        if ftype not in {"text", "url", ""}:
            return False
        text = " ".join(
            str(part or "")
            for part in (
                getattr(field, "label_text", ""),
                getattr(field, "name", ""),
                getattr(field, "placeholder", ""),
                getattr(field, "aria_label", ""),
                getattr(field, "element_id", ""),
            )
        ).lower()
        if not text.strip():
            return False
        return any(token in text for token in _WORKABLE_URL_FIELD_TOKENS)

    async def _hard_clear_input(self, page, field) -> None:
        """Force-empty a React-controlled text input.

        ``locator.fill('')`` sets ``el.value`` directly, which a controlled
        React input may revert on the next render.  Using the native setter
        plus a bubbling ``input`` event guarantees the React state machine
        observes the empty value, so a subsequent ``.type()`` doesn't append
        to a leftover URL.
        """
        selector = getattr(field, "selector", None)
        if not selector:
            return
        try:
            locator = page.locator(selector).first
            if not await locator.count():
                return
            await locator.evaluate(
                """(el) => {
                    if (!el) return;
                    const proto = (el.tagName === 'TEXTAREA')
                        ? window.HTMLTextAreaElement.prototype
                        : window.HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value');
                    if (setter && setter.set) {
                        setter.set.call(el, '');
                    } else if ('value' in el) {
                        el.value = '';
                    }
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    if (el.blur) el.blur();
                }"""
            )
        except Exception as exc:
            log.debug(
                "workable_hard_clear_failed",
                selector=selector,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Submit — guard with Turnstile + translate bot-detection banners
    # ------------------------------------------------------------------

    async def submit(self, page) -> SubmitResult:
        # 1) Make sure the page actually has a Turnstile token before we POST.
        #    Without this, Workable's apply endpoint silently returns an
        #    error and the page renders a generic red banner.
        widget_present = await self._turnstile_widget_present(page)
        if widget_present:
            token_ready = await self._wait_for_turnstile_token(page, timeout_ms=20000)
            if not token_ready or await self._turnstile_failed(page):
                log.warning(
                    "workable_turnstile_blocking_submit",
                    url=getattr(page, "url", ""),
                    site_key=self.TURNSTILE_SITE_KEY,
                )
                return SubmitResult(
                    ok=False,
                    error=(
                        "workable_turnstile_failed: Cloudflare Turnstile did not mint a "
                        "verification token (site key "
                        f"{self.TURNSTILE_SITE_KEY}).  Open the page manually, complete "
                        "the 'Verify you are human' challenge, then resubmit."
                    ),
                )

        # 2) Delegate to the generic flow.
        result = await super().submit(page)

        # 3) Workable surfaces both "we couldn't verify you" and post-submit
        #    failures in the same red banner with no validation errors. Treat
        #    the bot-detection banner as an actionable error so the
        #    orchestrator can escalate to manual takeover instead of marking
        #    the application as done.
        if not result.ok:
            banner = await self._read_error_banner(page)
            if banner and self._looks_like_bot_banner(banner):
                log.warning(
                    "workable_submit_blocked_by_bot_banner",
                    url=getattr(page, "url", ""),
                    banner=banner[:300],
                )
                return SubmitResult(
                    ok=False,
                    error=(
                        "workable_submit_blocked_by_bot_detection: "
                        + banner.strip()
                        + " (Cloudflare Turnstile site key "
                        + self.TURNSTILE_SITE_KEY
                        + ")"
                    ),
                )
        return result

    # ------------------------------------------------------------------
    # Turnstile helpers
    # ------------------------------------------------------------------

    async def _turnstile_widget_present(self, page) -> bool:
        """Return True if a Cloudflare Turnstile widget is mounted on the page.

        Matches both the public CSS class (`.cf-turnstile`) and the
        challenge iframe used in managed mode.  We deliberately check
        ``query_selector`` (not ``is_visible``) — when Turnstile passes
        silently it stays invisible, but the widget container remains in
        the DOM and the hidden token input is what we wait on.
        """
        for selector in (
            "[data-sitekey]",
            ".cf-turnstile",
            "[class*='cf-turnstile']",
            "input[name='cf-turnstile-response']",
            "iframe[src*='challenges.cloudflare.com']",
        ):
            try:
                element = await page.query_selector(selector)
                if element is not None:
                    return True
            except Exception as exc:
                log.debug("workable_turnstile_probe_failed", selector=selector, error=str(exc))
                continue
        return False

    async def _wait_for_turnstile_token(self, page, *, timeout_ms: int) -> bool:
        """Block until ``cf-turnstile-response`` has a non-empty value or we time out."""
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            try:
                token = await page.evaluate(
                    """() => {
                        const el = document.querySelector("input[name='cf-turnstile-response']");
                        return (el && el.value) || '';
                    }"""
                )
                if token:
                    log.debug("workable_turnstile_token_minted", token_length=len(token))
                    return True
            except Exception as exc:
                log.debug("workable_turnstile_token_probe_failed", error=str(exc))
            await asyncio.sleep(0.5)
        log.warning("workable_turnstile_token_timeout", timeout_ms=timeout_ms)
        return False

    async def _turnstile_failed(self, page) -> bool:
        """Return True if the visible Turnstile widget is in the failed state."""
        try:
            text = await page.evaluate(
                """() => {
                    const root = document.querySelector('.cf-turnstile, [class*="cf-turnstile"]');
                    return ((root && (root.innerText || root.textContent)) || '').toLowerCase();
                }"""
            )
        except Exception:
            return False
        if not text:
            return False
        # "verify you are human" alone is the success-pending state; only treat
        # the explicit failure copy as a hard fail so we don't false-positive
        # on the "Verifying…" transient state.
        return any(marker in text for marker in ("verification failed", "could not verify", "try again"))

    async def _read_error_banner(self, page) -> str:
        """Return the first visible error/alert banner text on the page.

        Workable's `<div class="component-Alert ...">` element (and the older
        `[role="alert"]` variant) surface every form-level failure here.
        """
        try:
            text = await page.evaluate(
                """() => {
                    const matches = Array.from(document.querySelectorAll(
                        '[class*="error"], [class*="alert"], [role="alert"], [data-ui="application-form-error"]'
                    ));
                    for (const node of matches) {
                        if (!node || !node.isConnected) continue;
                        const style = window.getComputedStyle(node);
                        if (style.visibility === 'hidden' || style.display === 'none') continue;
                        const value = (node.innerText || node.textContent || '').trim();
                        if (value) return value;
                    }
                    return '';
                }"""
            )
            return text or ""
        except Exception:
            return ""

    @staticmethod
    def _looks_like_bot_banner(text: str) -> bool:
        lowered = (text or "").lower()
        return any(token in lowered for token in _WORKABLE_BOT_BANNER_TOKENS)

    @staticmethod
    def _looks_like_confirmation(text: str) -> bool:
        lowered = (text or "").lower()
        return any(token in lowered for token in _WORKABLE_CONFIRMATION_TOKENS)
