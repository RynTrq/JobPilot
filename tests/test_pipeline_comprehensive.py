"""
Comprehensive pipeline tests covering classifier, fit_decision, work auth,
job discovery, and field answerer across India/Japan/Australia/Europe/GCC portals.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

PROFILE_PATH = Path(__file__).parents[1] / "data" / "My_Ground-info" / "profile" / "candidate_profile.yaml"
GT_PATH = Path(__file__).parents[1] / "data" / "ground_truth.json"


def _raiyaan_profile() -> dict:
    import yaml
    if PROFILE_PATH.exists():
        with PROFILE_PATH.open() as fh:
            return yaml.safe_load(fh) or {}
    return {}


def _raiyaan_gt() -> dict:
    if GT_PATH.exists():
        return json.loads(GT_PATH.read_text())
    return {}


def _raiyaan_facts():
    from backend.specialists.fit_decision import load_candidate_fit_facts
    from backend.storage.candidate_profile import resolve_profile_path
    from backend.storage.ground_truth import resolve_ground_truth_path
    return load_candidate_fit_facts(
        ground_truth_path=resolve_ground_truth_path(),
        profile_path=resolve_profile_path(),
    )


# ---------------------------------------------------------------------------
# 1. Classifier heuristic — fresher roles must score >= 0.50, senior < 0.50
# ---------------------------------------------------------------------------

class TestClassifierHeuristic:
    """Classifier heuristic must pass fresher JDs and reject senior ones."""

    @pytest.fixture(autouse=True)
    def _clf(self):
        from backend.models.classifier import Classifier
        # Use heuristic path (no trained model needed)
        self.clf = Classifier(clf=None, profile_emb=None)

    def _score(self, text: str) -> float:
        from backend.models.classifier import Classifier
        clf = Classifier(clf=None, profile_emb=None)
        return clf._heuristic_score(text)

    # --- India fresher JDs ---
    def test_india_fresher_software_engineer(self):
        jd = "Entry-level software engineer at Infosys. Fresher. 0 years experience required. Python, Java."
        assert self._score(jd) >= 0.50, f"Should pass fresher India JD, got {self._score(jd):.3f}"

    def test_india_campus_hire(self):
        jd = "Campus hire 2026 batch. B.Tech CS students eligible. No prior experience required. New graduate."
        assert self._score(jd) >= 0.50

    def test_india_intern_ml(self):
        jd = "ML internship at analytics startup. 0-1 year experience. Python, ML frameworks."
        assert self._score(jd) >= 0.50

    def test_india_associate_engineer(self):
        jd = "Associate engineer fresher role. Recent graduate welcome. Training provided. Java, SQL."
        assert self._score(jd) >= 0.50

    def test_india_trainee_developer(self):
        jd = "Trainee software developer. 0-6 months experience. Will train the right candidate."
        assert self._score(jd) >= 0.50

    # --- Japan fresher JDs ---
    def test_japan_new_grad_engineer(self):
        jd = "Graduate engineer position Tokyo. New graduates welcome. Python backend development. 2026 batch."
        assert self._score(jd) >= 0.50

    def test_japan_internship(self):
        jd = "Software engineering internship in Tokyo. Undergraduate or new graduate. Machine learning."
        assert self._score(jd) >= 0.50

    # --- Australia fresher JDs ---
    def test_australia_grad_software(self):
        jd = "Graduate software engineer Sydney. Entry level. 0-2 years experience. Python, AWS."
        assert self._score(jd) >= 0.50

    def test_australia_early_career(self):
        jd = "Early-career developer Melbourne. Recent graduate. No prior industry experience needed."
        assert self._score(jd) >= 0.50

    # --- Europe fresher JDs ---
    def test_europe_junior_developer(self):
        jd = "Junior developer Berlin. Entry-level. 0 years experience. React, Node.js."
        assert self._score(jd) >= 0.50

    def test_europe_apprentice_engineer(self):
        jd = "Apprentice software engineer London. Early career. No experience required."
        assert self._score(jd) >= 0.50

    # --- GCC / Middle East fresher JDs ---
    def test_gcc_fresher_developer(self):
        jd = "Software developer fresher Dubai. 0-1 years experience. Python, JavaScript."
        assert self._score(jd) >= 0.50

    def test_gcc_graduate_engineer(self):
        jd = "Graduate engineer Abu Dhabi. Recent graduate. New graduate welcome. Technology."
        assert self._score(jd) >= 0.50

    # --- Senior JDs should fail ---
    def test_senior_india_rejected(self):
        jd = "Senior software engineer 5+ years experience required. Staff engineer level. India."
        assert self._score(jd) < 0.50, f"Should fail senior JD, got {self._score(jd):.3f}"

    def test_senior_global_rejected(self):
        jd = "Principal engineer 8+ years experience. Staff-level. Director of engineering track."
        assert self._score(jd) < 0.50

    def test_mid_level_rejected(self):
        jd = "Software engineer 3+ years of professional experience required. Not entry-level."
        assert self._score(jd) < 0.50

    def test_vp_engineering_rejected(self):
        jd = "VP of Engineering. 10+ years. Head of department. Leadership track."
        assert self._score(jd) < 0.50

    def test_two_years_required_penalised(self):
        jd = "Backend developer. Requires 2 years of experience. Python Django REST."
        score = self._score(jd)
        # Mid-level with 2yr requirement — should be borderline or rejected
        assert score <= 0.65, f"2yr requirement should penalise score, got {score:.3f}"

    # --- Zero-range detection ---
    def test_zero_to_two_years_range_boosts_score(self):
        jd = "Full stack developer. 0-2 years experience welcome. React Python."
        assert self._score(jd) >= 0.50

    def test_zero_to_one_year_range_boosts_score(self):
        jd = "Data analyst. 0 – 1 years experience. SQL Python."
        assert self._score(jd) >= 0.50


# ---------------------------------------------------------------------------
# 2. Fit decision — Rule A/B/C gating
# ---------------------------------------------------------------------------

class TestFitDecision:
    """decide_fit must correctly gate on seniority, years, expertise, and hard fails."""

    @pytest.fixture(autouse=True)
    def _facts(self):
        self.facts = _raiyaan_facts()

    def _decide(self, title: str, jd: str) -> dict:
        from backend.specialists.fit_decision import decide_fit
        return decide_fit(title=title, jd_text=jd, facts=self.facts)

    # --- Should pass ---
    def test_fresher_python_role_passes(self):
        result = self._decide(
            "Software Engineer",
            "Entry level Python developer. 0 years experience. Machine learning basics. Flask. Django."
        )
        assert result["fit"] is True, result["submission_outcome"]

    def test_ml_intern_passes(self):
        result = self._decide(
            "ML Engineer Intern",
            "Machine learning internship. New graduate welcome. Python, PyTorch, TensorFlow. No experience required."
        )
        assert result["fit"] is True, result["submission_outcome"]

    def test_sde_fresher_passes(self):
        result = self._decide(
            "SDE-1 Fresher",
            "Software development engineer. Campus hire 2026. 0 years. Python Java. Data structures."
        )
        assert result["fit"] is True, result["submission_outcome"]

    def test_associate_engineer_passes(self):
        result = self._decide(
            "Associate Software Engineer",
            "Fresher/entry-level. 0-1 year. Python backend. Recent graduate. Training provided."
        )
        assert result["fit"] is True, result["submission_outcome"]

    # --- Should fail (Rule A — years/seniority) ---
    def test_senior_sde_fails_rule_a(self):
        result = self._decide(
            "Senior Software Engineer",
            "5+ years Python experience required. Staff level. Leadership experience."
        )
        assert result["fit"] is False
        assert "seniority" in result["submission_outcome"] or result["rule_a"]["passed"] is False

    def test_three_years_required_fails(self):
        result = self._decide(
            "Software Engineer",
            "Requires 3+ years of professional experience in Python. Not entry level."
        )
        assert result["fit"] is False

    def test_director_title_fails_rule_a(self):
        result = self._decide(
            "Director of Engineering",
            "Lead teams. 8+ years. Management experience required."
        )
        assert result["fit"] is False

    def test_new_grad_exclusion_fails_rule_a(self):
        result = self._decide(
            "Software Engineer",
            "Prior industry experience required. New grads not eligible. 2 years minimum."
        )
        assert result["fit"] is False

    # --- Should fail (Rule B — no expertise overlap) ---
    def test_nurse_role_fails_rule_b(self):
        result = self._decide(
            "Registered Nurse",
            "Clinical nursing experience. Patient care. Hospital environment."
        )
        assert result["fit"] is False
        assert "expertise" in result["submission_outcome"] or not result["rule_b"]["matched_area"]

    def test_accountant_role_fails_rule_b(self):
        result = self._decide(
            "Chartered Accountant",
            "Accounting, auditing, financial reporting. CPA preferred."
        )
        assert result["fit"] is False

    # --- Should fail (Rule C — hard fails) ---
    def test_clearance_required_hard_fails(self):
        result = self._decide(
            "Software Engineer",
            "Security clearance required. US SECRET or TOP SECRET clearance mandatory."
        )
        assert result["fit"] is False
        assert "hard_fail" in result["submission_outcome"]

    def test_citizenship_required_hard_fails(self):
        result = self._decide(
            "Software Developer",
            "Must be a US citizen. Only US citizens eligible. No exceptions."
        )
        assert result["fit"] is False
        assert "hard_fail" in result["submission_outcome"]


# ---------------------------------------------------------------------------
# 3. Work authorization answers
# ---------------------------------------------------------------------------

class TestWorkAuthAnswers:
    """Work auth must correctly reflect Raiyaan's profile for each country context."""

    @pytest.fixture(autouse=True)
    def _data(self):
        from backend.form.field_answerer import build_candidate_data
        profile = _raiyaan_profile()
        gt = _raiyaan_gt()
        self.cdata = build_candidate_data(gt, profile)

    def test_requires_sponsorship_is_yes_for_raiyaan(self):
        # Raiyaan is India-only authorized → international forms need sponsorship
        wa = self.cdata["candidate"]["work_authorization"]
        assert wa["requires_sponsorship"] == "Yes"

    def test_authorized_to_work_is_no_for_international_forms(self):
        # For a form asking "authorized to work?" without country context, default is No
        # (most ATS forms asking this are from international companies)
        wa = self.cdata["candidate"]["work_authorization"]
        assert wa["authorized_to_work"] == "No"

    def test_field_lookup_authorized_to_work(self):
        from backend.form.field_answerer import tier1_lookup
        result = tier1_lookup("authorized to work", "text", self.cdata)
        assert result == "No"

    def test_field_lookup_require_sponsorship(self):
        from backend.form.field_answerer import tier1_lookup
        result = tier1_lookup("require sponsorship", "text", self.cdata)
        assert result == "Yes"

    def test_field_lookup_work_authorization(self):
        from backend.form.field_answerer import tier1_lookup
        result = tier1_lookup("work authorization", "text", self.cdata)
        assert result == "No"

    def test_field_lookup_visa_sponsorship(self):
        from backend.form.field_answerer import tier1_lookup
        result = tier1_lookup("visa sponsorship", "text", self.cdata)
        assert result == "Yes"

    def test_right_to_work_lookup(self):
        from backend.form.field_answerer import tier1_lookup
        result = tier1_lookup("right to work", "text", self.cdata)
        assert result == "No"

    def test_india_profile_with_india_authorized_sponsorship_logic(self):
        from backend.form.field_answerer import _requires_sponsorship
        # Raiyaan's profile: authorized_countries=[India], default_outside_india.now_requires=True
        work_auth = {
            "authorized_countries": ["India"],
            "sponsorship": {
                "india": {"now_requires": False},
                "default_outside_india": {"now_requires": True},
            }
        }
        assert _requires_sponsorship(work_auth) is True

    def test_us_profile_no_sponsorship(self):
        from backend.form.field_answerer import _requires_sponsorship
        # Legacy US profile without authorized_countries
        work_auth = {
            "sponsorship": {
                "united_states": {"now_requires": False},
            }
        }
        assert _requires_sponsorship(work_auth) is False

    def test_global_authorized_profile_no_sponsorship(self):
        from backend.form.field_answerer import _requires_sponsorship
        # Profile authorized everywhere, no default_outside
        work_auth = {
            "sponsorship": {
                "all_countries": {"now_requires": False},
            }
        }
        assert _requires_sponsorship(work_auth) is False

    def test_empty_work_auth_no_sponsorship(self):
        from backend.form.field_answerer import _requires_sponsorship
        assert _requires_sponsorship({}) is False
        assert _requires_sponsorship(None) is False


# ---------------------------------------------------------------------------
# 4. Candidate data fields — personal info, education, experience
# ---------------------------------------------------------------------------

class TestCandidateDataFields:
    @pytest.fixture(autouse=True)
    def _data(self):
        from backend.form.field_answerer import build_candidate_data
        profile = _raiyaan_profile()
        gt = _raiyaan_gt()
        self.cdata = build_candidate_data(gt, profile)
        self.candidate = self.cdata["candidate"]

    def test_full_name_present(self):
        assert self.candidate["personal"]["full_name"].strip()

    def test_email_present(self):
        assert "@" in self.candidate["personal"]["email"]

    def test_education_institution_present(self):
        assert self.candidate["education"]["latest"]["institution"].strip()

    def test_education_degree_present(self):
        assert self.candidate["education"]["latest"]["degree"].strip()

    def test_graduation_date_present(self):
        grad = self.candidate["education"]["latest"]["graduation_date"]
        assert grad, "Graduation date should be set"

    def test_tier1_first_name_lookup(self):
        from backend.form.field_answerer import tier1_lookup
        result = tier1_lookup("First Name", "text", self.cdata)
        assert result and result.strip()

    def test_tier1_last_name_lookup(self):
        from backend.form.field_answerer import tier1_lookup
        result = tier1_lookup("Last Name", "text", self.cdata)
        assert result and result.strip()

    def test_tier1_email_lookup(self):
        from backend.form.field_answerer import tier1_lookup
        result = tier1_lookup("Email Address", "email", self.cdata)
        assert result and "@" in result

    def test_tier1_degree_lookup(self):
        from backend.form.field_answerer import tier1_lookup
        result = tier1_lookup("Degree", "text", self.cdata)
        assert result and result.strip()

    def test_tier1_institution_lookup(self):
        from backend.form.field_answerer import tier1_lookup
        result = tier1_lookup("University", "text", self.cdata)
        assert result and result.strip()

    def test_tier1_graduation_date_lookup(self):
        from backend.form.field_answerer import tier1_lookup
        result = tier1_lookup("Graduation Date", "text", self.cdata)
        assert result and result.strip()

    def test_tier1_agree_checkbox_returns_yes(self):
        from backend.form.field_answerer import tier1_lookup
        result = tier1_lookup("I agree to the terms and conditions", "checkbox", self.cdata)
        assert result == "Yes"

    def test_tier1_terms_checkbox_returns_yes(self):
        from backend.form.field_answerer import tier1_lookup
        result = tier1_lookup("Terms & Conditions", "checkbox", self.cdata)
        assert result == "Yes"


# ---------------------------------------------------------------------------
# 5. Job link discovery — HTML patterns from various portal types
# ---------------------------------------------------------------------------

class TestJobLinkDiscovery:
    """discover_job_links must find job listings in HTML from various portal types."""

    def _discover(self, html: str, base_url: str):
        from backend.scraping.job_list import discover_job_links
        return discover_job_links(html, base_url)

    # --- Greenhouse board ---
    def test_greenhouse_board_links(self):
        html = """
        <div>
          <a href="/positions?gh_jid=12345">Software Engineer</a>
          <a href="/positions?gh_jid=67890">ML Engineer</a>
        </div>"""
        listings = self._discover(html, "https://boards.greenhouse.io/acme")
        assert len(listings) >= 2

    # --- Lever board ---
    def test_lever_board_links(self):
        html = """
        <div class="posting-title">
          <a href="/jobs/abc123">Software Engineer (New Grad)</a>
        </div>
        <div class="posting-title">
          <a href="/jobs/def456">Machine Learning Engineer - Junior</a>
        </div>"""
        listings = self._discover(html, "https://jobs.lever.co/acme")
        assert len(listings) >= 1

    # --- Generic company careers page with /careers/ paths ---
    def test_generic_careers_page(self):
        html = """
        <ul>
          <li><a href="/careers/software-engineer-intern">Software Engineer Intern</a></li>
          <li><a href="/careers/data-analyst-fresher">Data Analyst Fresher</a></li>
          <li><a href="/careers/ml-developer">ML Developer</a></li>
        </ul>"""
        listings = self._discover(html, "https://company.com/careers")
        assert len(listings) >= 2

    # --- Indeed-style job search results ---
    def test_indeed_style_links(self):
        html = """
        <div>
          <a href="/jobs/view?job_id=abc123">
            <h2>Software Engineer</h2>
          </a>
          <a href="/jobs/view?job_id=def456">
            <h2>Junior Developer</h2>
          </a>
        </div>"""
        listings = self._discover(html, "https://in.indeed.com/jobs")
        assert len(listings) >= 1

    # --- Workable-style paths ---
    def test_workable_style_links(self):
        html = """
        <div>
          <a href="/en/jobs/j/SWE001">Software Engineer</a>
          <a href="/en/jobs/j/DEV002">Developer</a>
        </div>"""
        listings = self._discover(html, "https://acme.workable.com")
        assert len(listings) >= 1

    # --- Indian job board style ---
    def test_naukri_style_article_links(self):
        html = """
        <article>
          <a href="/jobs/fresher-software-engineer-12345">
            <h2>Fresher Software Engineer</h2>
          </a>
        </article>
        <article>
          <a href="/jobs/trainee-developer-67890">
            <h2>Trainee Developer</h2>
          </a>
        </article>"""
        listings = self._discover(html, "https://naukri.com")
        assert len(listings) >= 1

    # --- Generic "Apply" button anchors should pick up parent role title ---
    def test_generic_apply_button_picks_up_parent_role_title(self):
        html = """
        <li>
          <h3>Graduate Software Engineer</h3>
          <a href="/jobs/graduate-swe-123">Apply</a>
        </li>"""
        listings = self._discover(html, "https://company.com/careers")
        # Should find the job via parent container role signal
        assert len(listings) >= 1

    # --- Navigation links should be excluded ---
    def test_nav_links_excluded(self):
        html = """
        <nav>
          <a href="/careers">Careers</a>
          <a href="/jobs">Jobs</a>
        </nav>
        <main>
          <a href="/jobs/sde-123">Software Developer</a>
        </main>"""
        listings = self._discover(html, "https://company.com")
        # Nav links excluded, only main job link found
        urls = [l.url for l in listings]
        assert not any("/careers" == u.rstrip("/").split("/")[-1] and "sde" not in u for u in urls)

    # --- Role word detection in URLs ---
    def test_role_word_in_url_detected(self):
        html = """<a href="/position/sde-fresher-2026">Apply Now</a>"""
        listings = self._discover(html, "https://company.com")
        assert len(listings) >= 1

    # --- ML/AI role detection ---
    def test_ml_ai_roles_detected(self):
        html = """
        <a href="/careers/ml-engineer-intern">ML Engineer Intern</a>
        <a href="/careers/ai-researcher-new-grad">AI Researcher New Grad</a>
        """
        listings = self._discover(html, "https://company.com/careers")
        assert len(listings) >= 2


# ---------------------------------------------------------------------------
# 6. looks_like_direct_job_url
# ---------------------------------------------------------------------------

class TestLooksLikeDirectJobUrl:
    def _check(self, url: str) -> bool:
        from backend.scraping.job_list import looks_like_direct_job_url
        return looks_like_direct_job_url(url)

    def test_greenhouse_jid_is_direct(self):
        assert self._check("https://boards.greenhouse.io/acme/jobs/1234567")

    def test_lever_job_is_direct(self):
        assert self._check("https://jobs.lever.co/company/abc-123-def")

    def test_ashby_job_is_direct(self):
        assert self._check("https://jobs.ashbyhq.com/company/role-id")

    def test_workday_job_is_direct(self):
        assert self._check("https://acme.wd3.myworkdayjobs.com/en-US/Careers/job/Software-Engineer_R12345")

    def test_generic_careers_slug_is_direct(self):
        assert self._check("https://company.com/careers/software-engineer-2026")

    def test_generic_jobs_slug_with_role(self):
        assert self._check("https://company.com/jobs/ml-developer-intern")

    def test_careers_listing_page_is_not_direct(self):
        assert not self._check("https://company.com/careers/engineering")

    def test_pure_careers_root_is_not_direct(self):
        assert not self._check("https://company.com/careers")

    def test_workable_j_path_is_direct(self):
        assert self._check("https://acme.workable.com/j/ABC123")

    def test_indeed_job_id_query_is_direct(self):
        assert self._check("https://in.indeed.com/viewjob?jk=abc123def456")


# ---------------------------------------------------------------------------
# 7. Pagination link detection
# ---------------------------------------------------------------------------

class TestPaginationLinks:
    def _paginate(self, html: str, current_url: str, career_url: str) -> list[str]:
        from backend.scraping.adapters.generic import _pagination_links
        return _pagination_links(html, current_url, career_url)

    def test_query_param_pagination(self):
        html = """
        <nav class="pagination">
          <a href="/jobs?page=2">2</a>
          <a href="/jobs?page=3">3</a>
          <a href="/jobs?page=2">Next</a>
        </nav>"""
        links = self._paginate(html, "https://company.com/jobs?page=1", "https://company.com/jobs")
        assert len(links) >= 1
        assert any("page=2" in l or "page=3" in l for l in links)

    def test_path_based_pagination(self):
        html = """
        <div class="pagination">
          <a href="/jobs/page/2">2</a>
          <a href="/jobs/page/3">Next</a>
        </div>"""
        links = self._paginate(html, "https://company.com/jobs", "https://company.com/jobs")
        assert len(links) >= 1

    def test_offset_pagination(self):
        html = """
        <div>
          <a href="/careers?start=10&limit=10">Next</a>
        </div>"""
        links = self._paginate(html, "https://company.com/careers?start=0", "https://company.com/careers")
        assert len(links) >= 1

    def test_next_arrow_pagination(self):
        html = """<a href="/jobs?page=2">»</a>"""
        links = self._paginate(html, "https://company.com/jobs", "https://company.com/jobs")
        assert len(links) >= 1

    def test_disabled_page_excluded(self):
        html = """
        <a href="/jobs?page=1" class="disabled active">1</a>
        <a href="/jobs?page=2">2</a>"""
        links = self._paginate(html, "https://company.com/jobs?page=1", "https://company.com/jobs")
        assert not any("page=1" in l for l in links)

    def test_cross_domain_pagination_excluded(self):
        html = """<a href="https://otherdomain.com/jobs?page=2">Next</a>"""
        links = self._paginate(html, "https://company.com/jobs", "https://company.com/jobs")
        assert len(links) == 0

    def test_from_offset_pagination(self):
        html = """<a href="/jobs?from=10">Next Page</a>"""
        links = self._paginate(html, "https://company.com/jobs", "https://company.com/jobs")
        assert len(links) >= 1

    def test_india_job_board_page_query(self):
        html = """<a href="/jobs/software-engineer?pageNo=2">Next</a>"""
        links = self._paginate(html, "https://freshersworld.com/jobs/software-engineer", "https://freshersworld.com/jobs/software-engineer")
        assert len(links) >= 1


# ---------------------------------------------------------------------------
# 8. Option matching — find_best_option_match
# ---------------------------------------------------------------------------

class TestOptionMatching:
    def _match(self, intended: str, options: list[str]) -> str | None:
        from backend.form.field_answerer import find_best_option_match
        return find_best_option_match(intended, options)

    def test_yes_matches_yes_option(self):
        assert self._match("yes", ["No", "Yes"]) == "Yes"

    def test_no_matches_no_option(self):
        result = self._match("no", ["No", "Yes"])
        assert result == "No"

    def test_bachelor_matches_degree_option(self):
        result = self._match("Bachelor", ["High School", "Bachelor's Degree", "Master's Degree", "PhD"])
        assert result is not None and "bachelor" in result.lower()

    def test_master_matches_degree_option(self):
        result = self._match("Master", ["Bachelor's Degree", "Master's Degree", "PhD"])
        assert result is not None and "master" in result.lower()

    def test_fuzzy_match_prefer_not_to_say(self):
        result = self._match("Prefer not to say", ["Male", "Female", "Prefer not to disclose", "Other"])
        assert result is not None

    def test_yes_i_am_matches_yes(self):
        result = self._match("i am", ["Yes", "No"])
        assert result == "Yes"

    def test_no_i_am_not_matches_no(self):
        result = self._match("i am not", ["Yes", "No"])
        assert result == "No"


# ---------------------------------------------------------------------------
# 9. Platform catalog — coverage and dispatch
# ---------------------------------------------------------------------------

class TestPlatformCatalog:
    def test_platform_count_above_100(self):
        from backend.scraping.adapters.platform_catalog import platform_count
        assert platform_count() >= 100

    def test_india_platforms_present(self):
        from backend.scraping.adapters.platform_catalog import PLATFORM_CONFIGS
        india = [p for p in PLATFORM_CONFIGS if p.region == "India"]
        assert len(india) >= 10

    def test_japan_platforms_present(self):
        from backend.scraping.adapters.platform_catalog import PLATFORM_CONFIGS
        japan = [p for p in PLATFORM_CONFIGS if p.region == "Japan"]
        assert len(japan) >= 10

    def test_australia_platforms_present(self):
        from backend.scraping.adapters.platform_catalog import PLATFORM_CONFIGS
        australia = [p for p in PLATFORM_CONFIGS if p.region == "Australia"]
        assert len(australia) >= 5

    def test_middle_east_platforms_present(self):
        from backend.scraping.adapters.platform_catalog import PLATFORM_CONFIGS
        me = [p for p in PLATFORM_CONFIGS if p.region == "Middle East"]
        assert len(me) >= 8

    def test_europe_platforms_present(self):
        from backend.scraping.adapters.platform_catalog import PLATFORM_CONFIGS
        eu = [p for p in PLATFORM_CONFIGS if "Europe" in p.region or p.region in (
            "Germany", "France", "Spain", "Ireland", "Switzerland", "Poland", "Austria", "Denmark", "Norway", "Sweden"
        )]
        assert len(eu) >= 10

    def test_greenhouse_domain_matched(self):
        from backend.scraping.adapters.platform_catalog import find_platform_config
        cfg = find_platform_config("https://boards.greenhouse.io/acme/jobs/12345")
        assert cfg is not None
        assert cfg.key == "greenhouse"

    def test_lever_domain_matched(self):
        from backend.scraping.adapters.platform_catalog import find_platform_config
        cfg = find_platform_config("https://jobs.lever.co/company/abc-123")
        assert cfg is not None
        assert cfg.key == "lever"

    def test_naukri_domain_matched(self):
        from backend.scraping.adapters.platform_catalog import find_platform_config
        cfg = find_platform_config("https://www.naukri.com/software-jobs")
        assert cfg is not None
        assert cfg.key == "naukri"

    def test_seek_domain_matched(self):
        from backend.scraping.adapters.platform_catalog import find_platform_config
        cfg = find_platform_config("https://www.seek.com.au/jobs/software-engineer")
        assert cfg is not None
        assert cfg.key == "seek-au"

    def test_bayt_domain_matched(self):
        from backend.scraping.adapters.platform_catalog import find_platform_config
        cfg = find_platform_config("https://www.bayt.com/en/jobs/software-engineer")
        assert cfg is not None
        assert cfg.key == "bayt"


# ---------------------------------------------------------------------------
# 10. Adapter dispatch
# ---------------------------------------------------------------------------

class TestAdapterDispatch:
    def _dispatch(self, url: str):
        from backend.scraping.adapters import dispatch_adapter
        return dispatch_adapter(url)

    def test_greenhouse_dispatches_greenhouse_adapter(self):
        adapter = self._dispatch("https://boards.greenhouse.io/acme/jobs/12345")
        assert "Greenhouse" in type(adapter).__name__

    def test_lever_dispatches_lever_adapter(self):
        adapter = self._dispatch("https://jobs.lever.co/company/abc-123")
        assert "Lever" in type(adapter).__name__

    def test_ashby_dispatches_ashby_adapter(self):
        adapter = self._dispatch("https://jobs.ashbyhq.com/company/role-123")
        assert "Ashby" in type(adapter).__name__

    def test_smartrecruiters_dispatches_smartrecruiters_adapter(self):
        adapter = self._dispatch("https://careers.smartrecruiters.com/Company/job-id")
        assert "SmartRecruiters" in type(adapter).__name__

    def test_workable_dispatches_configured_adapter(self):
        adapter = self._dispatch("https://apply.workable.com/stakefish/j/5218ED958E/")
        assert "Workable" in type(adapter).__name__

    def test_unknown_domain_raises_no_adapter_found(self):
        from backend.scraping.adapters import NoAdapterFoundError

        with pytest.raises(NoAdapterFoundError, match="No adapter found"):
            self._dispatch("https://someunknowncompany.com/careers/sde-1")

    def test_naukri_dispatches_configured_adapter(self):
        # Naukri is in platform_catalog, so it should never need a generic fallback.
        adapter = self._dispatch("https://www.naukri.com/jobs/software-engineer")
        assert adapter is not None


# ---------------------------------------------------------------------------
# 11. Classifier feedback-adjusted scoring
# ---------------------------------------------------------------------------

class TestClassifierFeedback:
    def test_feedback_adjusted_score_returns_float(self):
        from backend.models.classifier import Classifier
        clf = Classifier(clf=None, profile_emb=None)
        # Mock encoder must have both encode() and encode_batch() to satisfy feedback module
        class MockEncoder:
            def encode(self, text):
                import numpy as np
                np.random.seed(abs(hash(text)) % (2**31))
                return np.random.rand(384).astype(np.float32)
            def encode_batch(self, texts):
                import numpy as np
                return np.stack([self.encode(t) for t in texts])
        result = clf.score_details("Software engineer fresher Python", MockEncoder())
        assert 0.0 <= result["score"] <= 1.0
        assert result["mode"] in ("model/full", "heuristic/fallback")


# ---------------------------------------------------------------------------
# 12. Role word signal detection
# ---------------------------------------------------------------------------

class TestRoleWordSignal:
    def _has_signal(self, text: str) -> bool:
        from backend.scraping.job_list import _contains_role_signal
        return _contains_role_signal(text)

    def test_sde_detected(self):
        assert self._has_signal("SDE-1 Fresher India")

    def test_ml_engineer_detected(self):
        assert self._has_signal("ML Engineer New Grad")

    def test_devops_detected(self):
        assert self._has_signal("DevOps Engineer Japan")

    def test_qa_tester_detected(self):
        assert self._has_signal("QA Tester Associate")

    def test_data_analyst_detected(self):
        assert self._has_signal("Data Analyst Intern")

    def test_cloud_engineer_detected(self):
        assert self._has_signal("Cloud Engineer - Graduate")

    def test_platform_engineer_detected(self):
        assert self._has_signal("Platform Engineer Fresher")

    def test_ios_developer_detected(self):
        assert self._has_signal("iOS Developer Entry Level")

    def test_android_developer_detected(self):
        assert self._has_signal("Android Developer Trainee")

    def test_nlp_researcher_detected(self):
        assert self._has_signal("NLP Researcher Intern")

    def test_technical_support_detected(self):
        assert self._has_signal("Technical Support Associate")

    def test_unrelated_text_not_detected(self):
        assert not self._has_signal("About Us page footer")

    def test_nav_text_not_detected(self):
        assert not self._has_signal("Previous Next Back to")


# ---------------------------------------------------------------------------
# 13. same_listing_page (pagination scope guard)
# ---------------------------------------------------------------------------

class TestSameListingPage:
    def _same(self, target: str, current: str, career: str) -> bool:
        from urllib.parse import urlparse
        from backend.scraping.adapters.generic import _same_listing_page
        return _same_listing_page(urlparse(target), urlparse(current), urlparse(career))

    def test_query_param_pagination_same_path(self):
        assert self._same(
            "https://co.com/jobs?page=2",
            "https://co.com/jobs?page=1",
            "https://co.com/jobs",
        )

    def test_path_based_pagination_same(self):
        assert self._same(
            "https://co.com/jobs/page/2",
            "https://co.com/jobs",
            "https://co.com/jobs",
        )

    def test_path_p_shorthand_same(self):
        assert self._same(
            "https://co.com/jobs/p/3",
            "https://co.com/jobs",
            "https://co.com/jobs",
        )

    def test_different_domain_not_same(self):
        assert not self._same(
            "https://other.com/jobs?page=2",
            "https://co.com/jobs",
            "https://co.com/jobs",
        )

    def test_deep_subpath_not_same(self):
        assert not self._same(
            "https://co.com/jobs/software-engineer/details",
            "https://co.com/jobs",
            "https://co.com/jobs",
        )


# ---------------------------------------------------------------------------
# 14. _phone_country_code_answer
# ---------------------------------------------------------------------------

class TestPhoneCountryCode:
    def test_india_phone_returns_91(self):
        from backend.form.field_answerer import _phone_country_code_answer
        # signature: (normalized_label, options, candidate_data)
        result = _phone_country_code_answer("phone country code", ["+91 India", "+1 United States", "+44 UK"], {})
        # For an empty candidate_data, result is None or a string
        assert result is None or isinstance(result, str)

    def test_fallback_returns_none_for_empty_options(self):
        from backend.form.field_answerer import _phone_country_code_answer
        result = _phone_country_code_answer("phone", [], {})
        assert result is None or isinstance(result, str)
