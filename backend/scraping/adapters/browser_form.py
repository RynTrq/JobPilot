from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import urljoin, urlparse

import structlog

from backend import config
from backend.form.answerer import FormField
from backend.scraping.adapters.base import JobListing, SubmitResult
from backend.scraping.adapters.generic import GenericAdapter
from backend.scraping.browser import goto_with_pacing, human_delay


FIELD_ENUM_SCRIPT = r"""
() => {
  const fields = [];
  const seenGroups = new Set();
  const seenElements = new Set();
  const cssEscape = window.CSS && CSS.escape ? CSS.escape : (value) => String(value).replace(/"/g, '\\"');

  function visible(el) {
    if (!el || !el.isConnected) return false;
    if (el.closest('[hidden], [inert], [aria-hidden="true"]')) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden'
      && style.display !== 'none'
      && rect.width > 0
      && rect.height > 0;
  }

  function text(el) {
    return (el?.innerText || el?.textContent || '').replace(/\s+/g, ' ').trim();
  }

  function labelFor(el) {
    const ariaLabel = el.getAttribute('aria-label');
    if (ariaLabel) return ariaLabel;

    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      for (const id of labelledBy.split(/\s+/)) {
        const node = document.getElementById(id);
        if (node && text(node)) return text(node);
      }
    }

    if (el.id) {
      const byFor = document.querySelector(`label[for="${cssEscape(el.id)}"]`);
      if (byFor && text(byFor)) return text(byFor);
    }

    const wrappingLabel = el.closest('label');
    if (wrappingLabel && text(wrappingLabel)) return text(wrappingLabel).replace(/\s+/g, ' ').trim();

    const placeholder = el.getAttribute('placeholder');
    if (placeholder) return placeholder;

    const fieldset = el.closest('fieldset');
    const legend = fieldset?.querySelector('legend');
    if (legend && text(legend)) return text(legend);

    const prev = el.previousElementSibling;
    if (prev && text(prev)) return text(prev);

    let parent = el.parentElement;
    for (let depth = 0; parent && depth < 6; depth++, parent = parent.parentElement) {
      const directChildren = Array.from(parent.children || []);
      const label = directChildren.find((child) => child !== el && /^(LABEL|SPAN|DIV|P)$/i.test(child.tagName) && text(child));
      if (label && text(label)) return text(label);
      const heading = directChildren.find((child) => child !== el && /^(LEGEND|H1|H2|H3|H4|H5|H6)$/i.test(child.tagName) && text(child));
      if (heading && text(heading)) return text(heading);
    }

    const describedBy = el.getAttribute('aria-describedby');
    if (describedBy) {
      for (const id of describedBy.split(/\s+/)) {
        const node = document.getElementById(id);
        if (node && text(node)) return text(node);
      }
    }
    
    return el.getAttribute('name') || el.id || '';
  }

  function groupLabelFor(el, group) {
    const fieldset = el.closest('fieldset');
    const legend = fieldset?.querySelector('legend');
    if (legend && text(legend)) return text(legend);

    const questionContainer = el.closest('li.application-question, li.custom-question, [data-automation-id*="question"]');
    if (questionContainer) {
      const promptNode = Array.from(questionContainer.children || []).find((child) => {
        if (group.includes(child)) return false;
        return !group.some((option) => child.contains(option)) && text(child) && text(child).length < 300;
      });
      if (promptNode && text(promptNode)) return text(promptNode);
    }

    let parent = el.parentElement;
    for (let depth = 0; parent && depth < 8; depth++, parent = parent.parentElement) {
      const previous = parent.previousElementSibling;
      if (previous && text(previous) && text(previous).length < 300) {
        return text(previous);
      }
      const directChildren = Array.from(parent.children || []).filter((child) => {
        if (group.includes(child)) return false;
        return !group.some((option) => child.contains(option));
      });
      const prompt = directChildren.find((child) => /^(LABEL|LEGEND|SPAN|DIV|P|H1|H2|H3|H4|H5|H6|DT)$/i.test(child.tagName) && text(child) && text(child).length < 300);
      if (prompt && text(prompt)) return text(prompt);
    }

    return labelFor(el);
  }

  function mark(el, attr, idx) {
    el.setAttribute(attr, String(idx));
    return `[${attr}="${idx}"]`;
  }

  function selectorFor(el, idx) {
    const id = el.getAttribute('id');
    if (id) return `#${cssEscape(id)}`;
    const name = el.getAttribute('name');
    if (name) {
      const byName = `[name="${cssEscape(name)}"]`;
      if ((document.querySelectorAll(byName) || []).length === 1) return byName;
    }
    const testId = el.getAttribute('data-testid');
    if (testId) {
      const byTestId = `[data-testid="${cssEscape(testId)}"]`;
      if ((document.querySelectorAll(byTestId) || []).length === 1) return byTestId;
    }
    const automationId = el.getAttribute('data-automation-id');
    if (automationId) {
      const byAutomation = `[data-automation-id="${cssEscape(automationId)}"]`;
      if ((document.querySelectorAll(byAutomation) || []).length === 1) return byAutomation;
    }
    el.setAttribute('data-jobpilot-field', String(idx));
    return `[data-jobpilot-field="${idx}"]`;
  }

  function fieldTypeFor(el) {
    const tag = (el.tagName || '').toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const role = (el.getAttribute('role') || '').toLowerCase();
    if (tag === 'select') return 'select';
    if (tag === 'textarea') return 'textarea';
    if (role === 'combobox' || role === 'listbox') return 'select';
    if (role === 'checkbox') return 'checkbox';
    if (role === 'radio') return 'radio';
    if (role === 'switch') return 'checkbox';
    if (role === 'spinbutton') return 'number';
    if (role === 'textbox' || el.isContentEditable) return 'text';
    if (tag === 'input') return type || 'text';
    return '';
  }

  function inferredOptionsFor(labelText, type, tag, role) {
    if (type !== 'select' || tag === 'select') return null;
    const label = (labelText || '').replace(/\s+/g, ' ').trim().toLowerCase();
    if (!label) return null;
    const booleanQuestion =
      /\b(have|has|do|does|did|are|is|can|could|will|would|should)\s+you\b/.test(label)
      || /\b(previously worked|eligible|authorized|authorised|sponsor|sponsorship|right to work)\b/.test(label);
    if (booleanQuestion && !/\b(country|city|location|gender|race|ethnicity|veteran|disability)\b/.test(label)) {
      return ['Yes', 'No'];
    }
    return null;
  }

  const selectors = [
    'input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="reset"]):not([type="image"])',
    'textarea',
    'select',
    '[contenteditable="true"]',
    '[role="textbox"]',
    '[role="combobox"]',
    '[role="listbox"]',
    '[role="checkbox"]',
    '[role="radio"]',
    '[role="switch"]',
    '[role="spinbutton"]',
    '[data-testid*="input"] input',
    '[data-testid*="field"] input',
    '[data-testid*="field"] textarea',
    '[data-testid*="field"] select',
    '[data-testid*="select"] [role="combobox"]',
  ];

  const elements = [];
  for (const selector of selectors) {
    try {
      for (const el of Array.from(document.querySelectorAll(selector))) {
        if (seenElements.has(el)) continue;
        seenElements.add(el);
        if (!visible(el)) continue;
        const type = fieldTypeFor(el);
        if (!type) continue;
        elements.push(el);
      }
    } catch (err) {
      continue;
    }
  }

  for (const el of elements) {
    const type = fieldTypeFor(el);
    const name = el.getAttribute('name') || '';
    const tag = (el.tagName || '').toLowerCase();
    const role = el.getAttribute('role') || '';
    const labelText = labelFor(el);
    const required = !!el.required || el.getAttribute('aria-required') === 'true';
    const enabled = !(el.disabled || el.getAttribute('disabled') !== null || el.getAttribute('readonly') !== null || el.getAttribute('aria-disabled') === 'true');
    if ((type === 'radio' || type === 'checkbox') && name) {
      const groupKey = `${type}:${name}`;
      if (seenGroups.has(groupKey)) continue;
      seenGroups.add(groupKey);
      const group = Array.from(document.querySelectorAll(`input[type="${type}"][name="${cssEscape(name)}"], [role="${type}"][name="${cssEscape(name)}"]`)).filter(visible);
      const groupLabelText = groupLabelFor(el, group);
      const firstRect = (group[0] || el).getBoundingClientRect();
      const optionSelectors = {};
      const options = [];
      for (const option of group) {
        const optionLabel = labelFor(option) || option.value || 'selected';
        const optionIndex = `${fields.length}-${Object.keys(optionSelectors).length}`;
        option.setAttribute('data-jobpilot-option', optionIndex);
        options.push(optionLabel);
        optionSelectors[optionLabel] = `[data-jobpilot-option="${optionIndex}"]`;
      }
      fields.push({
        label_text: groupLabelText,
        field_type: type,
        selector: selectorFor(el, fields.length),
        required,
        options,
        option_selectors: optionSelectors,
        char_limit: el.maxLength && el.maxLength > 0 ? el.maxLength : null,
        name,
        accept: el.accept || null,
        tag,
        role,
        element_id: el.id || null,
        placeholder: el.getAttribute('placeholder') || null,
        aria_label: el.getAttribute('aria-label') || null,
        visible: true,
        enabled,
        bounding_box: {
          x: firstRect.left + window.scrollX,
          y: firstRect.top + window.scrollY,
          width: firstRect.width,
          height: firstRect.height
        }
      });
      continue;
    }
    const idx = fields.length;
    const rect = el.getBoundingClientRect();
    fields.push({
      label_text: labelText,
      field_type: type,
      selector: selectorFor(el, idx),
      required,
      options: tag === 'select'
        ? Array.from(el.options).map(opt => opt.text.trim()).filter(Boolean)
        : inferredOptionsFor(labelText, type, tag, role),
      option_selectors: null,
      char_limit: el.maxLength && el.maxLength > 0 ? el.maxLength : null,
      name,
      accept: el.accept || null,
      tag,
      role,
      element_id: el.id || null,
        placeholder: el.getAttribute('placeholder') || null,
        aria_label: el.getAttribute('aria-label') || null,
        visible: true,
        enabled,
        bounding_box: {
          x: rect.left + window.scrollX,
          y: rect.top + window.scrollY,
          width: rect.width,
          height: rect.height
        }
      });
  }
  fields.sort((a, b) => {
    const yDiff = (a.bounding_box?.y || 0) - (b.bounding_box?.y || 0);
    if (Math.abs(yDiff) > 10) return yDiff;
    return (a.bounding_box?.x || 0) - (b.bounding_box?.x || 0);
  });
  return fields;
}
"""

log = structlog.get_logger()


class BrowserFormAdapter(GenericAdapter):
    apply_selectors = (
        "button:has-text('Apply')",
        "a:has-text('Apply')",
        "button:has-text('Apply now')",
        "a:has-text('Apply now')",
        "button:has-text('Apply Now')",
        "a:has-text('Apply Now')",
        "a:has-text('Apply for this role')",
        "a:has-text('Apply for this job')",
        "button:has-text('Start Application')",
        "button:has-text('Start application')",
        "a:has-text('Start Application')",
        "a:has-text('Start application')",
        "button:has-text('Begin Application')",
        "a:has-text('Begin Application')",
        "button:has-text('Apply online')",
        "a:has-text('Apply online')",
        "button:has-text('Apply Online')",
        "a:has-text('Apply Online')",
        "button:has-text('Easy Apply')",
        "a:has-text('Easy Apply')",
        "button:has-text('Quick Apply')",
        "a:has-text('Quick Apply')",
        "button:has-text(\"I'm interested\")",
        "a:has-text(\"I'm interested\")",
        "button:has-text('Submit application')",
        "[role='button']:has-text('Apply')",
        "[role='button']:has-text('Apply now')",
        "[role='button']:has-text('Start application')",
        "[data-testid*='apply']",
        "[data-testid*='Apply']",
        "[data-automation-id*='apply']",
        "[data-automation-id*='Apply']",
        "a[href*='/apply']",
        "a[href*='apply']",
        "a[href*='job_application']",
        "a[href*='application']",
    )
    submit_selectors = (
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Submit')",
        "button:has-text('Submit application')",
        "button:has-text('Send application')",
        "button:has-text('Apply')",
    )
    continue_selectors = (
        "button:has-text('Next')",
        "button:has-text('Continue')",
        "button:has-text('Review')",
        "button:has-text('Save and continue')",
    )
    apply_keywords = (
        "apply",
        "apply now",
        "start application",
        "begin application",
        "submit application",
        "apply online",
        "easy apply",
        "quick apply",
    )
    manual_terms = (
        "manual",
        "manually",
        "fill out manually",
        "fill manually",
        "enter manually",
        "continue manually",
        "apply manually",
    )
    non_manual_terms = ("linkedin", "indeed", "upload", "resume", "cv", "autofill", "import")

    async def _goto(self, page, url: str) -> None:
        try:
            await goto_with_pacing(page, url, timeout=20000)
            await self._settle(page, 250)
        except Exception:
            await page.goto(url, wait_until="load", timeout=30000)
            await self._settle(page, 500)

    async def list_jobs(self, page, career_url: str) -> list[JobListing]:
        await self._goto(page, career_url)
        if self._looks_like_direct_job_url(career_url):
            return [
                JobListing(
                    url=career_url,
                    title_preview=await self._page_title(page),
                    ext_id=career_url.rstrip("/").split("/")[-1],
                )
            ]
        listings = await super().list_jobs(page, career_url)
        if listings:
            return listings
        return [JobListing(url=career_url, title_preview=await self._page_title(page), ext_id=career_url.rstrip("/").split("/")[-1])]

    async def extract_description(self, page, job_url: str) -> str:
        return await super().extract_description(page, job_url)

    async def open_application(self, page, job_url: str) -> None:
        await self._goto(page, job_url)
        await self._cleanup_extra_pages(page)
        if await self._is_auth_wall(page):
            raise NotImplementedError("Sign-in is required before the application form can be automated.")
        if await self._form_context(page, require_application_markers=True):
            return
        if await self._select_manual_entry(page):
            await self._settle(page, 500)
            if await self._form_context(page):
                return
        for selector in self.apply_selectors:
            try:
                element = page.locator(selector).first
                if await element.count() and await element.is_visible():
                    if await self._activate_apply_trigger(page, element, job_url):
                        return
            except NotImplementedError:
                raise
            except Exception as exc:
                log.debug("open_application_selector_failed", selector=selector, url=getattr(page, "url", job_url), error=str(exc))
                continue
        text_trigger = await self._find_apply_trigger_by_text(page)
        if text_trigger is not None and await self._activate_apply_trigger(page, text_trigger, job_url):
            return
        raise NotImplementedError("No supported application form was found on this site.")

    async def enumerate_fields(self, page) -> list[FormField]:
        context = await self._form_context(page)
        if context is None:
            raise NotImplementedError("No fillable application fields were found on this site.")
        raw_fields = await context.evaluate(FIELD_ENUM_SCRIPT)
        fields = [
            FormField(**field)
            for field in raw_fields
            if (field.get("label_text") or field.get("name")) and field.get("enabled", True)
        ]
        for field in fields:
            if field.field_type in {"radio", "checkbox"} and field.options and len(field.options) > 1:
                normalized_label = " ".join((field.label_text or "").split()).strip().lower()
                normalized_options = {" ".join(str(option).split()).strip().lower() for option in field.options}
                if normalized_label in normalized_options:
                    better_label = await self._derive_group_prompt(context, field)
                    if better_label:
                        field.label_text = better_label
        if not fields:
            raise NotImplementedError("No fillable application fields were found on this site.")
        return fields

    async def _derive_group_prompt(self, context, field: FormField) -> str | None:
        if not field.selector:
            return None
        locator = context.locator(field.selector).first
        try:
            prompt = await locator.evaluate(
                """(el) => {
                    const text = (node) => (node?.innerText || node?.textContent || '').replace(/\\s+/g, ' ').trim();
                    const optionLabels = new Set();
                    const fieldName = el.getAttribute('name');
                    if (fieldName) {
                        for (const option of Array.from(document.querySelectorAll(`input[name="${fieldName}"]`))) {
                            const wrapping = option.closest('label');
                            if (wrapping && text(wrapping)) optionLabels.add(text(wrapping));
                        }
                    }
                    let parent = el.parentElement;
                    for (let depth = 0; parent && depth < 8; depth++, parent = parent.parentElement) {
                        const prev = parent.previousElementSibling;
                        if (prev && text(prev) && !optionLabels.has(text(prev))) return text(prev);
                        if (parent.classList && parent.classList.contains('application-field')) {
                            const wrapperPrev = parent.previousElementSibling;
                            if (wrapperPrev && text(wrapperPrev) && !optionLabels.has(text(wrapperPrev))) return text(wrapperPrev);
                        }
                    }
                    const fieldset = el.closest('fieldset');
                    const legend = fieldset?.querySelector('legend');
                    if (legend && text(legend)) return text(legend);
                    const question = el.closest('li.application-question, li.custom-question');
                    if (question) {
                        const parts = Array.from(question.childNodes)
                            .map((node) => text(node))
                            .filter(Boolean)
                            .filter((value) => !optionLabels.has(value));
                        if (parts.length) return parts[0];
                    }
                    return '';
                }"""
            )
        except Exception as exc:
            log.debug("derive_group_prompt_failed", selector=field.selector, error=str(exc))
            return None
        cleaned = " ".join(str(prompt or "").split()).strip()
        return cleaned or None

    async def fill_field(self, page, field: FormField, value) -> None:
        await human_delay()
        context = await self._form_context(page)
        if context is None:
            raise NotImplementedError("No fillable application fields were found on this site.")
        if not field.selector or field.field_type == "file":
            return
        field_type = field.field_type.lower()
        locator = context.locator(field.selector).first
        await locator.scroll_into_view_if_needed()
        if field_type == "checkbox":
            target = next(iter(field.option_selectors.values())) if field.option_selectors else field.selector
            box = context.locator(target).first
            await box.scroll_into_view_if_needed()
            truthy = str(value).strip().lower() in {"1", "true", "yes", "y", "checked", "on"}
            if truthy:
                try:
                    if await box.count() and not await box.is_checked():
                        await box.check(force=True)
                except Exception:
                    await box.click(force=True)
            await self._verify_field_value(context, field, truthy)
            return
        if field_type == "radio":
            selector = self._option_selector(field, str(value))
            if selector:
                radio = context.locator(selector).first
                await radio.scroll_into_view_if_needed()
                try:
                    await radio.check(force=True)
                except Exception:
                    await radio.click(force=True)
            await self._verify_field_value(context, field, value)
            return
        if field.role in {"combobox", "listbox"} and field.tag != "select":
            await locator.click(force=True)
            await page.keyboard.press("Control+a")
            await page.keyboard.press("Delete")
            try:
                await page.keyboard.type(str(value or ""), delay=0 if self._timing_disabled() else 15)
                await page.keyboard.press("Enter")
            except Exception:
                pass
            await locator.evaluate(
                """(el, nextValue) => {
                    if (el.isContentEditable) el.textContent = nextValue;
                    else if ('value' in el) el.value = nextValue;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                str(value or ""),
            )
            await self._verify_field_value(context, field, value)
            return
        if field_type == "select":
            try:
                await locator.select_option(label=str(value))
                await self._verify_field_value(context, field, value)
                return
            except Exception:
                pass
            try:
                await locator.select_option(value=str(value))
                await self._verify_field_value(context, field, value)
                return
            except Exception:
                pass
            options = field.options or []
            value_lower = str(value).strip().lower()
            for option in options:
                label = (option or "").strip()
                if value_lower and (value_lower == label.lower() or value_lower in label.lower() or label.lower() in value_lower):
                    await locator.select_option(label=label)
                    await self._verify_field_value(context, field, label)
                    return
            return
        if field.tag == "textarea" or field.role == "textbox" or field.tag == "input" or field.tag == "div":
            await locator.click(force=True)
            try:
                await locator.fill(str(value or ""))
                await locator.evaluate(
                    """(el) => {
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
                        if (el.blur) el.blur();
                    }"""
                )
                await self._verify_field_value(context, field, value)
                return
            except Exception:
                try:
                    await locator.evaluate(
                        """(el) => {
                            if (el.isContentEditable) {
                                el.textContent = "";
                            } else if ('value' in el) {
                                el.value = "";
                            }
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }"""
                    )
                except Exception:
                    pass
            await page.keyboard.press("Control+a")
            await page.keyboard.press("Delete")
            try:
                await locator.type(str(value or ""), delay=0 if self._timing_disabled() else 12)
            except Exception:
                await locator.evaluate(
                    """(el, nextValue) => {
                        if (el.isContentEditable) {
                            el.textContent = nextValue;
                        } else if ('value' in el) {
                            const descriptor =
                                Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')
                                || Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value');
                            if (descriptor && descriptor.set) descriptor.set.call(el, nextValue);
                            else el.value = nextValue;
                        }
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }""",
                    str(value or ""),
                )
            await locator.evaluate(
                """(el) => {
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
                    if (el.blur) el.blur();
                }"""
            )
            if not self._timing_disabled():
                await asyncio.sleep(0.05)
            await self._verify_field_value(context, field, value)
            return

    async def attach_resume(self, page, pdf_path: str | Path) -> None:
        await self._attach_file(page, pdf_path, kind="resume")

    async def attach_cover_letter(self, page, pdf_path: str | Path) -> None:
        await self._attach_file(page, pdf_path, kind="cover")

    async def submit(self, page) -> SubmitResult:
        await human_delay()
        context = await self._form_context(page)
        if context is None:
            return SubmitResult(ok=False, error="submit button not found")
        for _ in range(5):
            submit = None
            for selector in self.submit_selectors:
                candidate = context.locator(selector).last
                if await candidate.count():
                    submit = candidate
                    break
            if submit is not None:
                await submit.scroll_into_view_if_needed()
                await submit.click()
                break
            advanced = await self._advance_multi_step_form(context)
            if not advanced:
                return SubmitResult(ok=False, error="submit button not found")
            await context.wait_for_load_state("networkidle", timeout=30000)
        try:
            await context.wait_for_selector("text=/thank|submitted|received|confirmation|application sent/i", timeout=30000)
            confirmation = " ".join((await context.locator("body").inner_text()).split())[:1000]
            return SubmitResult(ok=True, confirmation_text=confirmation)
        except Exception:
            return SubmitResult(ok=False, error="no confirmation")

    async def _attach_file(self, page, pdf_path: str | Path, *, kind: str) -> None:
        file_path = Path(pdf_path)
        if not file_path.exists():
            raise RuntimeError(f"{kind} upload file does not exist: {file_path}")
        context = await self._form_context(page)
        if context is None:
            raise RuntimeError("resume upload field not found")
        fields = await self.enumerate_fields(page)
        files = [field for field in fields if field.field_type == "file"]
        if not files:
            log.info("file_attach_skipped", kind=kind, reason="no_file_fields_present")
            return
        if kind == "resume":
            preferred = [field for field in files if any(token in field.label_text.lower() for token in ["resume", "cv"])]
        else:
            preferred = [field for field in files if any(token in field.label_text.lower() for token in ["cover", "letter"])]
        field = (preferred or files)[0] if (preferred or files) else None
        if field is None or not field.selector:
            if kind == "cover":
                return
            raise RuntimeError("resume upload field not found")
        await context.locator(field.selector).set_input_files(str(file_path))
        log.info("file_attached", kind=kind, path=str(file_path), selector=field.selector)

    def _option_selector(self, field: FormField, value: str) -> str | None:
        if not field.option_selectors:
            return field.selector
        value_lower = value.lower().strip()
        for label, selector in field.option_selectors.items():
            label_lower = label.lower().strip()
            if label_lower == value_lower or value_lower in label_lower or label_lower in value_lower:
                return selector
        return None

    async def _has_form(self, page) -> bool:
        try:
            return await page.evaluate(
                r"""
                () => {
                const visible = (el) => {
                  if (!el || !el.isConnected) return false;
                  if (el.closest('[hidden], [inert], [aria-hidden="true"]')) return false;
                  const style = window.getComputedStyle(el);
                  const rect = el.getBoundingClientRect();
                  return style.visibility !== 'hidden'
                    && style.display !== 'none'
                    && rect.width > 0
                    && rect.height > 0;
                };
                const visibleFields = Array.from(document.querySelectorAll(
                  'input, textarea, select, [contenteditable="true"], [role="textbox"], [role="combobox"], [role="listbox"], [role="checkbox"], [role="radio"], [role="switch"], [role="spinbutton"], [data-automation-id], [data-testid*="field"], [data-testid*="input"]'
                )).filter((el) => {
                  const type = (el.type || '').toLowerCase();
                  if (['hidden', 'submit', 'button', 'reset', 'image'].includes(type)) return false;
                  return visible(el);
                });
                if (!visibleFields.length) return false;

                const text = (el) => (el?.innerText || el?.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
                const labelFor = (el) => {
                  if (el.id) {
                    const byFor = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                    if (byFor && text(byFor)) return text(byFor);
                  }
                  const wrappingLabel = el.closest('label');
                  if (wrappingLabel && text(wrappingLabel)) return text(wrappingLabel);
                  const labelledBy = (el.getAttribute('aria-labelledby') || '').split(/\s+/).map((id) => text(document.getElementById(id))).filter(Boolean);
                  return [el.getAttribute('aria-label'), el.getAttribute('placeholder'), el.name, el.id, ...labelledBy]
                    .filter(Boolean).join(' ').toLowerCase();
                };
                const combined = visibleFields.map((el) => `${labelFor(el)} ${el.name || ''} ${el.id || ''} ${el.type || ''}`).join(' ');
                const searchOnly = visibleFields.every((el) => {
                  const haystack = `${labelFor(el)} ${el.name || ''} ${el.id || ''} ${el.type || ''}`.toLowerCase();
                  return /\b(search|keyword|radius|zip|zipcode|postal|language|hl)\b/.test(haystack)
                    || ['k', 'l', 'r', 'hl'].includes((el.name || '').toLowerCase());
                });
                if (searchOnly) return false;
                if (visibleFields.some((el) => (el.type || '').toLowerCase() === 'file')) return true;
                if (/(resume|cv|cover letter|full name|first name|last name|email|phone|linkedin|github|portfolio|work authorization|sponsor)/.test(combined)) return true;
                return visibleFields.length >= 4;
                }
                """
            )
        except Exception:
            return False

    async def _form_context(self, page, *, require_application_markers: bool = False):
        contexts = [page, *getattr(page, "frames", [])]
        seen = set()
        for context in contexts:
            marker = id(context)
            if marker in seen:
                continue
            seen.add(marker)
            try:
                # Fallback: if heuristic misses, trust actual extracted fields.
                raw_fields = await context.evaluate(FIELD_ENUM_SCRIPT)
                application_like = self._raw_fields_look_like_application(
                    raw_fields,
                    allow_large_generic_forms=not require_application_markers,
                )
                non_application_widget = self._raw_fields_look_like_non_application_widget(raw_fields)
                search_filter_like = self._raw_fields_look_like_search_filter(raw_fields)
                if non_application_widget:
                    continue
                if any(field.get("label_text") or field.get("name") for field in raw_fields) and (
                    application_like or (not require_application_markers and not search_filter_like)
                ):
                    return context
                if await self._has_form(context) and (application_like or (not require_application_markers and not search_filter_like)):
                    return context
            except Exception as exc:
                log.debug("form_context_probe_failed", url=getattr(page, "url", None), error=str(exc))
                continue
        return None

    def _raw_fields_look_like_application(self, raw_fields: list[dict], *, allow_large_generic_forms: bool = True) -> bool:
        fields = [
            field
            for field in raw_fields
            if (field.get("label_text") or field.get("name"))
            and field.get("enabled", True)
            and field.get("visible", True)
        ]
        if not fields:
            return False
        if self._raw_fields_look_like_non_application_widget(fields):
            return False
        labels = " ".join(
            " ".join(
                str(field.get(key) or "")
                for key in ("label_text", "name", "placeholder", "aria_label", "field_type", "accept")
            ).lower()
            for field in fields
        )
        if any((field.get("field_type") or "").lower() == "file" for field in fields):
            return True
        marker_groups = (
            ("resume", "cv", "cover letter"),
            ("full name", "first name", "last name", "given name", "surname"),
            ("email", "e-mail"),
            ("phone", "mobile", "telephone"),
            ("linkedin", "github", "portfolio", "website"),
            ("work authorization", "right to work", "sponsor", "visa"),
        )
        matched_groups = sum(1 for group in marker_groups if any(marker in labels for marker in group))
        if matched_groups >= 2:
            return True
        if self._raw_fields_look_like_search_filter(fields):
            return False
        search_markers = ("search", "keyword", "radius", "zip", "zipcode", "postal", "alert", "subscribe")
        if len(fields) <= 2 and any(marker in labels for marker in search_markers):
            return False
        return allow_large_generic_forms and len(fields) >= 4

    def _raw_fields_look_like_non_application_widget(self, raw_fields: list[dict]) -> bool:
        fields = [
            field
            for field in raw_fields
            if (field.get("label_text") or field.get("name"))
            and field.get("enabled", True)
            and field.get("visible", True)
        ]
        if not fields:
            return False
        field_texts = [
            " ".join(
                str(field.get(key) or "")
                for key in ("label_text", "name", "placeholder", "aria_label", "element_id")
            ).lower()
            for field in fields
        ]
        combined = " ".join(field_texts)
        strong_application_markers = (
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
        if any(marker in combined for marker in strong_application_markers):
            return False
        widget_markers = (
            "alert",
            "ask anything",
            "chat",
            "chatbot",
            "job alert",
            "manage alerts",
            "notified",
            "notification",
            "recommendation",
            "similar jobs",
            "subscribe",
        )
        matched = sum(1 for text in field_texts if any(marker in text for marker in widget_markers))
        return matched >= max(1, len(fields) - 1)

    def _raw_fields_look_like_search_filter(self, raw_fields: list[dict]) -> bool:
        fields = [
            field
            for field in raw_fields
            if (field.get("label_text") or field.get("name"))
            and field.get("enabled", True)
            and field.get("visible", True)
        ]
        if not fields:
            return False
        if self._raw_fields_look_like_non_application_widget(fields):
            return True
        field_texts = [
            " ".join(
                str(field.get(key) or "")
                for key in ("label_text", "name", "placeholder", "aria_label", "element_id")
            ).lower()
            for field in fields
        ]
        search_markers = (
            "alert",
            "category",
            "department",
            "distance",
            "experience",
            "filter",
            "job title",
            "keyword",
            "location",
            "minimum salary",
            "radius",
            "salary",
            "search",
            "sort",
            "sort by",
            "subscribe",
            "zip",
            "zipcode",
        )
        application_markers = (
            "address",
            "cover letter",
            "current company",
            "current title",
            "cv",
            "e-mail",
            "email",
            "first name",
            "full name",
            "github",
            "last name",
            "legal name",
            "linkedin profile",
            "phone",
            "portfolio",
            "resume",
            "right to work",
            "sponsor",
            "surname",
            "visa",
            "work authorization",
            "website",
        )
        if any(marker in text for text in field_texts for marker in application_markers):
            return False
        matched = sum(1 for text in field_texts if any(marker in text for marker in search_markers))
        return matched >= max(1, len(fields) - 1)

    async def _is_auth_wall(self, page) -> bool:
        try:
            parsed = urlparse(page.url)
            host = parsed.hostname or ""
            path = parsed.path.lower()
            if "accounts.google.com" in host:
                return True
            if any(token in path for token in ("/login", "/signin", "/sign-in", "/applicant/login")):
                return True
            if "mypage" in host and "applicant" in path:
                return True
            return await page.evaluate(
                r"""
                () => {
                  const text = document.body?.innerText?.toLowerCase() || '';
                  const fields = Array.from(document.querySelectorAll('input')).filter((el) => {
                    if (!el || !el.isConnected) return false;
                    if (el.closest('[hidden], [inert], [aria-hidden="true"]')) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                  });
                  const names = fields.map((el) => `${el.name || ''} ${el.id || ''} ${el.type || ''}`.toLowerCase()).join(' ');
                  return /(sign in|log in|login|create account|forgot email|forgot password)/.test(text)
                    && /(password|identifier|username|email)/.test(names)
                    && !/(resume|cv|cover letter|application questions)/.test(text);
                }
                """
            )
        except Exception:
            return False

    async def _click_with_optional_popup(self, page, element) -> None:
        popup = None
        try:
            async with page.expect_popup(timeout=1200) as popup_info:
                await element.click()
            popup = await popup_info.value
        except Exception:
            popup = None

        if popup is None:
            return
        try:
            await popup.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        popup_url = getattr(popup, "url", "") or ""
        if popup_url and popup_url != "about:blank":
            await self._goto(page, popup_url)
        try:
            await popup.close()
        except Exception:
            pass

    async def _activate_apply_trigger(self, page, element, job_url: str) -> bool:
        await element.scroll_into_view_if_needed()
        href = await element.get_attribute("href")
        target = await element.get_attribute("target")
        if href and target != "_blank":
            destination = urljoin(page.url, href)
            parsed = urlparse(destination)
            if parsed.scheme in {"http", "https"}:
                await self._goto(page, destination)
            else:
                await self._click_with_optional_popup(page, element)
        else:
            await self._click_with_optional_popup(page, element)
        await self._cleanup_extra_pages(page)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception as exc:
            log.debug("open_application_load_state_timeout", url=getattr(page, "url", job_url), error=str(exc))
        await self._settle(page, 250)
        if await self._is_auth_wall(page):
            raise NotImplementedError("Sign-in is required before the application form can be automated.")
        if await self._select_manual_entry(page):
            await self._settle(page, 500)
        return bool(await self._form_context(page))

    async def _find_apply_trigger_by_text(self, page):
        selectors = ("button", "a", "[role='button']", "input[type='button']", "input[type='submit']")
        for selector in selectors:
            locator = page.locator(selector)
            try:
                count = await locator.count()
            except Exception:
                continue
            for index in range(min(count, 100)):
                candidate = locator.nth(index)
                try:
                    if not await candidate.is_visible():
                        continue
                    text = " ".join((await candidate.inner_text()).split()).lower()
                except Exception:
                    try:
                        text = " ".join(str((await candidate.get_attribute("value")) or "").split()).lower()
                    except Exception:
                        continue
                if not text:
                    continue
                if any(keyword in text for keyword in self.apply_keywords):
                    return candidate
        return None

    async def _select_manual_entry(self, page) -> bool:
        try:
            body_text = ((await page.locator("body").inner_text()) or "").lower()
        except Exception:
            body_text = ""
        if not any(term in body_text for term in self.manual_terms + self.non_manual_terms):
            return False
        selectors = ("button", "a", "label", "[role='button']", "[role='radio']", "[role='option']", "input[type='radio']")
        for selector in selectors:
            locator = page.locator(selector)
            try:
                count = await locator.count()
            except Exception:
                continue
            for index in range(min(count, 100)):
                candidate = locator.nth(index)
                try:
                    if not await candidate.is_visible():
                        continue
                except Exception:
                    continue
                label = await self._manual_candidate_text(page, candidate, selector)
                if not label:
                    continue
                if any(term in label for term in self.non_manual_terms):
                    continue
                if not any(term in label for term in self.manual_terms):
                    continue
                try:
                    await candidate.scroll_into_view_if_needed()
                    await candidate.click(force=True)
                    await self._settle(page, 200)
                except Exception as exc:
                    log.debug("manual_entry_click_failed", selector=selector, label=label, error=str(exc))
                    continue
                for continue_selector in self.continue_selectors:
                    try:
                        next_button = page.locator(continue_selector).last
                        if await next_button.count() and await next_button.is_visible():
                            await next_button.click()
                            await self._settle(page, 400)
                            break
                    except Exception:
                        continue
                log.info("manual_entry_selected", url=getattr(page, "url", ""), label=label)
                return True
        return False

    async def _manual_candidate_text(self, page, candidate, selector: str) -> str:
        if selector == "input[type='radio']":
            try:
                return " ".join(
                    (
                        await candidate.evaluate(
                            """(el) => {
                                const text = (node) => (node?.innerText || node?.textContent || '').replace(/\\s+/g, ' ').trim();
                                const wrapping = el.closest('label');
                                if (wrapping && text(wrapping)) return text(wrapping);
                                if (el.id) {
                                    const byFor = document.querySelector(`label[for="${el.id}"]`);
                                    if (byFor && text(byFor)) return text(byFor);
                                }
                                return el.value || '';
                            }"""
                        )
                    ).split()
                ).lower()
            except Exception:
                return ""
        try:
            return " ".join((await candidate.inner_text()).split()).lower()
        except Exception:
            return ""

    def _timing_disabled(self) -> bool:
        return bool(config.BROWSER_TEST_MODE or os.environ.get("PYTEST_CURRENT_TEST"))

    async def _settle(self, page, milliseconds: int) -> None:
        if self._timing_disabled() or milliseconds <= 0:
            return
        await page.wait_for_timeout(milliseconds)

    async def _cleanup_extra_pages(self, page) -> None:
        context = getattr(page, "context", None)
        if context is None:
            return
        try:
            pages = list(context.pages)
        except Exception:
            return
        for extra in pages:
            if extra is page:
                continue
            try:
                url = getattr(extra, "url", "") or ""
                if url == "about:blank" or not url:
                    await extra.close()
            except Exception:
                continue

    async def _verify_field_value(self, context, field: FormField, expected_value) -> None:
        if not field.selector:
            return
        locator = context.locator(field.selector).first
        expected = str(expected_value or "").strip()
        try:
            if field.field_type == "checkbox":
                actual = await locator.is_checked()
                if actual == bool(expected_value):
                    return
            elif field.field_type == "radio":
                selector = self._option_selector(field, expected)
                if selector and await context.locator(selector).first.is_checked():
                    return
            elif field.field_type == "select":
                actual = (await locator.input_value()).strip()
                if actual.lower() == expected.lower():
                    return
            elif field.role in {"combobox", "listbox"} and field.tag != "select":
                actual = " ".join((await locator.inner_text()).split())
                if expected and expected.lower() in actual.lower():
                    return
            else:
                actual = (await locator.input_value()).strip()
                if actual == expected:
                    return
        except Exception as exc:
            log.warning("field_value_verification_failed", field=field.label_text or field.name, error=str(exc))
            return
        log.warning("field_value_mismatch", field=field.label_text or field.name, expected=expected)

    async def _advance_multi_step_form(self, context) -> bool:
        for selector in self.continue_selectors:
            candidate = context.locator(selector).last
            if not await candidate.count():
                continue
            try:
                await human_delay()
                await candidate.scroll_into_view_if_needed()
                await candidate.click()
                log.info("advanced_multi_step_form", selector=selector)
                return True
            except Exception:
                continue
        return False

    async def _page_title(self, page) -> str | None:
        generic_titles = {"jobs", "job details", "careers", "step forward with us"}
        for selector in ["h1", "[data-qa='job-title']"]:
            try:
                locator = page.locator(selector).first
                if await locator.count():
                    text = " ".join((await locator.inner_text()).split())
                    if text and text.lower() not in generic_titles:
                        return text[:160]
            except Exception:
                continue
        try:
            title = " ".join((await page.title()).split())
            if title and title.lower() not in generic_titles:
                return title[:160]
        except Exception:
            pass
        for selector in ["[class*='title']"]:
            try:
                locator = page.locator(selector).first
                if await locator.count():
                    text = " ".join((await locator.inner_text()).split())
                    if text and text.lower() not in generic_titles:
                        return text[:160]
            except Exception:
                continue
        return None

    def _looks_like_direct_job_url(self, url: str) -> bool:
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        lowered = [part.lower() for part in parts]
        if (parsed.hostname or "").lower().endswith("workable.com") and "j" in lowered and len(parts) > lowered.index("j") + 1:
            return True
        if "results" in lowered and len(parts) > lowered.index("results") + 1:
            return True
        if "listing" in lowered and len(parts) > lowered.index("listing") + 1:
            return True
        if "job" in lowered and len(parts) > lowered.index("job") + 1:
            return True
        if "jobs" in lowered and len(parts) > lowered.index("jobs") + 1:
            return True
        return False


class SinglePageBrowserFormAdapter(BrowserFormAdapter):
    async def list_jobs(self, page, career_url: str) -> list[JobListing]:
        await self._goto(page, career_url)
        return [JobListing(url=career_url, title_preview=await self._page_title(page), ext_id=career_url.rstrip("/").split("/")[-1])]
