from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from backend.config import TEMPLATE_DIR
from backend.resume.assembler import latex_escape


class CoverLetterAssembler:
    def __init__(self, template_path: Path | None = None):
        self.template_path = template_path or TEMPLATE_DIR / "cover_letter" / "cover.tex.jinja"
        self.env = Environment(
            loader=FileSystemLoader(str(self.template_path.parent)),
            undefined=StrictUndefined,
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
            comment_start_string="((#",
            comment_end_string="#))",
        )

    def render(self, context: dict[str, Any]) -> str:
        return self.env.get_template(self.template_path.name).render(**latex_escape(context))

    def render_to_file(self, context: dict[str, Any], out_path: Path) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(self.render(context))
        return out_path
