from __future__ import annotations

from pathlib import Path

from backend.contracts import PromptMetadata


REQUIRED_HEADER_KEYS = {
    "id",
    "specialist",
    "default_model",
    "temperature",
    "max_tokens",
    "schema",
    "updated",
}


class PromptRegistryError(ValueError):
    pass


def parse_prompt_metadata(path: Path) -> PromptMetadata:
    headers = _read_header(path)
    missing = sorted(REQUIRED_HEADER_KEYS - headers.keys())
    if missing:
        raise PromptRegistryError(f"{path} missing prompt header keys: {', '.join(missing)}")
    return PromptMetadata(
        prompt_id=headers["id"],
        specialist=headers["specialist"],
        default_model=headers["default_model"],
        temperature=float(headers["temperature"]),
        max_tokens=int(headers["max_tokens"]),
        schema_ref=headers["schema"],
        updated=headers["updated"],
        eval_path=headers.get("eval"),
    )


def discover_prompt_metadata(prompt_dir: Path) -> list[PromptMetadata]:
    return [parse_prompt_metadata(path) for path in sorted(prompt_dir.glob("*.txt"))]


def _read_header(path: Path) -> dict[str, str]:
    headers: dict[str, str] = {}
    saw_header = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            if saw_header:
                break
            continue
        if not line.startswith("#"):
            break
        saw_header = True
        key, sep, value = line[1:].partition(":")
        if sep:
            headers[key.strip()] = value.strip()
    return headers
