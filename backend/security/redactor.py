from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RedactionFinding:
    kind: str
    value_hash: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class RedactionResult:
    text: str
    findings: tuple[RedactionFinding, ...]


_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("aadhaar", re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b", re.IGNORECASE)),
    ("pan", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    ("passport_labeled", re.compile(r"\bpassport(?:\s+(?:number|no\.?))?\s*[:#-]?\s*[A-Z0-9]{6,12}\b", re.IGNORECASE)),
    ("dob_labeled", re.compile(r"\b(?:dob|date\s+of\s+birth)\s*[:#-]?\s*\d{1,4}[/-]\d{1,2}[/-]\d{1,4}\b", re.IGNORECASE)),
)

_SENSITIVE_LABELS = re.compile(
    r"\b(?:aadhaar|aadhar|pan(?:\s+card)?|ssn|social\s+security|passport|date\s+of\s+birth|dob)\b",
    re.IGNORECASE,
)


def detect_sensitive(text: str | None) -> tuple[RedactionFinding, ...]:
    """Detect sensitive PII without returning the raw value."""
    if not text:
        return ()
    findings: list[RedactionFinding] = []
    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            findings.append(_finding(kind, match.group(0), match.start(), match.end()))
    for match in _SENSITIVE_LABELS.finditer(text):
        findings.append(_finding("sensitive_label", match.group(0), match.start(), match.end()))
    return tuple(_dedupe(findings))


def contains_sensitive(*texts: str | None) -> bool:
    return any(detect_sensitive(text) for text in texts if text)


def redact_text(text: str | None) -> RedactionResult:
    if not text:
        return RedactionResult(text or "", ())
    findings = detect_sensitive(text)
    if not findings:
        return RedactionResult(text, ())
    redacted = text
    for finding in sorted(findings, key=lambda item: item.start, reverse=True):
        redacted = redacted[: finding.start] + f"[REDACTED:{finding.kind}]" + redacted[finding.end :]
    return RedactionResult(redacted, findings)


def _finding(kind: str, value: str, start: int, end: int) -> RedactionFinding:
    digest = hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return RedactionFinding(kind=kind, value_hash=digest, start=start, end=end)


def _dedupe(findings: list[RedactionFinding]) -> list[RedactionFinding]:
    seen: set[tuple[str, int, int]] = set()
    unique: list[RedactionFinding] = []
    for finding in sorted(findings, key=lambda item: (item.start, -(item.end - item.start))):
        key = (finding.kind, finding.start, finding.end)
        if key in seen:
            continue
        if any(_overlaps(finding, selected) for selected in unique):
            continue
        seen.add(key)
        unique.append(finding)
    return unique


def _overlaps(left: RedactionFinding, right: RedactionFinding) -> bool:
    return left.start < right.end and right.start < left.end
