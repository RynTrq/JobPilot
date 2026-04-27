from __future__ import annotations

# Compatibility wrapper: the repository's concrete matcher implementation lives in
# backend.form.field_matcher. This module preserves the prompt-expected import path
# without changing the existing runtime behavior.

from backend.form.field_matcher import FieldMatcher, INDEX_PHRASES, MatchResult, resolve_path

__all__ = ["FieldMatcher", "INDEX_PHRASES", "MatchResult", "resolve_path"]
