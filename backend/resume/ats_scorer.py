from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


ATS_SCORE_FLOOR = 85

WEIGHTS = {
    "keyword_overlap": 0.25,
    "section_presence": 0.20,
    "format_compliance": 0.15,
    "length": 0.10,
    "text_extractability": 0.10,
    "banned_pattern_absence": 0.10,
    "hyperlink_validity": 0.05,
    "font_compliance": 0.05,
}

REQUIRED_SECTION_GROUPS = [
    ("contact", ("email", "phone", "linkedin", "github")),
    ("education", ("education", "university", "college", "degree")),
    ("skills", ("skills", "technologies", "technical skills")),
    ("experience_or_projects", ("experience", "projects", "work")),
    ("achievements", ("achievements", "awards", "publications", "honors")),
]

BANNED_PATTERNS = [
    r"[\U0001f300-\U0001faff]",
    r"[★◆■●]",
    r"data:image/",
    r"https?://\S{80,}",
]


@dataclass(frozen=True)
class AtsScore:
    score: int
    passed: bool
    floor: int
    components: dict[str, float]
    failures: list[str]

    def model_dump(self) -> dict:
        return {
            "score": self.score,
            "passed": self.passed,
            "floor": self.floor,
            "components": self.components,
            "failures": self.failures,
        }


def score_resume_text(
    pdf_text: str,
    *,
    latex_text: str = "",
    keywords_exact: list[str] | None = None,
    allowed_link_domains: set[str] | None = None,
    page_count: int = 1,
) -> AtsScore:
    text = _normalise_text(pdf_text)
    latex = _normalise_text(latex_text)
    components = {
        "keyword_overlap": _keyword_overlap(text, keywords_exact or []),
        "section_presence": _section_presence(text),
        "format_compliance": _format_compliance(pdf_text, latex_text),
        "length": 1.0 if page_count == 1 else 0.0,
        "text_extractability": _text_extractability(text, latex),
        "banned_pattern_absence": _banned_pattern_absence(pdf_text),
        "hyperlink_validity": _hyperlink_validity(pdf_text, allowed_link_domains or set()),
        "font_compliance": _font_compliance(latex_text),
    }
    total = int(round(sum(components[name] * weight * 100 for name, weight in WEIGHTS.items())))
    failures = [name for name, value in components.items() if value < 1.0]
    return AtsScore(score=total, passed=total >= ATS_SCORE_FLOOR, floor=ATS_SCORE_FLOOR, components=components, failures=failures)


def score_resume_pdf(
    pdf_path: Path,
    *,
    latex_text: str = "",
    keywords_exact: list[str] | None = None,
    allowed_link_domains: set[str] | None = None,
) -> AtsScore:
    text = extract_pdf_text(pdf_path)
    return score_resume_text(
        text,
        latex_text=latex_text,
        keywords_exact=keywords_exact,
        allowed_link_domains=allowed_link_domains,
        page_count=_pdf_page_count(pdf_path),
    )


def extract_pdf_text(pdf_path: Path) -> str:
    try:
        with tempfile.NamedTemporaryFile(suffix=".txt") as handle:
            subprocess.run(["pdftotext", str(pdf_path), handle.name], check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return Path(handle.name).read_text(encoding="utf-8", errors="replace")
    except Exception:
        try:
            from pdfminer.high_level import extract_text

            return str(extract_text(str(pdf_path)) or "")
        except Exception:
            return ""


def _keyword_overlap(text: str, keywords: list[str]) -> float:
    cleaned_keywords = [_normalise_text(item) for item in keywords if _normalise_text(item)]
    if not cleaned_keywords:
        return 1.0
    hits = sum(1 for keyword in cleaned_keywords if keyword in text)
    ratio = hits / len(cleaned_keywords)
    if ratio >= 0.6:
        return 1.0
    if ratio >= 0.4:
        return 0.5
    return 0.0


def _section_presence(text: str) -> float:
    present = 0
    for _name, tokens in REQUIRED_SECTION_GROUPS:
        if any(token in text for token in tokens):
            present += 1
    return present / len(REQUIRED_SECTION_GROUPS)


def _format_compliance(pdf_text: str, latex_text: str) -> float:
    lowered = (pdf_text + "\n" + latex_text).lower()
    if any(token in lowered for token in ("includegraphics", "\\begin{tabular}", "\\multicolumn", "two column")):
        return 0.0
    return 1.0


def _text_extractability(text: str, latex: str) -> float:
    if not latex:
        return 1.0 if len(text) >= 200 else 0.5
    latex_words = {word for word in _tokens(latex) if len(word) > 2}
    if not latex_words:
        return 1.0
    text_words = set(_tokens(text))
    return min(1.0, len(latex_words & text_words) / max(len(latex_words), 1) / 0.95)


def _banned_pattern_absence(text: str) -> float:
    return 0.0 if any(re.search(pattern, text) for pattern in BANNED_PATTERNS) else 1.0


def _hyperlink_validity(text: str, allowed_domains: set[str]) -> float:
    links = re.findall(r"https?://[^\s)]+", text)
    if not links:
        return 1.0
    if not allowed_domains:
        return 0.0
    allowed = {domain.lower().removeprefix("www.") for domain in allowed_domains}
    for link in links:
        host = (urlparse(link).hostname or "").lower().removeprefix("www.")
        if host not in allowed:
            return 0.0
    return 1.0


def _font_compliance(latex_text: str) -> float:
    lowered = latex_text.lower()
    disallowed = ("fontspec", "helvet", "times", "mathptmx", "newtxtext", "sourcesans")
    if any(token in lowered for token in disallowed):
        return 0.0
    return 1.0


def _pdf_page_count(pdf_path: Path) -> int:
    data = pdf_path.read_bytes()
    return max(1, len(re.findall(rb"/Type\s*/Page\b", data)))


def _normalise_text(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9+.#/ -]+", " ", str(text or "").lower()).split())


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9+.#/]+", str(text or "").lower())
