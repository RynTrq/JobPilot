from __future__ import annotations

import asyncio
import random
from urllib.parse import urljoin
from urllib.parse import urlparse

import structlog

from backend import config
from backend.storage.button_memory import ButtonNameMemory

log = structlog.get_logger()

# Singleton instance for button name memory
_button_memory = ButtonNameMemory()


class NavigationError(RuntimeError):
    pass


APPLY_BUTTON_SELECTORS = [
    "button:has-text('Apply Now')",
    "button:has-text('Apply now')",
    "button:has-text('Apply for this Job')",
    "button:has-text('Apply for this Position')",
    "button:has-text('Apply for this role')",
    "button:has-text('Start Application')",
    "button:has-text('Start application')",
    "button:has-text('Begin Application')",
    "button:has-text('Apply Online')",
    "button:has-text('Apply online')",
    "button:has-text('Quick Apply')",
    "button:has-text('Easy Apply')",
    "button:has-text('Apply')",
    "a:has-text('Apply Now')",
    "a:has-text('Apply now')",
    "a:has-text('Apply for this Job')",
    "a:has-text('Apply for this role')",
    "a:has-text('Start Application')",
    "a:has-text('Start application')",
    "a:has-text('Apply Online')",
    "a:has-text('Apply online')",
    "a:has-text('Quick Apply')",
    "a:has-text('Easy Apply')",
    "a:has-text('Apply')",
    "[role='button']:has-text('Apply')",
    "[role='button']:has-text('Apply now')",
    "[role='button']:has-text('Start Application')",
    "[role='button']:has-text('Start application')",
    "[data-automation-id='applyButton']",
    "[data-qa='btn-apply']",
    "[data-testid*='apply']",
    "[data-testid*='Apply']",
    "[data-automation-id*='apply']",
    "[data-automation-id*='Apply']",
    "[class*='apply-button']",
    "[class*='applyButton']",
    "[class*='quick-apply']",
    "[class*='easy-apply']",
    "[class*='application-button']",
    "[id*='apply-btn']",
    "[id*='applyButton']",
    "[id*='apply']",
]

MANUAL_ENTRY_SELECTORS = [
    "button:has-text('Fill Out Manually')",
    "button:has-text('Fill Manually')",
    "button:has-text('Enter Manually')",
    "button:has-text('Continue manually')",
    "button:has-text('Apply manually')",
    "button:has-text('Manually')",
    "a:has-text('Fill Out Manually')",
    "a:has-text('Enter Manually')",
    "a:has-text('Continue manually')",
    "label:has-text('Fill Out Manually')",
    "label:has-text('Enter Manually')",
    "label:has-text('Continue manually')",
    "[role='radio']:has-text('Manually')",
    "[data-automation-id='manualEntry']",
    "[data-automation-id='fillManually']",
    "[data-automation-id*='manual']",
    "[data-testid*='manual']",
    "[class*='manual-application']",
    "[class*='manual']",
    "input[type='radio']",
]

NEXT_BUTTON_SELECTORS = [
    "button:has-text('Next')",
    "button:has-text('Continue')",
    "button:has-text('Save and Continue')",
    "button:has-text('Save & Continue')",
    "button:has-text('Proceed')",
    "button:has-text('Next Step')",
    "button:has-text('Continue to Next Step')",
    "button:has-text('Save and Next')",
    "button:has-text('Continue Application')",
    # Workday
    "[data-automation-id='bottom-navigation-next-button']",
    "[data-automation-id='pageFooterNextButton']",
    # iCIMS
    "[data-automation-id*='next']",
    "[data-testid*='next-button']",
    "[data-testid*='continue-button']",
    # SmartRecruiters
    "[data-test*='next']",
    "[data-test*='continue']",
    # Taleo / Oracle
    "[id*='next']",
    "[id*='continueBtn']",
    # Generic
    "input[type='submit'][value*='Next']",
    "input[type='submit'][value*='Continue']",
    "a:has-text('Next')",
    "a:has-text('Continue')",
    "[role='button']:has-text('Next')",
    "[role='button']:has-text('Continue')",
]

SUBMIT_BUTTON_SELECTORS = [
    "button:has-text('Submit Application')",
    "button:has-text('Submit')",
    "button:has-text('Send Application')",
    "button:has-text('Apply Now')",
    "button:has-text('Apply now')",
    "button:has-text('Apply')",
    "button:has-text('Complete Application')",
    "button:has-text('Finish')",
    "button:has-text('Confirm and Submit')",
    "button[onclick*='apply'], button[onclick*='submit']",
    "input[type='submit'][value*='Submit']",
    "input[type='submit'][value*='Apply']",
    "a:has-text('Apply Now')",
    "a:has-text('Apply now')",
    "a:has-text('Submit Application')",
    "a:has-text('Submit')",
    "a:has-text('Apply')",
    "[role='button']:has-text('Apply Now')",
    "[role='button']:has-text('Apply now')",
    "[role='button']:has-text('Submit')",
    "[role='button']:has-text('Apply')",
    # Workday
    "[data-automation-id='bottom-navigation-next-button']",
    "[data-automation-id='submit-application']",
    # Greenhouse
    "#submit_app",
    ".submit-btn",
    # iCIMS
    "[data-automation-id*='submit']",
    "[data-testid*='submit']",
    # SmartRecruiters
    "[data-test*='submit']",
    "[data-test*='apply']",
]

FORM_INDICATOR_SELECTORS = [
    "input[type='text']",
    "input[type='email']",
    "input[type='file']",
    "form",
]

ENTRY_METHOD_INDICATORS = [
    "linkedin",
    "apply with linkedin",
    "indeed",
    "apply with indeed",
    "manual",
    "fill manually",
    "enter manually",
    "fill out manually",
    "manually enter",
    "upload resume",
    "upload cv",
    "how would you like to apply",
    "choose how to apply",
    "start your application",
    "continue manually",
    "upload your resume",
    "use linkedin",
    "import your resume",
]

CONFIRMATION_TEXT_PATTERNS = [
    "Application submitted",
    "Thank you for applying",
    "We have received your application",
    "Application received",
    "You have successfully applied",
    "Application complete",
    # Workable-specific confirmation text
    "Your application was submitted",
    "application has been submitted",
    "Thanks for applying",
    "Great! Your application",
    "successfully submitted your application",
]

REVIEW_TEXT_PATTERNS = [
    "Review",
    "Review your application",
    "Please review",
    "Confirm your information",
    "Review and submit",
]

# Validation error selectors and patterns for post-click error recovery
VALIDATION_ERROR_SELECTORS = [
    "[class*='error']",
    "[class*='Error']",
    "[class*='invalid']",
    "[class*='Invalid']",
    "[role='alert']",
    ".field-error",
    ".form-error",
    ".validation-error",
    "[data-automation-id*='error']",
    "[data-testid*='error']",
    "[aria-invalid='true']",
]

VALIDATION_ERROR_TEXT_PATTERNS = [
    "required",
    "this field is required",
    "please fill",
    "please enter",
    "please select",
    "invalid",
    "cannot be blank",
    "must not be empty",
    "is not valid",
]

SOCIAL_ENTRY_TERMS = ("linkedin", "indeed", "monster", "glassdoor", "upload", "autofill")
NON_APPLICATION_WIDGET_MARKERS = (
    "ask anything",
    "chat",
    "chatbot",
    "create job alert",
    "get notified",
    "job alert",
    "manage alerts",
    "notified",
    "notification",
    "recommendations",
    "similar jobs",
    "sign up to receive",
    "subscribe",
)
STRONG_APPLICATION_FIELD_MARKERS = (
    "address line",
    "cover letter",
    "current company",
    "current title",
    "cv",
    "first name",
    "full name",
    "github",
    "last name",
    "legal name",
    "linkedin",
    "mailing address",
    "mobile",
    "phone",
    "portfolio",
    "resume",
    "right to work",
    "street address",
    "sponsor",
    "surname",
    "visa",
    "work authorization",
    "website",
)
APPLY_TRIGGER_KEYWORDS = (
    "apply",
    "apply now",
    "start application",
    "begin application",
    "submit application",
    "apply online",
    "easy apply",
    "quick apply",
)
MANUAL_ENTRY_POSITIVE_TERMS = (
    "manual",
    "manually",
    "fill out manually",
    "fill manually",
    "enter manually",
    "continue manually",
    "apply manually",
)


async def detect_apply_button(page) -> dict | None:
    await _wait_for_page_ready(page)
    if await _page_has_visible_form(page):
        log.info("apply_button_not_needed", url=getattr(page, "url", ""))
        return None
    if await _entry_method_visible(page):
        log.info("apply_button_not_needed_entry_method_visible", url=getattr(page, "url", ""))
        return {"element": None, "selector": "entry_method_visible", "opens_new_tab": False}

    deadline = asyncio.get_running_loop().time() + 10.0
    while asyncio.get_running_loop().time() < deadline:
        for selector in APPLY_BUTTON_SELECTORS:
            element = await page.query_selector(selector)
            if element is None:
                continue
            if not await _element_visible(element):
                continue
            opens_new_tab = await _button_opens_new_tab(page, element)
            info = {"element": element, "selector": selector, "opens_new_tab": opens_new_tab}
            log.info("apply_button_detected", selector=selector, opens_new_tab=opens_new_tab, url=getattr(page, "url", ""))
            return info
        text_probe = await _find_apply_button_by_text(page)
        if text_probe is not None:
            opens_new_tab = await _button_opens_new_tab(page, text_probe)
            info = {"element": text_probe, "selector": "text_probe", "opens_new_tab": opens_new_tab}
            log.info("apply_button_detected", selector="text_probe", opens_new_tab=opens_new_tab, url=getattr(page, "url", ""))
            return info
        if await _entry_method_visible(page):
            log.info("apply_button_not_needed_entry_method_visible", url=getattr(page, "url", ""))
            return {"element": None, "selector": "entry_method_visible", "opens_new_tab": False}
        await asyncio.sleep(0.25)

    raise NavigationError(f"No apply button or inline form found on page {getattr(page, 'url', '')}")


async def click_apply_and_get_form_page(page, browser_context, apply_button_info) -> object:
    element = apply_button_info["element"]
    if element is None:
        await handle_entry_method_selection(page)
        return page
    direct_href = await _direct_apply_href(page, element)
    if direct_href is not None:
        try:
            previous_url = getattr(page, "url", "") or ""
            await page.goto(direct_href, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception as exc:
                log.debug("apply_href_networkidle_wait_failed", url=direct_href, error=str(exc))
            form_page = page
            log.info("apply_navigated_via_href", from_url=previous_url, to_url=getattr(page, "url", direct_href))
            await handle_entry_method_selection(form_page)
            await _wait_for_page_ready(form_page)
            return form_page
        except Exception as exc:
            log.warning("apply_href_navigation_failed", url=direct_href, error=str(exc))
    if apply_button_info.get("opens_new_tab"):
        form_page = None
        try:
            async with page.expect_popup() as popup_info:
                await element.click()
            form_page = await popup_info.value
        except Exception as exc:
            log.warning("apply_popup_failed_falling_back_to_same_tab", url=getattr(page, "url", ""), error=str(exc))
            previous_url = getattr(page, "url", "") or ""
            try:
                async with page.expect_navigation(wait_until="networkidle"):
                    await element.click()
            except Exception as nav_exc:
                log.warning("apply_popup_fallback_navigation_failed", url=previous_url, error=str(nav_exc))
                try:
                    await asyncio.sleep(3.0)
                    current_url = getattr(page, "url", "") or ""
                    if not current_url or current_url == previous_url:
                        raise RuntimeError("url_did_not_change_after_apply_click")
                    log.info("apply_navigation_detected_via_url_change", from_url=previous_url, to_url=current_url)
                except Exception:
                    try:
                        await page.wait_for_load_state("networkidle", timeout=15000)
                    except Exception as fallback_exc:
                        log.warning("apply_popup_fallback_networkidle_wait_failed", url=getattr(page, "url", ""), error=str(fallback_exc))
                        await page.wait_for_load_state("domcontentloaded", timeout=10000)
            form_page = page
            log.info("apply_navigated_same_tab_after_popup_fallback", url=getattr(page, "url", ""))
        else:
            try:
                await form_page.wait_for_load_state("networkidle")
            except Exception as exc:
                log.warning("apply_popup_networkidle_wait_failed", url=getattr(form_page, "url", ""), error=str(exc))
                await form_page.wait_for_load_state("domcontentloaded")
            log.info("apply_opened_new_tab", url=getattr(form_page, "url", ""))
    else:
        previous_url = getattr(page, "url", "") or ""
        try:
            async with page.expect_navigation(wait_until="networkidle"):
                await element.click()
        except Exception as exc:
            log.warning("apply_expect_navigation_failed", url=previous_url, error=str(exc))
            try:
                await asyncio.sleep(3.0)
                current_url = getattr(page, "url", "") or ""
                if not current_url or current_url == previous_url:
                    raise RuntimeError("url_did_not_change_after_apply_click")
                log.info("apply_navigation_detected_via_url_change", from_url=previous_url, to_url=current_url)
            except Exception:
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception as fallback_exc:
                    log.warning("apply_same_tab_networkidle_wait_failed", url=getattr(page, "url", ""), error=str(fallback_exc))
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
        form_page = page
        log.info("apply_navigated_same_tab", url=getattr(page, "url", ""))

    await handle_entry_method_selection(form_page)
    await _wait_for_page_ready(form_page)
    return form_page


async def _direct_apply_href(page, element) -> str | None:
    try:
        href = (await element.get_attribute("href") or "").strip()
    except Exception:
        return None
    if not href or href.startswith("#"):
        return None
    lowered = href.lower()
    if lowered.startswith(("javascript:", "mailto:", "tel:")):
        return None
    destination = urljoin(getattr(page, "url", "") or "", href)
    parsed = urlparse(destination)
    if parsed.scheme not in {"http", "https"}:
        return None
    return destination


async def handle_entry_method_selection(page) -> None:
    if await _page_has_visible_form(page):
        log.info("entry_method_not_needed_form_visible", url=getattr(page, "url", ""))
        return
    body_text = ""
    try:
        body_text = ((await page.locator("body").inner_text()) or "").lower()
    except Exception as exc:
        log.warning("entry_method_body_read_failed", url=getattr(page, "url", ""), error=str(exc))
        body_text = ""
    if not any(indicator in body_text for indicator in ENTRY_METHOD_INDICATORS):
        log.info("entry_method_not_detected", url=getattr(page, "url", ""))
        return

    manual_choice = await _find_manual_entry_control(page)
    if manual_choice is not None:
        try:
            await manual_choice.click()
            next_button = await find_next_button(page)
            if next_button is not None:
                await next_button.click()
                await _wait_for_page_ready(page)
            log.info("entry_method_handled", method="manual", url=getattr(page, "url", ""))
            return
        except Exception as exc:
            log.warning("manual_entry_control_click_failed", url=getattr(page, "url", ""), error=str(exc))

    for selector in MANUAL_ENTRY_SELECTORS:
        try:
            if selector == "input[type='radio']":
                radios = await page.query_selector_all(selector)
                radio = await _choose_manual_radio(page, radios)
                if radio is None:
                    continue
                await radio.click()
            else:
                element = await page.query_selector(selector)
                if element is None or not await _element_visible(element):
                    continue
                await element.click()
            next_button = await find_next_button(page)
            if next_button is not None:
                await next_button.click()
                await page.wait_for_load_state("networkidle")
            log.info("entry_method_handled", method="manual", url=getattr(page, "url", ""))
            return
        except Exception as exc:
            log.warning("manual_entry_selector_failed", selector=selector, url=getattr(page, "url", ""), error=str(exc))
            continue

    raise NavigationError(f"Manual entry option was detected but could not be selected on {getattr(page, 'url', '')}")


async def wait_for_post_submit_confirmation(page, timeout_ms: int = 12000) -> bool:
    """Poll for a confirmation page after submit click on React/SPA forms.

    Workable and similar SPAs don't trigger a full navigation after submit —
    they replace page content client-side.  `networkidle` alone is unreliable
    because the SPA may keep background requests alive.  This polls every 500ms
    for up to `timeout_ms` and returns True if a confirmation is detected.
    """
    deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
    while asyncio.get_event_loop().time() < deadline:
        page_url = (getattr(page, "url", "") or "").lower()
        if any(token in page_url for token in ("/confirmation", "/success", "/thank-you", "/submitted")):
            return True
        if await _page_contains_any_text(page, CONFIRMATION_TEXT_PATTERNS):
            return True
        # Also detect Workable's "Submitting" → disappear transition:
        # once the submit button is gone and form inputs are gone, the SPA
        # transitioned away from the form.
        try:
            submit_gone = not await page.query_selector("button[type='submit']:visible, button:has-text('Submit'):visible, button:has-text('Submitting'):visible")
            inputs_gone = not await page.query_selector("input[type='text']:visible, input[type='email']:visible, textarea:visible, input[type='file']:visible")
            if submit_gone and inputs_gone:
                await asyncio.sleep(0.5)
                if await _page_contains_any_text(page, CONFIRMATION_TEXT_PATTERNS):
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return False


async def detect_page_type(page) -> str:
    page_url = (getattr(page, "url", "") or "").lower()
    if any(token in page_url for token in ("/confirmation", "/success", "/thank-you", "/submitted")):
        return "confirmation_page"
    if await _page_contains_any_text(page, CONFIRMATION_TEXT_PATTERNS):
        return "confirmation_page"
    if await _page_contains_any_text(page, REVIEW_TEXT_PATTERNS):
        submit_button = await find_submit_button(page)
        next_button = await find_next_button(page)
        if submit_button is not None and next_button is None:
            return "review_page"
    if await _has_visible_form_inputs(page):
        return "form_page"
    return "unknown"


async def find_next_button(page):
    # First check registered alternative button names from memory
    registered_alternatives = _button_memory.get_alternatives("next")
    for alt_name in registered_alternatives:
        for tag in ("button", "a", "[role='button']"):
            selector = f"{tag}:has-text('{alt_name}')"
            element = await page.query_selector(selector)
            if element is not None and await _element_visible(element) and not await _element_disabled(element):
                text = (await _element_text(element)).lower()
                if "save for later" not in text:
                    log.info("found_next_button_from_memory", alt_name=alt_name, url=getattr(page, "url", ""))
                    return element
    
    # Fall back to standard selectors
    for selector in NEXT_BUTTON_SELECTORS:
        element = await page.query_selector(selector)
        if element is None:
            continue
        if not await _element_visible(element):
            continue
        if await _element_disabled(element):
            continue
        text = (await _element_text(element)).lower()
        if "save for later" in text:
            continue
        if "submit" in text or "send application" in text:
            continue
        return element
    return None


async def find_submit_button(page):
    # First check registered alternative button names from memory
    registered_alternatives = _button_memory.get_alternatives("submit")
    for alt_name in registered_alternatives:
        for tag in ("button", "a", "[role='button']"):
            selector = f"{tag}:has-text('{alt_name}')"
            element = await page.query_selector(selector)
            if element is not None and await _element_visible(element) and not await _element_disabled(element):
                text = (await _element_text(element)).lower()
                if "save for later" not in text:
                    log.info("found_submit_button_from_memory", alt_name=alt_name, url=getattr(page, "url", ""))
                    return element

    # Standard selectors
    for selector in SUBMIT_BUTTON_SELECTORS:
        element = await page.query_selector(selector)
        if element is None:
            continue
        if not await _element_visible(element):
            continue
        if await _element_disabled(element):
            continue
        text = (await _element_text(element)).lower()
        if "save for later" in text:
            continue
        return element

    # Fallback: full text scan for any clickable element whose text signals submission.
    # Catches non-standard buttons like "Join the awesome squad. Apply now!"
    _SUBMIT_KEYWORDS = ("apply now", "submit application", "send application", "complete application", "submit your application")
    for selector in ("button", "a", "[role='button']", "input[type='submit']"):
        try:
            elements = await page.query_selector_all(selector)
        except Exception:
            continue
        for element in elements:
            if not await _element_visible(element):
                continue
            if await _element_disabled(element):
                continue
            text = (await _element_text(element)).lower()
            if not text or "save for later" in text:
                continue
            if any(kw in text for kw in _SUBMIT_KEYWORDS):
                log.info("found_submit_button_via_text_scan", text=text[:80], url=getattr(page, "url", ""))
                return element

    return None


async def advance_to_next_page(page) -> str:
    last_error: Exception | None = None
    for attempt in range(1, config.MAX_RETRY_BUDGET + 1):
        next_button = await find_next_button(page)
        if next_button is None:
            raise NavigationError("No next button found but form is not complete")
        old_first_input = await _first_visible_form_input(page)
        try:
            await next_button.scroll_into_view_if_needed()
            await asyncio.sleep(random.uniform(config.BROWSER_HUMAN_DELAY_MIN_SECONDS, config.BROWSER_HUMAN_DELAY_MAX_SECONDS))
            await next_button.click()
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    log.warning("next_page_load_wait_failed", url=getattr(page, "url", ""), attempt=attempt)
            if old_first_input is not None:
                try:
                    await old_first_input.wait_for(state="detached", timeout=5000)
                except Exception as exc:
                    log.debug("next_page_old_input_not_detached", url=getattr(page, "url", ""), error=str(exc), attempt=attempt)
            await page.wait_for_timeout(500)
            page_type = await detect_page_type(page)
            log.info("advanced_to_next_page", new_page_type=page_type, url=getattr(page, "url", ""), attempt=attempt)
            return page_type
        except Exception as exc:
            last_error = exc
            log.warning("advance_to_next_page_retry", url=getattr(page, "url", ""), attempt=attempt, error=str(exc))
            await asyncio.sleep(min(0.4 * attempt, 1.5))
    raise NavigationError(f"Failed to advance to next page after retries: {last_error}")


async def check_for_validation_errors(page) -> list[str]:
    error_selectors = [
        "[class*='error']",
        "[class*='invalid']",
        "[aria-invalid='true']",
        ".field-error",
        "[data-automation-id*='error']",
    ]
    errors: list[str] = []
    seen: set[str] = set()
    for selector in error_selectors:
        try:
            elements = await page.query_selector_all(selector)
        except Exception:
            continue
        for element in elements:
            try:
                if not await _element_visible(element):
                    continue
                text = (await _element_text(element)).strip()
            except Exception:
                continue
            if text and text not in seen:
                seen.add(text)
                errors.append(text)
    return errors


async def _wait_for_page_ready(page) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception as exc:
        log.debug("page_networkidle_wait_failed", url=getattr(page, "url", ""), error=str(exc))
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception as fallback_exc:
            log.debug("page_domcontentloaded_wait_failed", url=getattr(page, "url", ""), error=str(fallback_exc))


async def _page_has_visible_form(page) -> bool:
    for selector in FORM_INDICATOR_SELECTORS:
        try:
            elements = await page.query_selector_all(selector)
        except Exception:
            continue
        for element in elements:
            if not await _element_visible(element):
                continue
            if selector == "form":
                try:
                    action = ((await element.get_attribute("action")) or "").lower()
                except Exception as exc:
                    log.debug("form_action_read_failed", url=getattr(page, "url", ""), error=str(exc))
                    action = ""
                if "apply" in action or "application" in action:
                    return True
                continue
            if await _is_application_input(page, element):
                return True
    return False


async def _is_application_input(page, element) -> bool:
    try:
        input_type = ((await element.get_attribute("type")) or "").lower()
        name = ((await element.get_attribute("name")) or "").lower()
        aria_label = ((await element.get_attribute("aria-label")) or "").lower()
        placeholder = ((await element.get_attribute("placeholder")) or "").lower()
        element_id = ((await element.get_attribute("id")) or "").lower()
        labelish = " ".join([name, aria_label, placeholder, element_id]).strip()
    except Exception as exc:
        log.debug("application_input_introspection_failed", error=str(exc))
        return False
    page_url = (getattr(page, "url", "") or "").lower()
    if input_type == "file":
        return True
    if input_type == "email":
        if await _element_in_non_application_widget(element):
            return False
        if any(token in page_url for token in ("/apply", "/application", "job_application")):
            return True
        context_text = await _element_context_text(element)
        return any(marker in context_text for marker in STRONG_APPLICATION_FIELD_MARKERS)
    if input_type == "text" and any(token in labelish for token in ("name", "email", "phone", "first", "last")):
        if await _element_in_non_application_widget(element):
            return False
        return True
    return False


async def _element_in_non_application_widget(element) -> bool:
    context_text = await _element_context_text(element)
    if any(marker in context_text for marker in STRONG_APPLICATION_FIELD_MARKERS):
        return False
    return any(marker in context_text for marker in NON_APPLICATION_WIDGET_MARKERS)


async def _element_context_text(element) -> str:
    try:
        return await element.evaluate(
            r"""
            (el) => {
              const text = (node) => (node?.innerText || node?.textContent || '').replace(/\s+/g, ' ').trim();
              const selectors = [
                'form',
                'aside',
                'section',
                'article',
                '[role="form"]',
                '[role="search"]',
                '[class*="alert"]',
                '[class*="similar"]',
                '[class*="recommend"]',
                '[class*="chat"]',
                '[id*="alert"]',
                '[id*="chat"]',
              ];
              const node = el.closest(selectors.join(','));
              return text(node || el.parentElement || el).toLowerCase();
            }
            """
        )
    except Exception as exc:
        log.debug("element_context_text_failed", error=str(exc))
        return ""


async def _button_opens_new_tab(page, element) -> bool:
    try:
        target = (await element.get_attribute("target")) or ""
        if target == "_blank":
            return True
        href = (await element.get_attribute("href")) or ""
        if href.startswith("http"):
            current_host = urlparse(getattr(page, "url", "")).hostname or ""
            href_host = urlparse(href).hostname or ""
            if current_host and href_host and href_host != current_host:
                return True
    except Exception as exc:
        log.debug("apply_button_attribute_read_failed", url=getattr(page, "url", ""), error=str(exc))
    if "myworkdayjobs.com" in ((getattr(page, "url", "") or "").lower()):
        return True
    return False


async def _page_contains_any_text(page, patterns: list[str]) -> bool:
    for pattern in patterns:
        try:
            locator = page.get_by_text(pattern, exact=False)
            if await locator.count():
                first = locator.first
                if await first.is_visible():
                    return True
        except Exception as exc:
            log.debug("page_text_probe_failed", pattern=pattern, url=getattr(page, "url", ""), error=str(exc))
            continue
    return False


async def _has_visible_form_inputs(page) -> bool:
    selectors = [
        "input:not([type='hidden']):not([type='submit'])",
        "textarea",
        "select",
    ]
    for selector in selectors:
        try:
            elements = await page.query_selector_all(selector)
        except Exception:
            continue
        for element in elements:
            if await _element_visible(element):
                return True
    return False


async def _element_visible(element) -> bool:
    try:
        return bool(await element.is_visible())
    except Exception:
        return False


async def _element_disabled(element) -> bool:
    try:
        return bool(await element.is_disabled())
    except Exception:
        return False


async def _element_text(element) -> str:
    for method_name in ("inner_text", "text_content"):
        try:
            method = getattr(element, method_name)
            text = await method()
            if text:
                return " ".join(str(text).split())
        except Exception:
            continue
    try:
        value = await element.get_attribute("value")
        return " ".join(str(value or "").split())
    except Exception:
        return ""


async def _choose_manual_radio(page, radios) -> object | None:
    for radio in radios:
        label = (await _get_label_for_input(page, radio)).lower()
        if not label:
            continue
        if any(term in label for term in SOCIAL_ENTRY_TERMS):
            continue
        if not any(term in label for term in MANUAL_ENTRY_POSITIVE_TERMS):
            continue
        return radio
    return None


async def _find_apply_button_by_text(page):
    selectors = ["button", "a", "[role='button']", "input[type='button']", "input[type='submit']"]
    for selector in selectors:
        try:
            elements = await page.query_selector_all(selector)
        except Exception as exc:
            log.debug("apply_button_text_probe_failed", selector=selector, error=str(exc))
            continue
        for element in elements:
            if not await _element_visible(element):
                continue
            text = (await _element_text(element)).lower()
            if not text:
                continue
            if any(keyword in text for keyword in APPLY_TRIGGER_KEYWORDS):
                return element
    return None


async def _entry_method_visible(page) -> bool:
    try:
        return await _find_manual_entry_control(page) is not None
    except Exception as exc:
        log.debug("entry_method_visible_probe_failed", url=getattr(page, "url", ""), error=str(exc))
        return False


async def _find_manual_entry_control(page):
    selectors = [
        "button",
        "a",
        "label",
        "[role='button']",
        "[role='radio']",
        "[role='option']",
        "input[type='radio']",
    ]
    for selector in selectors:
        try:
            elements = await page.query_selector_all(selector)
        except Exception as exc:
            log.debug("manual_entry_control_probe_failed", selector=selector, error=str(exc))
            continue
        for element in elements:
            if not await _element_visible(element):
                continue
            if selector == "input[type='radio']":
                label = (await _get_label_for_input(page, element)).lower()
            else:
                label = (await _element_text(element)).lower()
            if not label:
                continue
            if any(term in label for term in SOCIAL_ENTRY_TERMS):
                continue
            if any(term in label for term in MANUAL_ENTRY_POSITIVE_TERMS):
                return element
    return None


async def _get_label_for_input(page, input_element) -> str:
    try:
        element_id = await input_element.get_attribute("id")
    except Exception:
        element_id = None
    if element_id:
        try:
            label = await page.query_selector(f"label[for='{element_id}']")
            if label is not None:
                text = await _element_text(label)
                if text:
                    return text
        except Exception as exc:
            log.debug("input_label_lookup_by_for_failed", error=str(exc))
    try:
        label = await input_element.evaluate_handle("el => el.closest('label')")
        if label:
            text = await label.evaluate("(el) => (el.innerText || el.textContent || '').trim()")
            if text:
                return " ".join(str(text).split())
    except Exception as exc:
        log.debug("input_label_lookup_by_ancestor_failed", error=str(exc))
    try:
        text = await input_element.evaluate(
            """(el) => {
                const next = el.nextSibling;
                if (next && next.textContent) return next.textContent.trim();
                return '';
            }"""
        )
        if text:
            return " ".join(str(text).split())
    except Exception as exc:
        log.debug("input_label_lookup_by_sibling_failed", error=str(exc))
    try:
        return " ".join(str((await input_element.get_attribute("value")) or "").split())
    except Exception:
        return ""


async def _first_visible_form_input(page):
    selectors = [
        "input:not([type='hidden']):not([type='submit'])",
        "textarea",
        "select",
        "[role='combobox']",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() and await locator.is_visible():
                return locator
        except Exception as exc:
            log.debug("first_visible_form_input_probe_failed", selector=selector, error=str(exc))
            continue
    return None


async def detect_validation_errors(page) -> list[dict]:
    """Detect visible form validation errors on the current page.

    Returns a list of dicts with 'text' and 'selector' keys for each
    visible error element found. Used for error recovery after clicking
    Next/Submit when validation fails.
    """
    errors: list[dict] = []
    for selector in VALIDATION_ERROR_SELECTORS:
        try:
            elements = await page.query_selector_all(selector)
            for el in elements:
                if not await _element_visible(el):
                    continue
                text = await _element_text(el)
                if not text:
                    continue
                text_lower = text.lower()
                # Only include if it matches a known error pattern
                if any(pattern in text_lower for pattern in VALIDATION_ERROR_TEXT_PATTERNS):
                    errors.append({"text": text, "selector": selector})
        except Exception:
            continue
    # Deduplicate by error text
    seen_texts: set[str] = set()
    unique_errors: list[dict] = []
    for error in errors:
        if error["text"] not in seen_texts:
            seen_texts.add(error["text"])
            unique_errors.append(error)
    if unique_errors:
        log.info("validation_errors_detected", count=len(unique_errors), errors=[e["text"][:80] for e in unique_errors[:5]])
    return unique_errors


async def check_form_advanced(page, previous_input_selectors: list[str] | None = None) -> dict:
    """Check whether the form page advanced after clicking Next.

    Compares the current visible form fields against a snapshot of
    fields from before clicking Next. If the fields haven't changed,
    the form likely didn't advance (validation error, stuck, etc.).

    Returns a dict with:
      - 'advanced': bool — whether the page appears to have moved forward
      - 'errors': list — any validation errors detected
      - 'current_inputs': list — current visible input selectors for future comparison
    """
    current_inputs: list[str] = []
    try:
        inputs = await page.query_selector_all("input:not([type='hidden']), textarea, select")
        for inp in inputs:
            if await _element_visible(inp):
                name = await inp.get_attribute("name") or await inp.get_attribute("id") or ""
                if name:
                    current_inputs.append(name)
    except Exception:
        pass

    errors = await detect_validation_errors(page)
    advanced = True
    if previous_input_selectors is not None and current_inputs:
        # If 80%+ of inputs are the same, the form likely didn't advance
        overlap = set(current_inputs) & set(previous_input_selectors)
        total = max(len(current_inputs), len(previous_input_selectors), 1)
        if len(overlap) / total > 0.8:
            advanced = False

    return {
        "advanced": advanced and not errors,
        "errors": errors,
        "current_inputs": current_inputs,
    }
