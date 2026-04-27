from __future__ import annotations

from typing import Any

COVER_FILE_LABELS = ("cover letter", "cover", "letter of interest", "motivation letter")
COVER_TEXTAREA_LABELS = ("cover letter", "cover note", "why are you applying", "tell us about yourself")
OPTIONAL_DOC_LABELS = ("optional documents", "additional documents")


def cover_letter_requested(fields: list[Any]) -> bool:
    """Return True only when the application form explicitly asks for cover-letter content."""
    for field in fields:
        label = _value(field, "label_text", "label", default="").lower()
        field_type = _value(field, "field_type", "type", default="").lower()
        surrounding = _value(field, "surrounding_text", "context", default="").lower()
        char_limit = _value(field, "char_limit", default=None)

        if field_type == "file" and any(token in label for token in COVER_FILE_LABELS):
            return True

        if field_type == "textarea" and any(token in label for token in COVER_TEXTAREA_LABELS):
            if "tell us about yourself" not in label:
                return True
            # `char_limit` can be None / int / str depending on the scraper. Coerce safely;
            # missing or unparseable values default to "no enforced limit", which is the
            # same as "long enough to require a cover letter".
            try:
                limit_value = int(char_limit) if char_limit is not None else None
            except (TypeError, ValueError):
                limit_value = None
            if limit_value is None or limit_value > 800:
                return True

        if any(token in label for token in OPTIONAL_DOC_LABELS) and "cover" in surrounding:
            return True

    return False


def _value(field: Any, *names: str, default=None):
    for name in names:
        if isinstance(field, dict) and name in field:
            return field[name]
        if hasattr(field, name):
            return getattr(field, name)
    return default
