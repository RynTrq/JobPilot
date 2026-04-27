from __future__ import annotations

from difflib import SequenceMatcher
import re

import structlog
from bs4 import BeautifulSoup

log = structlog.get_logger()

READY_SELECTORS = (
    "[data-automation-id='jobPostingDescription']",
    "[data-automation-id='jobPostingDescriptionText']",
    "[data-automation-id='jobPostingDescriptionContainer']",
    "[data-testid*='job-description']",
    "[data-qa*='job-description']",
    "[class*='job-description']",
    "[id*='job-description']",
    "[class*='jobDescription']",
    "[id*='jobDescription']",
    "article",
    "main",
)

SPINNER_SELECTORS = (
    ".loading",
    ".spinner",
    "[aria-busy='true']",
    ".skeleton",
    "[data-loading='true']",
    ".content-loading",
    "[data-testid='loading-spinner']",
    ".job-loading",
    "#loading",
    ".page-loader",
)


def _normalize_line(line: str) -> str:
    return " ".join(str(line or "").replace("\xa0", " ").split()).strip()


def _normalize_text(value: str) -> str:
    return "\n".join(line for line in (_normalize_line(part) for part in str(value or "").splitlines()) if line)


async def load_page_completely(page, url: str | None = None) -> None:
    if url:
        try:
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
        except Exception as exc:
            log.warning("domcontentloaded_timeout_continuing", url=url, error=str(exc))

    try:
        await page.wait_for_selector(", ".join(SPINNER_SELECTORS), state="hidden", timeout=1500)
    except Exception:
        pass

    previous_scroll_height = 0
    for _ in range(4):
        try:
            current_scroll_height = await page.evaluate("document.body ? document.body.scrollHeight : 0")
        except Exception as exc:
            log.debug("scroll_height_read_failed", error=str(exc))
            break
        if not current_scroll_height or current_scroll_height == previous_scroll_height:
            break
        previous_scroll_height = current_scroll_height
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(350)
        except Exception as exc:
            log.debug("lazy_load_scroll_failed", error=str(exc))
            break

    try:
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(150)
    except Exception as exc:
        log.debug("scroll_reset_failed", error=str(exc))

    try:
        await page.wait_for_load_state("networkidle", timeout=2500)
    except Exception as exc:
        log.debug("post_scroll_networkidle_timeout", error=str(exc))

    try:
        body_text = await page.inner_text("body")
        log.info("page_load_complete", word_count=len(body_text.split()))
    except Exception as exc:
        log.warning("page_load_complete_word_count_failed", error=str(exc))


async def extract_text_method_inner_text(page) -> str:
    try:
        return await page.inner_text("body")
    except Exception as exc:
        log.warning("extract_text_method_inner_text_failed", error=str(exc))
        return ""


async def extract_text_method_js_clone(page) -> str:
    try:
        return await page.evaluate(
            """() => {
                const clone = document.body.cloneNode(true);
                const unwanted = clone.querySelectorAll('script, style, noscript, meta, link, svg, canvas, [aria-hidden="true"]');
                unwanted.forEach(el => el.remove());
                return clone.innerText || clone.textContent || '';
            }"""
        )
    except Exception as exc:
        log.warning("extract_text_method_js_clone_failed", error=str(exc))
        return ""


async def extract_text_method_tree_walker(page) -> str:
    try:
        return await page.evaluate(
            """() => {
                const results = [];
                const walker = document.createTreeWalker(
                    document.body,
                    NodeFilter.SHOW_TEXT,
                    {
                        acceptNode: function(node) {
                            const parent = node.parentElement;
                            if (!parent) return NodeFilter.FILTER_REJECT;
                            const tag = parent.tagName.toLowerCase();
                            if (['script', 'style', 'noscript', 'svg', 'canvas'].includes(tag)) {
                                return NodeFilter.FILTER_REJECT;
                            }
                            const computed = window.getComputedStyle(parent);
                            if (computed.display === 'none' || computed.visibility === 'hidden') {
                                return NodeFilter.FILTER_REJECT;
                            }
                            const text = node.textContent.trim();
                            if (text.length === 0) return NodeFilter.FILTER_REJECT;
                            return NodeFilter.FILTER_ACCEPT;
                        }
                    }
                );
                let node;
                while ((node = walker.nextNode())) {
                    const text = node.textContent.trim();
                    if (text.length > 0) results.push(text);
                }
                return results.join('\\n');
            }"""
        )
    except Exception as exc:
        log.warning("extract_text_method_tree_walker_failed", error=str(exc))
        return ""


async def extract_text_method_shadow_dom(page) -> str:
    try:
        return await page.evaluate(
            """() => {
                function extractFromNode(root) {
                    const texts = [];
                    function walk(node) {
                        if (!node) return;
                        if (node.nodeType === Node.TEXT_NODE) {
                            const t = node.textContent.trim();
                            if (t.length > 0) texts.push(t);
                            return;
                        }
                        if (node.nodeType === Node.ELEMENT_NODE) {
                            const tag = node.tagName.toLowerCase();
                            if (['script', 'style', 'noscript'].includes(tag)) return;
                            if (node.shadowRoot) {
                                walk(node.shadowRoot);
                            }
                            for (const child of node.childNodes) {
                                walk(child);
                            }
                        } else if (node.nodeType === Node.DOCUMENT_FRAGMENT_NODE) {
                            for (const child of node.childNodes) {
                                walk(child);
                            }
                        }
                    }
                    walk(root);
                    return texts.join('\\n');
                }
                return extractFromNode(document.body);
            }"""
        )
    except Exception as exc:
        log.warning("extract_text_method_shadow_dom_failed", error=str(exc))
        return ""


async def extract_text_method_all_frames(page) -> str:
    texts: list[str] = []
    for frame in page.frames:
        try:
            text = await frame.inner_text("body")
        except Exception as exc:
            log.debug("frame_inner_text_failed", frame_url=getattr(frame, "url", ""), error=str(exc))
            continue
        if len(text.strip()) > 50:
            texts.append(text)
    return "\n\n--- FRAME BOUNDARY ---\n\n".join(texts)


def _line_overlap_ratio(candidate: str, primary: str) -> float:
    candidate_lines = [_normalize_line(line) for line in candidate.splitlines() if _normalize_line(line)]
    if not candidate_lines:
        return 1.0
    primary_lines = {_normalize_line(line) for line in primary.splitlines() if _normalize_line(line)}
    if not primary_lines:
        return 0.0
    matched = sum(1 for line in candidate_lines if line in primary_lines)
    return matched / max(1, len(candidate_lines))


def _token_similarity(candidate: str, primary: str) -> float:
    candidate_tokens = re.findall(r"\w+", candidate.lower())
    primary_tokens = re.findall(r"\w+", primary.lower())
    if not candidate_tokens:
        return 1.0
    if not primary_tokens:
        return 0.0
    candidate_set = set(candidate_tokens)
    primary_set = set(primary_tokens)
    return len(candidate_set & primary_set) / max(1, len(candidate_set))


def _text_similarity(candidate: str, primary: str) -> float:
    if not candidate:
        return 1.0
    if not primary:
        return 0.0
    return SequenceMatcher(None, candidate, primary).ratio()


def merge_extraction_results(texts: list[str]) -> str:
    candidates = [_normalize_text(text) for text in texts if text and len(text.strip()) >= 20]
    if not candidates:
        return ""
    candidates.sort(key=len, reverse=True)
    primary = candidates[0]
    for candidate in candidates[1:]:
        normalized_primary = _normalize_text(primary)
        normalized_candidate = _normalize_text(candidate)
        if not normalized_candidate:
            continue
        if normalized_candidate in normalized_primary:
            continue
        if _line_overlap_ratio(normalized_candidate, normalized_primary) >= 0.80:
            continue
        token_similarity = _token_similarity(normalized_candidate, normalized_primary)
        if token_similarity >= 0.92:
            continue
        if len(normalized_candidate) < len(normalized_primary) * 0.85 and token_similarity >= 0.75:
            continue
        if _text_similarity(normalized_candidate, normalized_primary) >= 0.88:
            continue
        primary = f"{primary}\n\n{candidate}"
    return primary


def minimal_safe_clean(text: str) -> str:
    cleaned = str(text or "").replace("\x00", "")
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = (
        cleaned.replace("\u200b", "")
        .replace("\u200c", "")
        .replace("\u200d", "")
        .replace("\u00a0", " ")
        .replace("\ufeff", "")
    )
    cleaned = re.sub(r"\n{4,}", "\n\n\n", cleaned)
    cleaned = re.sub(r"[ \t]{3,}", "  ", cleaned)
    return cleaned.strip()


def looks_like_access_interstitial(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    if not normalized:
        return False
    markers = [
        "verify you are not a bot",
        "verification successful",
        "waiting for",
        "ray id",
        "performance and security by cloudflare",
        "cf challenge",
        "attention required",
        "please enable javascript and cookies to continue",
    ]
    return sum(marker in normalized for marker in markers) >= 2


def _extract_text_with_soup(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "meta", "link", "svg", "canvas"]):
        tag.decompose()
    body = soup.body or soup
    return minimal_safe_clean(body.get_text("\n", strip=True))


def extract_text_from_html(html: str) -> str:
    if not html:
        return ""
    return _extract_text_with_soup(html)


async def _collect_page_texts(page) -> list[str]:
    return [
        await extract_text_method_inner_text(page),
        await extract_text_method_js_clone(page),
        await extract_text_method_tree_walker(page),
        await extract_text_method_shadow_dom(page),
        await extract_text_method_all_frames(page),
    ]


async def extract_text_from_page(page, html: str | None = None, ready_selectors: tuple[str, ...] | None = None) -> str:
    try:
        await page.wait_for_selector(", ".join(ready_selectors or READY_SELECTORS), state="attached", timeout=2500)
    except Exception:
        pass

    await load_page_completely(page)
    texts = await _collect_page_texts(page)
    for name, text in zip(
        ["inner_text", "js_clone", "tree_walker", "shadow_dom", "all_frames"],
        texts,
        strict=False,
    ):
        log.info("page_text_method_result", method=name, word_count=len(text.split()), char_count=len(text))

    merged = minimal_safe_clean(merge_extraction_results(texts))
    if looks_like_access_interstitial(merged):
        log.warning("page_text_blocked_by_interstitial", preview=merged[:240])
        return ""
    if len(merged.strip()) >= 50:
        return merged

    page_html = html if html is not None else await page.content()
    fallback = extract_text_from_html(page_html)
    if looks_like_access_interstitial(fallback):
        log.warning("page_html_blocked_by_interstitial", preview=fallback[:240])
        return ""
    return fallback if len(fallback) > len(merged) else merged
