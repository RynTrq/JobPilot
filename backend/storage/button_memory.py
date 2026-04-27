"""
Button Name Memory System - Stores custom button names for future form navigation.

Users can register alternative button names (e.g., "Apply now" for submit button)
to avoid "neither next nor submit button found" errors in future runs on similar sites.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from backend.config import DATA_DIR

log = structlog.get_logger()


@dataclass
class ButtonNameMapping:
    """Maps a normalized button purpose to alternative text values."""
    button_type: str  # "submit" or "next"
    alternative_names: list[str]  # list of text values to match
    first_seen: str  # ISO timestamp
    last_updated: str  # ISO timestamp
    sites: list[str]  # domains where this alternative was seen


class ButtonNameMemory:
    """Persistent memory for alternative button names across job application sites."""

    def __init__(self, storage_path: Path | None = None):
        self.storage_path = storage_path or (DATA_DIR / "button_name_memory.json")
        self._cache: dict[str, ButtonNameMapping] = {}
        self._load()

    def _load(self) -> None:
        """Load button names from disk into memory cache."""
        if not self.storage_path.exists():
            self._cache = {}
            return
        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
            self._cache = {}
            for key, mapping_dict in data.items():
                self._cache[key] = ButtonNameMapping(
                    button_type=mapping_dict["button_type"],
                    alternative_names=mapping_dict.get("alternative_names", []),
                    first_seen=mapping_dict.get("first_seen", ""),
                    last_updated=mapping_dict.get("last_updated", ""),
                    sites=mapping_dict.get("sites", []),
                )
            log.info("button_name_memory_loaded", count=len(self._cache))
        except Exception as exc:
            log.error("button_name_memory_load_failed", error=str(exc))
            self._cache = {}

    def _save(self) -> None:
        """Persist button names to disk."""
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                key: {
                    "button_type": mapping.button_type,
                    "alternative_names": mapping.alternative_names,
                    "first_seen": mapping.first_seen,
                    "last_updated": mapping.last_updated,
                    "sites": mapping.sites,
                }
                for key, mapping in self._cache.items()
            }
            self.storage_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            log.info("button_name_memory_saved", count=len(self._cache))
        except Exception as exc:
            log.error("button_name_memory_save_failed", error=str(exc))

    def register_button_name(
        self,
        button_type: str,
        alternative_name: str,
        domain: str | None = None,
    ) -> None:
        """Register an alternative button name for submit or next buttons."""
        if button_type not in ("submit", "next"):
            raise ValueError(f"button_type must be 'submit' or 'next', got {button_type}")

        alternative_name_lower = alternative_name.lower().strip()
        if not alternative_name_lower:
            return

        key = f"{button_type}:{alternative_name_lower}"
        now = datetime.now(timezone.utc).isoformat()

        if key in self._cache:
            mapping = self._cache[key]
            mapping.last_updated = now
            if domain and domain not in mapping.sites:
                mapping.sites.append(domain)
        else:
            self._cache[key] = ButtonNameMapping(
                button_type=button_type,
                alternative_names=[alternative_name_lower],
                first_seen=now,
                last_updated=now,
                sites=[domain] if domain else [],
            )
        self._save()
        log.info("button_name_registered", button_type=button_type, name=alternative_name_lower, domain=domain)

    def get_alternatives(self, button_type: str) -> list[str]:
        """Get all registered alternative names for a button type."""
        alternatives = []
        for mapping in self._cache.values():
            if mapping.button_type == button_type:
                alternatives.extend(mapping.alternative_names)
        return alternatives

    def get_all_mappings(self) -> list[dict[str, Any]]:
        """Get all registered button name mappings."""
        return [
            {
                "button_type": mapping.button_type,
                "alternative_names": mapping.alternative_names,
                "first_seen": mapping.first_seen,
                "last_updated": mapping.last_updated,
                "sites": mapping.sites,
            }
            for mapping in sorted(self._cache.values(), key=lambda m: m.last_updated, reverse=True)
        ]

    def clear_memory(self) -> None:
        """Clear all registered button names."""
        self._cache = {}
        if self.storage_path.exists():
            self.storage_path.unlink()
        log.info("button_name_memory_cleared")
