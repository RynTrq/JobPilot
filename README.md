# JobPilot

JobPilot is a local job-application assistant for macOS. It runs a Python FastAPI backend, drives a Chromium browser with Playwright, fills job application forms from your local profile data, generates tailored resume and cover-letter PDFs when needed, and exposes a native macOS menu-bar UI for starting runs, reviewing work, handling manual interventions, and viewing history.

JobPilot is designed to keep the human in control. Dry Run is the default mode. Real Submit must be enabled explicitly, and final review is enabled by default unless you opt into automatic final submission.

## What It Does

- Lists jobs from supported career pages and ATS platforms.
- Extracts job descriptions and checks listing liveness.
- Scores job fit with local classifier and specialist logic.
- Generates tailored resume and cover-letter artifacts when a form asks for them.
- Fills browser forms with profile, ground-truth, learned, and generated answers.
- Pauses for approval, unknown fields, login, CAPTCHA, validation blockers, and unsupported flows.
- Tracks separate History outcomes for Dry Run and Real Submits.
- Stores local state in SQLite and can optionally mirror canonical application records to MongoDB.

## Important Safety Behavior

Dry Run fills the form and stops before the final submit click. Before a dry run is marked complete, JobPilot audits required fields, including browser-required and asterisk-marked fields. If required values are missing, it tries to fill them or asks the user before declaring success.

Real Submit clicks the final submit button only when live submit is enabled. After clicking, JobPilot checks for confirmation pages and browser validation errors. If the browser flags missing or invalid fields, JobPilot retries filling once and asks the user when needed. A real submission is not marked successful while validation errors remain.

CAPTCHA handling is conservative. On the first CAPTCHA detection for a job, JobPilot waits, attempts one safe continue/next-style recovery, and checks again. If CAPTCHA appears again or persists, it raises manual takeover with: `CAPTCHA detected — Human intervention required.`

History is mode-specific. A green Dry Run entry cannot be dry-run again. A green Real Submit entry cannot be real-submitted again. Red entries remain actionable in their section.

## Project Layout

```text
backend/                    FastAPI server, orchestration, storage, scraping, form filling
backend/api/                REST and stream routes
backend/form/               Form filling, validation, CAPTCHA/manual takeover guards
backend/scraping/adapters/  ATS and browser-form adapters
backend/storage/            SQLite, MongoDB, learned answers, profile stores
menubar-app/JobPilot/       Swift menu-bar application
templates/                  LaTeX resume and cover-letter templates
data/                       Local database, browser profile, profile inputs, logs, generated outputs
```

## Requirements

- macOS
- Python 3.11
- `uv`
- Chromium installed through Playwright
- Xcode, if you want to build/run the menu-bar app
- A LaTeX distribution such as MacTeX or BasicTeX if you want PDF compilation

Install the Python environment:

```bash
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
playwright install chromium
```

## Configuration

Create `.env` in the repository root as needed. The defaults are intentionally cautious.

```env
JOBPILOT_HOST=127.0.0.1
JOBPILOT_PORT=8765

# Mode controls
DRY_RUN=1
AUTO_SUBMIT_WITHOUT_APPROVAL=0
LIVE_MODE=0

# Browser
BROWSER_PERSISTENT=1
BROWSER_USER_DATA_DIR=data/browser-profile
BROWSER_STEALTH_LEVEL=2

# Optional Mongo mirror
MONGO_URI=
MONGO_DB=jobpilot

# Model/provider behavior
GENERATOR_DISABLED=1
CLASSIFIER_THRESHOLD=0.65
CLASSIFIER_AUTO_PASS=0
```

Notes:

- `DRY_RUN=1` means final submit clicks are skipped.
- `DRY_RUN=0` enables real submit behavior, but the UI can also toggle this at runtime.
- `AUTO_SUBMIT_WITHOUT_APPROVAL=0` means final approval is required.
- `LIVE_MODE=1` shows/focuses the browser for easier observation. Warnings, missing fields, login, CAPTCHA, and errors still pause for user action.
- `GENERATOR_DISABLED=1` keeps LLM generation disabled unless you configure providers.

## Profile Data

JobPilot needs local ground-truth files before it can fill forms or generate job-specific documents. Create these files under `data/`:

```text
data/My_Ground-info/profile/candidate_profile.yaml
data/ground_truth.json
data/projects_library.json
data/bullet_library.json
data/defaults.json
```

`candidate_profile.yaml` is the policy-rich profile. It controls identity, work authorization, preferences, disclosure defaults, compensation rules, sensitive identifier policy, and automation guardrails. Save it exactly here:

```text
data/My_Ground-info/profile/candidate_profile.yaml
```

Use this shape:

```yaml
schema_version: 1
profile_id: your_name_or_handle
last_reviewed: 2026-04-27

identity:
  legal_name: Your Legal Name
  first_name: Your First Name
  last_name: Your Last Name
  preferred_name: Your Preferred Name
  email:
    primary: you@example.com
    backup: optional@example.com
  phone:
    country_code: "+1"
    number: "5551234567"
  location:
    city: Your City
    country: Your Country
  links:
    linkedin: https://www.linkedin.com/in/your-profile
    github: https://github.com/your-handle
    portfolio: https://your-site.example

sensitive_identifiers:
  handling: local_only_remove_before_sharing
  values:
    national_id:
      available: false
      number: null
      use_policy: never_without_manual_confirmation

education:
  - institution: University Name
    city: City
    country: Country
    degree: Bachelor of Science
    major: Computer Science
    graduation_date: 2026-05-15
    gpa:
      value: 3.8
      scale: 4.0
    relevant_coursework:
      - Data Structures and Algorithms
      - Operating Systems

awards_certifications:
  - type: certification
    title: Certification Name
    issuer: Issuer
    date_issued: 2025-01-01
    credential_url: null
    description: One sentence description.
    skills:
      - Python

work_authorization:
  authorized_countries:
    - United States
  sponsorship:
    united_states:
      now_requires: false
      future_requires: false
    default_outside_authorized_countries:
      now_requires: true
      future_requires: true

job_preferences:
  target_roles:
    - Software Engineer
    - Backend Engineer
  industries:
    - SaaS
    - Developer Tools
  locations:
    scope: worldwide
    willing_to_relocate: true
    modes:
      - remote
      - hybrid
      - onsite
  earliest_start_date: 2026-06-01
  employment_types:
    - full_time
  notice_period: 2 weeks

compensation_policy:
  target_salary:
    currency: USD
    amount: 100000
  reveal_policy: only_when_required

application_defaults:
  background_check_willingness: "Yes"
  pronouns: Prefer not to say
  disclosures:
    gender: Prefer not to say
    race: Prefer not to say
    veteran_status: I am not a protected veteran
    disability: I do not wish to answer

resume_strategy:
  objective: Accurate, ATS-friendly resume tailored only from verified facts.
  never_claim:
    - Skills or employment not present in these source files.

experience:
  - organization: Company or Lab
    title: Role Title
    location: City, Country
    start_date: 2025-01-01
    end_date: 2025-06-01
    responsibilities:
      - Built or improved a specific system.
    technologies:
      - Python
      - PostgreSQL

standard_form_answer_policy:
  company_interest: Use job/company facts; do not invent.
  role_interest: Tie role to verified projects and experience.
  about_self_default: Short professional summary from verified facts.

sensitive_answer_policy:
  salary_history: Prefer not to disclose unless legally required.
  date_of_birth: Ask before filling.

automation_rules:
  role_matching: Apply only to roles matching preferences or explicit user override.
  form_filling: Ask before submitting unknown required sensitive answers.
```

`ground_truth.json` is the structured resume/form evidence file. Save it exactly here:

```text
data/ground_truth.json
```

Use this shape:

```json
{
  "personal": {
    "full_name": "Your Legal Name",
    "preferred_name": "Your Preferred Name",
    "email": "you@example.com",
    "phone_e164": "+15551234567",
    "location_city": "Your City",
    "location_state": "Your State",
    "location_country": "Your Country",
    "citizenship": "Your Citizenship",
    "work_auth_us": "I am authorized to work in the US",
    "work_auth_eu": "I am not authorized to work in the EU",
    "linkedin_url": "https://www.linkedin.com/in/your-profile",
    "github_url": "https://github.com/your-handle",
    "portfolio_url": "https://your-site.example",
    "pronouns": "Prefer not to say"
  },
  "education": [
    {
      "institution": "University Name",
      "degree": "Bachelor of Science",
      "field": "Computer Science",
      "start_month_year": "2022-09",
      "end_month_year": "2026-05",
      "gpa": "3.8/4.0",
      "honors": [],
      "relevant_courses": [
        "Data Structures and Algorithms",
        "Operating Systems"
      ]
    }
  ],
  "experience": [
    {
      "id": "exp_company_role_2025",
      "title": "Software Engineering Intern",
      "company": "Company Name",
      "location": "City, Country",
      "start_month_year": "2025-01",
      "end_month_year": "2025-06",
      "summary_1line": "Built a concise, verified thing you actually did.",
      "tech_stack": ["Python", "FastAPI", "PostgreSQL"],
      "domains": ["backend", "data"]
    }
  ],
  "projects": [
    {
      "id": "project_slug",
      "title": "Project Name",
      "summary_1line": "One-line verified summary.",
      "url": "https://github.com/your-handle/project",
      "tech_stack": ["Python", "React"],
      "domains": ["backend", "frontend"]
    }
  ],
  "preferences": {
    "earliest_start_date": "2026-06-01",
    "notice_period_days": 14,
    "target_roles": ["Software Engineer", "Backend Engineer"],
    "minimum_salary": {
      "currency": "USD",
      "amount": 100000
    }
  },
  "profile_statement": "One short professional summary grounded only in verified facts."
}
```

`projects_library.json` is used by the resume builder to choose job-relevant projects. Save it exactly here:

```text
data/projects_library.json
```

Use this shape:

```json
{
  "projects": [
    {
      "id": "project_slug",
      "name": "Project Name",
      "start_month_year": "Jan 2025",
      "end_month_year": "Mar 2025",
      "github_url": "https://github.com/your-handle/project",
      "live_url": null,
      "one_line_summary": "A concise summary of what the project does.",
      "tech_stack": ["Python", "FastAPI", "PostgreSQL"],
      "domain_tags": ["backend", "api", "database"],
      "bullet_variants": [
        {
          "id": "project_slug_api_bullet",
          "tags": ["backend", "api"],
          "text": "Built a REST API with validated request models and persistent storage."
        }
      ]
    }
  ]
}
```

`bullet_library.json` gives the bullet picker a direct map of project/experience IDs to bullet variants. Save it exactly here:

```text
data/bullet_library.json
```

Use this shape:

```json
{
  "project_slug": [
    {
      "id": "project_slug_api_bullet",
      "text": "Built a REST API with validated request models and persistent storage.",
      "tags": ["backend", "api"],
      "impact_type": "verified"
    }
  ],
  "exp_company_role_2025": [
    {
      "id": "exp_company_role_pipeline",
      "text": "Improved a production data pipeline using verified tools and measurable scope.",
      "tags": ["data", "automation"],
      "impact_type": "verified"
    }
  ]
}
```

`defaults.json` provides fallbacks for resume generation and LLM-free operation. Save it exactly here:

```text
data/defaults.json
```

Use this shape:

```json
{
  "job_meta": {
    "company": "",
    "role_title": "Software Engineer",
    "top_requirements": ["software engineering", "backend development"],
    "why_company_fact": "",
    "jd_domain_tags": ["software", "backend"],
    "keywords_exact": ["Python", "React", "PostgreSQL"]
  },
  "tagline": "Software Engineer | Backend | Full Stack",
  "profile_paragraph": "Short verified professional summary.",
  "skills": {
    "languages": ["Python", "JavaScript", "Java"],
    "frameworks": ["FastAPI", "React"],
    "tools": ["Git", "Docker", "Linux"],
    "databases": ["PostgreSQL", "MongoDB"],
    "concepts": ["Data Structures", "REST API Design"],
    "coursework": ["Operating Systems", "Database Systems"]
  },
  "project_bullet": "Built a verified project component using the declared technology stack."
}
```

### A Practical Prompt To Generate The Files

You can ask an LLM to interview you and generate these files. Use a prompt like:

```text
I am setting up JobPilot, a local job application automation assistant. Interview me one question at a time and gather only verified facts. Produce five files in the exact schemas below:

1. data/My_Ground-info/profile/candidate_profile.yaml
2. data/ground_truth.json
3. data/projects_library.json
4. data/bullet_library.json
5. data/defaults.json

Rules:
- Do not invent degrees, employers, metrics, links, salaries, work authorization, or skills.
- Mark unknown optional values as null or empty arrays.
- Sensitive identifiers must default to unavailable or ask-before-use unless I explicitly provide a value and policy.
- Every project and experience must have a stable snake_case id.
- Bullet text must be truthful, specific, and grounded in the evidence I provide.
- Output valid YAML for candidate_profile.yaml and valid JSON for the other four files.

First ask me for identity/contact/location/links, then education, work authorization, job preferences, compensation policy, disclosures, experience, projects, skills, awards/certifications, and sensitive-answer policies.
```

After creating or editing these files, restart the backend so the stores and in-memory candidate data are reloaded.

## Running The Backend

```bash
source .venv/bin/activate
uvicorn backend.main:app --host 127.0.0.1 --port 8765 --reload
```

Useful endpoints:

- `GET /status`
- `POST /run/start`
- `POST /run/stop`
- `GET /applications`
- `DELETE /applications?job_url=...`
- `GET /runs`
- `GET /stream`

## Running The macOS App

Open `menubar-app/JobPilot.xcodeproj` in Xcode and run the `JobPilot` target. The app talks to the backend on `127.0.0.1:8765` by default, or `JOBPILOT_BACKEND_PORT` if set.

The menu-bar UI can:

- Start and stop runs.
- Toggle Dry Run / Real Submit.
- Toggle final-review behavior.
- Focus or open the automation browser.
- Answer approval, unknown field, and manual takeover prompts.
- View History, logs, runs, analytics, and stored applications.

## Running A Job Search

1. Start the backend.
2. Start the menu-bar app.
3. Paste a careers or open-roles URL. A list page works best; a single-job URL is mainly useful for forced retries from History.
4. Choose a limit.
5. Keep Dry Run enabled for first passes.
6. Start the run and respond to approvals or manual takeover prompts.

The orchestrator processes each listing through listing extraction, liveness checks, deduplication, classification, document generation, form filling, validation, and finalization.

## History Semantics

History has two sections:

- Dry Runs
- Real Submits

Each section has its own outcome fields in SQLite:

- `dry_run_outcome`, `dry_run_completed_at`, `dry_run_error`
- `real_submit_outcome`, `real_submit_completed_at`, `real_submit_error`

Green means the attempt succeeded in that mode and has no mode-specific error. Red means incomplete, blocked, failed, or requiring attention. Green entries are locked in the same mode. Red entries expose retry actions.

## Manual Intervention

JobPilot asks for help when it cannot safely continue, including:

- CAPTCHA or robot detection after the automatic one-time retry.
- Login, SSO, MFA, or expired sessions.
- Unknown required form fields.
- Browser validation errors that remain after automatic filling.
- Missing submit/next button selectors.
- Unsupported application flows.

When you complete the required action in the browser, choose continue in the menu-bar prompt.

## Data And Artifacts

Local state lives under `data/`:

- `data/jobpilot.db` stores runs, applications, pending actions, learned answers, and history.
- `data/browser-profile/` stores the persistent Chromium profile.
- `data/logs/` stores backend logs used by the menu-bar console.
- `data/outputs/` stores run artifacts when retained.

Generated artifacts may include resume PDFs, cover-letter PDFs, screenshots, HTML snapshots, pre-submit audits, ATS score JSON, and failure bundles.

## Development

Run the focused test suite:

```bash
pytest
```

Run a specific test file:

```bash
pytest tests/test_history_modes.py
```

Before changing form automation, prefer adding tests around:

- required-field audits,
- validation retry behavior,
- per-mode history persistence,
- CAPTCHA/manual takeover escalation,
- adapter field enumeration.

## Limitations

JobPilot cannot solve CAPTCHA challenges for you. It only retries once in case the page has a transient or skippable CAPTCHA-like interstitial, then asks for human help.

Job boards change frequently. Selectors and adapters may need updates for specific ATS platforms.

Real Submit should be used carefully. Keep Dry Run enabled until you have inspected the generated answers, documents, and final form state for the target site.
