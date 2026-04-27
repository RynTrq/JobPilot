from __future__ import annotations

from typing import Any

RESUME_FILE_LABELS = ("resume", "cv", "curriculum vitae", "résumé")
RESUME_TEXTAREA_LABELS = ("resume", "cv", "paste your resume")


def resume_requested(fields: list[Any]) -> bool:
    """Return True only when the application form has a slot for the resume/CV."""
    for field in fields:
        label = _value(field, "label_text", "label", default="").lower()
        name = _value(field, "name", default="").lower()
        field_type = _value(field, "field_type", "type", default="").lower()
        haystack = f"{label} {name}"

        if field_type == "file" and any(token in haystack for token in RESUME_FILE_LABELS):
            return True
        if field_type == "textarea" and any(token in haystack for token in RESUME_TEXTAREA_LABELS):
            return True
    return False


def _value(field: Any, *names: str, default=None):
    for name in names:
        if isinstance(field, dict) and name in field:
            return field[name]
        if hasattr(field, name):
            return getattr(field, name)
    return default
