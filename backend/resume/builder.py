from __future__ import annotations

import json
from typing import Any

from jinja2 import Template

from backend.config import DATA_DIR, ROOT_DIR
from backend.models.generator import sentence_count, word_count
from backend.resume.bullet_picker import BulletPicker
from backend.specialists.jd_extractor import JDExtractor
from backend.storage.candidate_profile import CandidateProfileStore
from backend.storage.ground_truth import GroundTruthStore

PROMPT_DIR = ROOT_DIR / "backend" / "models" / "prompts"

SKILL_KEYS = {"languages", "frameworks", "tools", "databases", "concepts", "coursework"}
BANNED_PROFILE_WORDS = {"passionate", "hardworking", "quick learner", "motivated", "driven"}


class ResumeContextBuilder:
    def __init__(self, encoder, generator):
        self.encoder = encoder
        self.generator = generator
        self.picker = BulletPicker(encoder)
        self.ground_truth = _safe_mapping(GroundTruthStore().read_if_exists())
        self.candidate_profile = _safe_mapping(CandidateProfileStore().read_if_exists())

    async def build(self, job_description: str, projects_library: dict[str, Any] | None = None) -> dict[str, Any]:
        defaults = _load_defaults()
        job_meta = await self.extract_job_meta(job_description)
        library = projects_library or _load_projects_library()
        selection = self.picker.select_projects(library, job_description)
        tagline = await self.tagline(job_meta)
        profile = await self.profile(job_meta, selection.projects_top3)
        skills = await self.skills(job_meta)
        projects_top3 = await self.maybe_add_project_bullet(job_meta, selection.projects_top3)
        return {
            "tagline": tagline or defaults["tagline"],
            "profile_paragraph": profile or defaults["profile_paragraph"],
            "skills": {key: skills[key] for key in ["languages", "frameworks", "tools", "databases", "concepts"]},
            "coursework": skills["coursework"],
            "personal": self._personal_context(),
            "education_entries": self._education_entries(),
            "experience_entries": self._experience_entries(),
            "awards_entries": self._awards_entries(),
            "projects_top3": projects_top3,
            "projects_other3": selection.projects_other3,
            "job_meta": job_meta,
        }

    async def extract_job_meta(self, job_description: str) -> dict[str, Any]:
        return (await JDExtractor(self.generator).extract(job_description)).model_dump(mode="json")

    async def tagline(self, job_meta: dict[str, Any]) -> str:
        text = await self.generator.complete_text_validated(
            "Write only the requested resume headline.",
            _render_prompt("tagline.txt", job_meta_json=json.dumps(job_meta, separators=(",", ":"))),
            default_key="tagline",
            max_tokens=40,
            temperature=0.2,
            require_tagline=True,
            banned_filter=False,
        )
        return text if text.count(" | ") == 2 else str(_load_defaults().get("tagline", text or ""))

    async def profile(self, job_meta: dict[str, Any], projects_top3: list[dict[str, Any]]) -> str:
        text = await self.generator.complete_text_validated(
            "Write a grounded resume profile.",
            _render_prompt(
                "profile.txt",
                job_meta_json=json.dumps(job_meta, separators=(",", ":")),
                top_domain_tags_from_ground_truth=", ".join(job_meta.get("jd_domain_tags", [])),
                top_3_projects_one_liners="; ".join(project["one_line_summary"] for project in projects_top3),
            ),
            default_key="profile_paragraph",
            max_tokens=120,
            temperature=0.3,
            min_words=40,
            max_words=75,
        )
        if sentence_count(text) not in {2, 3} or any(word in text.lower() for word in BANNED_PROFILE_WORDS):
            return str(_load_defaults().get("profile_paragraph", text))
        return text

    async def skills(self, job_meta: dict[str, Any]) -> dict[str, list[str]]:
        parsed = await self.generator.complete_json(
            "Select resume skills from fixed pools only.",
            _render_prompt("skills.txt", job_meta_json=json.dumps(job_meta, separators=(",", ":"))),
            default_key="skills",
            required_keys=SKILL_KEYS,
            max_tokens=400,
            temperature=0.1,
        )
        return validate_skills(parsed)

    async def maybe_add_project_bullet(self, job_meta: dict[str, Any], projects_top3: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(projects_top3) < 3:
            return projects_top3
        chosen_text = " ".join(" ".join(project.get("bullets", [])) for project in projects_top3).lower()
        missing = next((req for req in job_meta.get("top_requirements", []) if str(req).lower() not in chosen_text), None)
        if not missing:
            return projects_top3
        project = projects_top3[2]
        bullet = await self.generator.complete_text_validated(
            "Write a truthful project bullet or SKIP.",
            _render_prompt(
                "project_bullet.txt",
                project_name=project["name"],
                tech_stack=", ".join(project.get("tech_stack", [])),
                summary=project.get("one_line_summary", ""),
                existing_bullets="\n".join(project.get("bullets", [])),
                requirement=missing,
            ),
            default_key="project_bullet",
            max_tokens=60,
            temperature=0.2,
            min_words=15,
            max_words=30,
            project_tech_stack=project.get("tech_stack", []),
            require_action_verb=True,
        )
        if bullet != "SKIP" and 15 <= word_count(bullet) <= 30:
            project["bullets"] = [*project.get("bullets", [])[:2], bullet]
        return projects_top3

    def _personal_context(self) -> dict[str, Any]:
        personal = _safe_mapping(self.ground_truth.get("personal"))
        return {
            "full_name": personal.get("full_name", ""),
            "email": personal.get("email", ""),
            "phone": personal.get("phone_e164", ""),
            "linkedin_url": personal.get("linkedin_url", ""),
            "github_url": personal.get("github_url", ""),
            "portfolio_url": personal.get("portfolio_url", ""),
            "location_line": self._location_line(personal),
        }

    def _location_line(self, personal: dict[str, Any]) -> str:
        parts = [personal.get("location_city"), personal.get("location_country")]
        return ", ".join(part for part in parts if part)

    def _education_entries(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for education in self.ground_truth.get("education", []):
            edu = _safe_mapping(education)
            if not edu:
                continue
            entries.append(
                {
                    "degree_line": " in ".join(part for part in [edu.get("degree"), edu.get("field")] if part),
                    "date_range": _date_range(edu.get("start_month_year"), edu.get("end_month_year")),
                    "institution": edu.get("institution", ""),
                    "location": self._location_line(_safe_mapping(self.ground_truth.get("personal"))),
                    "details": _education_details(edu),
                }
            )
        return entries

    def _experience_entries(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for experience in self.ground_truth.get("experience", []):
            exp = _safe_mapping(experience)
            if not exp:
                continue
            bullets: list[str] = []
            summary = str(exp.get("summary_1line") or "").strip()
            if summary:
                bullets.append(summary)
            entries.append(
                {
                    "title": exp.get("title", ""),
                    "date_range": _date_range(exp.get("start_month_year"), exp.get("end_month_year")),
                    "company": exp.get("company", ""),
                    "location": exp.get("location", ""),
                    "bullets": bullets,
                }
            )
        return entries

    def _awards_entries(self) -> list[str]:
        entries: list[str] = []
        awards = self.candidate_profile.get("awards_certifications", [])
        if not isinstance(awards, list):
            awards = []
        for item in awards:
            entry = _safe_mapping(item)
            title = str(entry.get("title") or "").strip()
            issuer = str(entry.get("issuer") or "").strip()
            description = str(entry.get("description") or "").strip()
            if not title:
                continue
            line = title
            if issuer:
                line += f" ({issuer})"
            if description:
                line += f" --- {description}"
            entries.append(line)
            if len(entries) >= 4:
                break
        return entries


def validate_skills(parsed: dict[str, Any]) -> dict[str, list[str]]:
    defaults = _load_defaults().get("skills", {})
    if not isinstance(defaults, dict):
        defaults = {}
    out: dict[str, list[str]] = {}
    for key in SKILL_KEYS:
        values = parsed.get(key)
        fallback = defaults.get(key, [])
        if not isinstance(fallback, list):
            fallback = []
        out[key] = values if isinstance(values, list) and values else fallback
    for base in ["Python", "C/C++", "Java", "JavaScript/TypeScript"]:
        if base not in out["languages"]:
            out["languages"].append(base)
    for base in ["PostgreSQL", "MongoDB"]:
        if base not in out["databases"]:
            out["databases"].append(base)
    out["coursework"] = out["coursework"][:5]
    if len(out["coursework"]) < 3:
        fallback_course = defaults.get("coursework", [])
        out["coursework"] = fallback_course if isinstance(fallback_course, list) else []
    return out


def _date_range(start: str | None, end: str | None) -> str:
    month_names = {
        "01": "Jan",
        "02": "Feb",
        "03": "Mar",
        "04": "Apr",
        "05": "May",
        "06": "Jun",
        "07": "Jul",
        "08": "Aug",
        "09": "Sep",
        "10": "Oct",
        "11": "Nov",
        "12": "Dec",
    }

    def _format_month_year(value: str | None) -> str:
        text = str(value or "").strip()
        if len(text) >= 7 and text[4] == "-":
            return f"{month_names.get(text[5:7], text[5:7])} {text[:4]}"
        return text

    start_text = _format_month_year(start)
    end_text = _format_month_year(end)
    if start_text and end_text:
        return f"{start_text} -- {end_text}"
    return start_text or end_text


def _education_details(education: dict[str, Any]) -> list[str]:
    details: list[str] = []
    honors = education.get("honors") or []
    if honors:
        details.append("Honors: " + ", ".join(str(item) for item in honors[:3]))

    # Enforce user policy: never include bachelor CGPA/GPA unless explicitly approved.
    gpa = education.get("gpa")
    degree = str(education.get("degree") or "").lower()
    is_bachelor = any(token in degree for token in ["bachelor", "btech", "b.tech", "b.s", "b.a"])
    if gpa and not is_bachelor:
        try:
            numeric = float(str(gpa).split("/")[0].strip())
            if numeric >= 7.0:
                details.append(f"GPA: {gpa}")
        except (ValueError, IndexError):
            pass
    return details


def _safe_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _render_prompt(name: str, **context: Any) -> str:
    return Template((PROMPT_DIR / name).read_text()).render(**context)


def _load_defaults() -> dict[str, Any]:
    path = DATA_DIR / "defaults.json"
    if not path.exists():
        raise FileNotFoundError(
            f"missing defaults file at {path}. Run `python scripts/seed_defaults.py` "
            "or copy templates/defaults.json into data/."
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"defaults.json is not valid JSON: {exc}") from exc


def _load_projects_library() -> dict[str, Any]:
    path = DATA_DIR / "projects_library.json"
    if not path.exists():
        raise FileNotFoundError(f"missing {path}; run scripts/build_projects_library.py")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"projects_library.json is not valid JSON: {exc}") from exc
