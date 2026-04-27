from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import structlog

log = structlog.get_logger()


def compile_latex(tex_path: Path, out_dir: Path) -> Path:
    if shutil.which("pdflatex") is None:
        raise RuntimeError("pdflatex not found. Install BasicTeX and required packages before compiling PDFs.")
    if not tex_path.exists():
        raise RuntimeError(f"LaTeX source file does not exist: {tex_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    tex_dir = tex_path.parent
    for _ in range(2):
        try:
            result = subprocess.run(
                ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-output-directory", str(out_dir), str(tex_path)],
                capture_output=True,
                timeout=60,
                check=False,
                cwd=str(tex_dir),
            )
        except subprocess.TimeoutExpired as exc:
            log.error("pdflatex_timeout", tex_path=str(tex_path), out_dir=str(out_dir), timeout_seconds=60)
            raise RuntimeError(f"pdflatex timed out after 60 seconds for {tex_path}") from exc
        if result.returncode != 0:
            output = result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace")
            log.error("pdflatex_failed", tex_path=str(tex_path), out_dir=str(out_dir), returncode=result.returncode, stderr_preview=output[-2000:])
            raise RuntimeError(output)
    pdf_path = out_dir / f"{tex_path.stem}.pdf"
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        log_tail = _read_log_tail(out_dir / f"{tex_path.stem}.log")
        log.error("pdflatex_missing_pdf", tex_path=str(tex_path), pdf_path=str(pdf_path), log_tail=log_tail)
        raise RuntimeError(f"pdflatex did not produce {pdf_path}. Log tail:\n{log_tail}")
    log.info("pdflatex_succeeded", tex_path=str(tex_path), pdf_path=str(pdf_path))
    return pdf_path


def _read_log_tail(log_path: Path, lines: int = 50) -> str:
    if not log_path.exists():
        return "LaTeX log file not found."
    content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])
