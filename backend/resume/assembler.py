from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from backend.config import TEMPLATE_DIR


LATEX_REPLACEMENTS = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def latex_escape(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, str):
        return "".join(LATEX_REPLACEMENTS.get(ch, ch) for ch in value)
    if isinstance(value, list):
        return [latex_escape(item) for item in value]
    if isinstance(value, dict):
        return {key: latex_escape(val) for key, val in value.items()}
    return value


class ResumeAssembler:
    def __init__(self, template_path: Path | None = None):
        self.template_path = template_path or TEMPLATE_DIR / "resume" / "resume.tex.jinja"
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
        template = self.env.get_template(self.template_path.name)
        return template.render(**latex_escape(context))

    def render_to_file(self, context: dict[str, Any], out_path: Path) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(self.render(context))
        return out_path
