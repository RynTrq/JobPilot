from __future__ import annotations

import re
from typing import Any

from jinja2 import Template

from backend.config import ROOT_DIR
from backend.storage.ground_truth import GroundTruthStore

HOOK_BANNED_PHRASES = [
    "leader in",
    "industry leader",
    "leadership in",
    "known for its leadership",
    "team known for",
    "recognized as a leader",
    "cutting-edge",
    "cutting edge",
    "innovative solutions",
    "best workplace",
    "mission-driven leader",
    "world-class",
    "world class",
    "best-in-class",
]

KNOWN_TECH_TERMS = [
    "Java", "Spring", "Spring Boot", "Spring Cloud", "Hibernate",
    "Kubernetes", "K8s", "Docker", "Helm", "Istio",
    "Ruby", "Ruby on Rails", "Rails", "Sinatra",
    "PHP", "Laravel", "Symfony", "WordPress",
    "Scala", "Kotlin", "Rust", "Elixir", "Erlang", "Haskell", "Clojure", "OCaml",
    ".NET", "ASP.NET", "Entity Framework",
    "Django", "Flask",
    "Hadoop", "Spark", "Kafka", "Redis", "RabbitMQ",
    "MongoDB", "MySQL", "Cassandra", "DynamoDB", "Oracle", "Snowflake", "BigQuery",
    "Terraform", "Pulumi", "Ansible", "Chef", "Puppet",
    "Jenkins", "CircleCI", "GitLab CI", "Bazel",
    "gRPC", "Apache Kafka", "Nginx", "Apache",
    "Kibana", "Elasticsearch", "Logstash", "Grafana", "Prometheus",
    "Airflow", "Databricks",
    "AWS", "GCP", "Azure", "EC2", "Lambda", "CloudFormation", "CloudFront",
    "Angular", "Vue", "Svelte",
    "GraphQL",
    "Microservices", "Serverless",
]


def _forbidden_tech_terms(candidate_evidence_block: str) -> list[str]:
    lower = candidate_evidence_block.lower()
    forbidden: list[str] = []
    for term in KNOWN_TECH_TERMS:
        pattern = r"\b" + re.escape(term.lower()) + r"\b"
        if not re.search(pattern, lower):
            forbidden.append(term)
    return forbidden


def _allowed_tech_from_evidence(candidate_evidence_block: str) -> str:
    tokens: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"\(stack:([^)]*)\)", candidate_evidence_block):
        for token in match.group(1).split(","):
            clean = token.strip().rstrip(".")
            key = clean.lower()
            if clean and key not in seen:
                seen.add(key)
                tokens.append(clean)
    return ", ".join(tokens)

class CoverLetterWriter:
    def __init__(self, generator):
        self.generator = generator
        self.ground_truth = GroundTruthStore().read_if_exists()

    async def paragraph(self, prompt: str, context: str, max_tokens: int = 220, temperature: float = 0.3) -> str:
        return await self.generator.complete(prompt, context, max_tokens=max_tokens, temperature=temperature)

    async def build(self, job_meta: dict[str, Any], candidate_evidence_block: str, earliest_start_date: str) -> dict[str, str]:
        personal = self.ground_truth.get("personal", {})
        candidate_name = personal.get("full_name", "The candidate")
        candidate_one_liner = self.ground_truth.get("profile_statement", "")
        hook_result = await self.generator.grounded_answer(
            question="Write the opening cover-letter paragraph using only the provided evidence.",
            source_blocks={
                "candidate_name": candidate_name,
                "candidate_one_liner": candidate_one_liner,
                "job_meta": job_meta,
            },
            min_confidence=0.88,
            max_tokens=180,
        )
        paragraph_hook = str(hook_result.get("answer_text") or "").strip()
        if not paragraph_hook or hook_result.get("unknown_flag") or any(phrase in paragraph_hook.lower() for phrase in HOOK_BANNED_PHRASES):
            paragraph_hook = (
                f"I am applying for the {job_meta.get('role_title', 'Software Engineer')} role at "
                f"{job_meta.get('company', 'your company')} because the work described aligns closely with my recent software projects and academic focus."
            )
        fit_close_result = await self.generator.grounded_answer(
            question="Write the fit and close paragraphs for this cover letter using only the provided evidence.",
            source_blocks={
                "candidate_name": candidate_name,
                "earliest_start_date": earliest_start_date,
                "job_meta": job_meta,
                "candidate_evidence_block": candidate_evidence_block,
                "allowed_technologies": _allowed_tech_from_evidence(candidate_evidence_block) or [],
            },
            min_confidence=0.9,
            max_tokens=320,
        )
        paragraph_fit_close = str(fit_close_result.get("answer_text") or "").strip()
        if _is_degenerate_fit_close(paragraph_fit_close):
            paragraph_fit_close = _fallback_fit_close(
                role_title=job_meta.get("role_title", "the role"),
                company=job_meta.get("company", ""),
                candidate_evidence_block=candidate_evidence_block,
                earliest_start_date=earliest_start_date,
            )
        return {"paragraph_hook": paragraph_hook, "paragraph_fit_close": paragraph_fit_close}


def _is_degenerate_fit_close(text: str) -> bool:
    if not text:
        return True
    words = len(text.split())
    if words < 80:
        return True
    if "available to start" not in text.lower() and "earliest start" not in text.lower():
        return True
    return False


def _fallback_fit_close(role_title: str, company: str, candidate_evidence_block: str, earliest_start_date: str) -> str:
    bullets = [_clean_evidence(line) for line in candidate_evidence_block.splitlines() if line.strip().startswith("-")]
    evidence = [b for b in bullets if b][:2]
    at_company = f" at {company}" if company else ""
    first = f"The {role_title} position{at_company} maps directly to the work I have been doing over the past year."
    if len(evidence) >= 2:
        support = (
            f"I recently built {evidence[0]}, which gave me hands-on practice with the full-stack workflows this role centres on. "
            f"In parallel, I shipped {evidence[1]}, sharpening the backend and data-handling instincts the team would rely on."
        )
    elif evidence:
        support = (
            f"I recently built {evidence[0]}, which gave me hands-on practice with the full-stack workflows this role centres on, "
            f"alongside coursework in systems, databases, and applied software engineering at IIIT Delhi."
        )
    else:
        support = (
            "My final-year coursework at IIIT Delhi in systems, databases, and applied software engineering, "
            "paired with a portfolio of full-stack and data-pipeline projects, has prepared me to contribute quickly."
        )
    fit = f"{first} {support}"
    start_phrase = (
        f"I am available to start as early as {earliest_start_date}."
        if earliest_start_date
        else "I am available to start immediately."
    )
    close = f"I would welcome a conversation about how this background maps to the {role_title} role. {start_phrase}"
    return f"{fit}\n\n{close}"


def _clean_evidence(line: str) -> str:
    text = line.lstrip("- ").strip()
    idx = text.find(" (stack:")
    if idx != -1:
        text = text[:idx]
    text = text.rstrip(".").strip()
    if not text:
        return ""
    return text[0].lower() + text[1:]


def _prompt(name: str, **context: Any) -> str:
    path = ROOT_DIR / "backend" / "models" / "prompts" / name
    return Template(path.read_text()).render(**context)
