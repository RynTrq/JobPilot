# JobPilot

JobPilot is a local macOS job-application copilot. It reads jobs from career pages and ATS platforms, scores whether they fit the user, opens application forms in Chromium, fills fields from the user's ground truth, generates tailored resume and cover-letter PDFs when needed, and keeps a human in the loop for classifier review, unknown fields, CAPTCHA, login, validation problems, and final submit approval.

The most important setup idea is simple: JobPilot is not supposed to be about the original developer. It becomes about the person using it through the `data/ground_truth/` directory. A fresh user adds their own verified facts there, and resume generation, cover-letter generation, classifier fit scoring, and form filling all start using that user's files.

Dry Run is the default. Real Submit must be enabled intentionally.

## Fresh Clone Setup

Start from a clean clone:

```bash
git clone <your-repo-url>
cd JobPilot-main/jobpilot
```

Install the system tools first:

| Tool | Required | Why JobPilot needs it |
| --- | --- | --- |
| Xcode | Yes, for the macOS app | Builds and runs the Swift menu-bar app. |
| Xcode Command Line Tools | Yes | Provides compiler and SDK tools used by Python and Xcode dependencies. |
| `uv` | Yes | Creates the Python 3.11 environment and installs locked dependencies. |
| Python 3.11 | Yes | The backend is pinned to Python `>=3.11,<3.12`. |
| Playwright Chromium | Yes | Browser automation target for application forms. |
| LaTeX, MacTeX or BasicTeX | Yes for resume/cover PDFs | Compiles generated `.tex` files into PDFs. |
| MongoDB | Optional | Optional mirror for application records. SQLite works locally without it. |
| Cloud LLM API keys | Optional | Enables cloud fallback providers. Local/fallback behavior works without keys. |

Install commands:

```bash
xcode-select --install
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.11
uv venv --python 3.11
source .venv/bin/activate
uv pip install -e ".[dev]"
uv run playwright install chromium
```

Install MacTeX or BasicTeX separately before generating PDFs.

## Required Directory Structure

Create this local structure before running a real application workflow:

```text
jobpilot/
  backend/
  menubar-app/
  templates/
  data/
    ground_truth/
      ground_truth.json
      candidate_profile.yaml
      projects_library.json
      bullet_library.json
      defaults.json
      classifier_feedback.jsonl        # auto-created after classifier reviews
      classifier.pkl                   # optional; usually absent for a new user
    browser-profile/                   # auto-created
    logs/                              # auto-created
    outputs/                           # auto-created
    jobpilot.db                        # auto-created SQLite database
```

Do not commit `data/ground_truth/`. It contains private identity, employment, compensation, eligibility, and classifier preference data. The `.gitignore` excludes it.

Backward compatibility: older local setups may have `data/ground_truth.json`, `data/projects_library.json`, `data/bullet_library.json`, `data/defaults.json`, and `data/My_Ground-info/profile/candidate_profile.yaml`. JobPilot can still read those if the new `data/ground_truth/` files are not present, but new users should use the directory above.

## Ground Truth Setup

The `data/ground_truth/` directory is the user-owned brain of JobPilot.

| File | Used by | Purpose |
| --- | --- | --- |
| `ground_truth.json` | Form filling, resume builder, cover-letter writer, fit rules, classifier heuristic | Canonical structured facts: identity, education, experience, projects, skills, preferences, EEOC defaults, free-form reusable answers. |
| `candidate_profile.yaml` | Form filling, strict fit rules, resume builder | Policy-rich profile: work authorization, compensation rules, disclosure defaults, sensitive-answer policy, application guardrails. |
| `projects_library.json` | Resume generation and free-text form answers | Project catalog with bullet variants and tags so JobPilot can select job-relevant proof. |
| `bullet_library.json` | Resume bullet selection | Experience/project bullet variants keyed by stable IDs from `ground_truth.json`. |
| `defaults.json` | Resume/generation fallbacks | Safe fallback text and skill pools when a generator cannot confidently produce output. |
| `classifier_feedback.jsonl` | Classifier learning | Auto-created. Every classifier review appends a pass/fail training example. |
| `classifier.pkl` | Optional classifier model | Optional trained model. A fresh user normally does not have this file. |

### `data/ground_truth/ground_truth.json`

Use valid JSON with this schema:

```json
{
  "personal": {
    "full_name": "Your Legal Name",
    "preferred_name": "Your Preferred Name",
    "email": "you@example.com",
    "phone_e164": "+15551234567",
    "location_city": "Your City",
    "location_state": null,
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
      "start_month_year": "2022-08",
      "end_month_year": "2026-05",
      "gpa": null,
      "honors": [],
      "relevant_courses": ["Data Structures", "Operating Systems"]
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
  "skills": {
    "languages": ["Python"],
    "frameworks": ["FastAPI"],
    "tools": ["Git"],
    "ml": [],
    "soft": ["Communication"]
  },
  "preferences": {
    "desired_roles": ["Software Engineer", "Backend Engineer"],
    "desired_industries": ["SaaS", "Developer Tools"],
    "salary_min_usd_annual": 100000,
    "salary_expected_inr_lpa": null,
    "notice_period_days": 14,
    "willing_to_relocate": true,
    "earliest_start_date": "2026-06-01"
  },
  "eeoc": {
    "gender": "Prefer not to say",
    "race": "Prefer not to say",
    "veteran": "Prefer not to say",
    "disability": "Prefer not to say",
    "hispanic_latino": "Prefer not to say"
  },
  "freeform_answers": {
    "why_this_role": "One grounded sentence.",
    "greatest_strength": "One grounded sentence.",
    "greatest_weakness": "One grounded sentence.",
    "five_year_goals": "One grounded sentence."
  },
  "profile_statement": "One short professional summary grounded only in verified facts.",
  "custom": {}
}
```

Rules:

- Keep every top-level key, even if some arrays are empty.
- Use stable snake_case `id` values for every experience and project. The same IDs are used by `bullet_library.json`.
- Dates must be `YYYY-MM`, except `preferences.earliest_start_date`, which must be `YYYY-MM-DD`.
- Do not add extra keys to `ground_truth.json`; the backend validates this file strictly.

### `data/ground_truth/candidate_profile.yaml`

Use YAML for broader policies and optional sections:

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
    race_ethnicity: Prefer not to say
    veteran_status: Prefer not to say
    disability: Prefer not to say

resume_strategy:
  objective: Accurate, ATS-friendly resume tailored only from verified facts.
  never_claim:
    - Skills, degrees, employers, metrics, or tools not present in the source files.

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

The YAML reader accepts extra policy sections, so this is the best place for nuanced human rules.

### `data/ground_truth/projects_library.json`

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

### `data/ground_truth/bullet_library.json`

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

### `data/ground_truth/defaults.json`

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
  "project_bullet": "Built a verified project component using the declared technology stack.",
  "grounded_answer": {
    "answer_text": "UNKNOWN",
    "source_keys_used": [],
    "confidence_0_to_1": 0.0,
    "unknown_flag": true,
    "fallback_reason": "defaults"
  },
  "grounded_json": {}
}
```

## Ground Truth Validation

After creating the files, run:

```bash
uv run python - <<'PY'
from backend.storage.ground_truth import GroundTruth
from backend.form.field_answerer import load_candidate_data, build_candidate_corpus

gt = GroundTruth.load()
candidate_data = load_candidate_data()
corpus = build_candidate_corpus(candidate_data)

print("ground_truth.json valid for:", gt.personal.full_name or "<name missing>")
print("form-answer corpus facts:", len(corpus))
print("projects:", len(gt.projects))
print("experience:", len(gt.experience))
PY
```

If this fails, fix the file named in the error before starting the backend.

## Prompt To Create Ground Truth

You can ask an LLM to interview the user and produce the files:

```text
I am setting up JobPilot, a local job application automation assistant.
Interview me one question at a time and gather only verified facts.
Produce these files exactly:

1. data/ground_truth/ground_truth.json
2. data/ground_truth/candidate_profile.yaml
3. data/ground_truth/projects_library.json
4. data/ground_truth/bullet_library.json
5. data/ground_truth/defaults.json

Rules:
- Do not invent degrees, employers, metrics, links, salaries, work authorization, or skills.
- Mark unknown optional values as null or empty arrays.
- Sensitive identifiers must default to unavailable or ask-before-use unless I explicitly provide a value and policy.
- Every project and experience must have a stable snake_case id.
- Bullet text must be truthful, specific, and grounded in the evidence I provide.
- Output valid YAML for candidate_profile.yaml and valid JSON for the other four files.
- Use the exact schemas from the JobPilot README.

Ask first for identity/contact/location/links, then education, work authorization, job preferences, compensation policy, disclosures, experience, projects, skills, awards/certifications, and sensitive-answer policies.
```

Restart the backend after editing ground-truth files.

## Classifier Learning

A new user should start without `data/ground_truth/classifier.pkl`. In that cold-start state, JobPilot uses:

1. The user's `ground_truth.json` skills, desired roles, preferences, and experience to produce a first-pass heuristic score.
2. `data/ground_truth/classifier_feedback.jsonl` to adjust future scores based on the user's reviewed examples.

Existing users keep their training history. JobPilot reads both the legacy `data/classifier_feedback.jsonl` file and the newer `data/ground_truth/classifier_feedback.jsonl` file, then appends new reviews to the newer user-owned file. If an existing `data/classifier.pkl` model is present and no newer `data/ground_truth/classifier.pkl` exists, JobPilot also continues using that model. That means older feedback keeps influencing scores while new users, who have neither file yet, start fresh.

Every classifier review stores a training signal immediately when the user clicks Approve or Fail:

```json
{"job_url":"...","label":"pass","score":0.71,"description_text":"...","created_at":"..."}
```

The label is the user's actual review decision. If the classifier says fail but the user approves it, that row is stored as `pass`. If the classifier says pass but the user rejects it, that row is stored as `fail`.

The scorer reads the feedback file every time it scores a later job, so new review clicks are available to the classifier during the same run. As the file grows, similar future jobs are nudged toward the user's preferences. Keep `classifier_feedback.jsonl` if you want the classifier to keep learning. Delete it only when you intentionally want to reset a user's learned preferences. Do not commit it to GitHub.

Important settings:

```env
CLASSIFIER_THRESHOLD=0.65
CLASSIFIER_AUTO_PASS=0
```

Keep `CLASSIFIER_AUTO_PASS=0` while training a new user. That forces human review and creates better feedback. Once the user has enough reviewed examples, enabling auto-pass is safer.

## Configuration

Create `.env` in the repository root as needed:

```env
JOBPILOT_HOST=127.0.0.1
JOBPILOT_PORT=8765
JOBPILOT_GROUND_TRUTH_DIR=data/ground_truth

DRY_RUN=1
AUTO_SUBMIT_WITHOUT_APPROVAL=0
LIVE_MODE=0

BROWSER_PERSISTENT=1
BROWSER_USER_DATA_DIR=data/browser-profile
BROWSER_STEALTH_LEVEL=2

MONGO_URI=
MONGO_DB=jobpilot

GENERATOR_DISABLED=1
CLASSIFIER_THRESHOLD=0.65
CLASSIFIER_AUTO_PASS=0

GROQ_API_KEY=
GEMINI_API_KEY=
CEREBRAS_API_KEY=
MISTRAL_API_KEY=
OPENROUTER_API_KEY=
```

Notes:

- `DRY_RUN=1` fills forms and stops before final submit.
- `DRY_RUN=0` enables real-submit behavior.
- `AUTO_SUBMIT_WITHOUT_APPROVAL=0` requires final review before a real final submit.
- `LIVE_MODE=1` opens a visible browser so the user can watch and take over.
- `GENERATOR_DISABLED=1` keeps MLX generation disabled. Cloud/provider keys or local MLX setup are needed for richer generation.
- `JOBPILOT_GROUND_TRUTH_DIR` can point to another private folder, but `data/ground_truth` is the expected default.

## Running The Backend

```bash
source .venv/bin/activate
uv run uvicorn backend.main:app --host 127.0.0.1 --port 8765 --reload
```

Useful endpoints:

- `GET /status`
- `GET /settings`
- `GET /ground_truth`
- `PUT /ground_truth`
- `POST /run/start`
- `POST /run/stop`
- `GET /applications`
- `GET /runs`
- `GET /stream`

## Running The macOS App

Open:

```text
menubar-app/JobPilot.xcodeproj
```

Run the `JobPilot` target in Xcode. The app connects to the backend on `127.0.0.1:8765` by default.

The menu-bar app can:

- Start and stop runs.
- Toggle Dry Run and Real Submit.
- Toggle final-review behavior.
- Focus or open the automation browser.
- Answer classifier, approval, unknown-field, and manual-takeover prompts.
- View History, logs, analytics, runs, and stored applications.

## Running A Job Search

1. Start the backend.
2. Start the menu-bar app.
3. Paste a careers page, ATS search page, or supported job listing URL.
4. Choose a limit.
5. Keep Dry Run enabled for the first pass.
6. Review classifier decisions so the user-specific classifier learns.
7. Respond to unknown fields, CAPTCHA, login, and final-review prompts.

The orchestrator processes listings through scraping, liveness checks, deduplication, classifier review, resume/cover-letter generation, form filling, validation, and finalization.

## Safety Behavior

Dry Run:

- Fills the application form.
- Audits required fields, including browser-required and asterisk-marked fields.
- Attempts to fill missing required fields.
- Asks the user when a required answer cannot be safely inferred.
- Stops before the final submit click.

Real Submit:

- Requires live submit mode.
- Requires final approval unless explicitly disabled.
- Clicks submit only after validation.
- Confirms the browser did not flag missing or invalid fields.
- Does not mark the job successful while validation errors remain.

CAPTCHA:

- On first detection, JobPilot attempts one safe continue/retry.
- If CAPTCHA appears again or persists, it prompts: `CAPTCHA detected — Human intervention required.`

History:

- The History panel has separate Dry Run and Real Submit sections.
- A green Dry Run entry cannot be run again as Dry Run.
- A green Real Submit entry cannot be run again as Real Submit.
- Red entries remain actionable.

## Tech Stack

| Layer | Repo location | Tech | How it is used |
| --- | --- | --- | --- |
| Backend API | `backend/main.py`, `backend/api/*`, `backend/config.py` | FastAPI, Uvicorn, Pydantic, python-dotenv, structlog | Starts the runtime, validates settings, exposes REST/stream endpoints, and wires stores, browser, model routing, classifier, and gates. |
| Orchestration | `backend/orchestrator.py`, `backend/conductor.py`, `backend/retry.py` | asyncio, Tenacity | Runs each job through extraction, liveness, fit review, document generation, form fill, validation, history, and artifact cleanup. |
| Browser automation | `backend/scraping/browser.py`, `backend/scraping/adapters/*`, `backend/form/*` | Playwright, playwright-stealth, RapidFuzz | Opens Chromium, scrapes job lists, detects apply buttons, fills fields, handles next/submit buttons, and pauses for manual takeover. |
| HTML/text extraction | `backend/scraping/job_page.py`, `backend/specialists/jd_cleaner.py` | BeautifulSoup4, Trafilatura | Turns noisy job pages into readable job descriptions. |
| Embeddings | `backend/models/encoder.py` | sentence-transformers, NumPy | Encodes job descriptions, profile facts, labels, projects, and bullets for semantic matching. |
| Classifier | `backend/models/classifier.py`, `backend/models/classifier_feedback.py` | scikit-learn artifact support, NumPy, JSONL feedback | Scores fit from the user's ground truth, starts cold without a model file, and learns from review feedback. |
| LLM routing | `backend/llm/router.py`, `backend/llm/providers.py`, `backend/models/generator.py` | MLX, Groq, Gemini, Cerebras, Mistral, OpenRouter | Routes generation locally or to configured cloud providers while respecting privacy and schema needs. |
| Form answering | `backend/form/field_answerer.py`, `backend/form/answerer.py`, `backend/storage/learned_answers.py` | Ground-truth JSON/YAML, SQLite learned answers, translation cache | Answers structured fields from user facts, reuses learned answers, and asks the user when needed. |
| Resume and cover letters | `backend/resume/*`, `backend/cover_letter/*`, `templates/*` | Jinja2, LaTeX | Builds job-specific resume/cover contexts and compiles PDFs. |
| Storage | `backend/storage/*`, `data/*` | SQLite, optional MongoDB, JSON, YAML, JSONL | Stores history, runs, applications, pending actions, learned answers, button memory, profile files, and classifier feedback. |
| Alerts/manual gates | `backend/alarm/*` | pyobjc, AppKit | Sends native macOS attention prompts for approvals, classifier review, CAPTCHA, login, validation, and manual takeover. |
| macOS app | `menubar-app/JobPilot/*` | Swift, SwiftUI, AppKit, Combine | Provides menu-bar controls, settings, history, prompts, logs, and backend communication. |
| Tests/tooling | `pyproject.toml`, `uv.lock`, `tests/*` | pytest, pytest-asyncio, httpx, hypothesis, TexSoup | Pins dependencies and validates backend, form, history, scraping, and setup behavior. |

## Models And Artifacts

| Model/artifact | Where | Purpose |
| --- | --- | --- |
| `BAAI/bge-small-en-v1.5` | `backend/models/encoder.py` | Main embedding model for semantic matching and feedback similarity. Falls back to deterministic hashing if unavailable. |
| `data/ground_truth/classifier_feedback.jsonl` | `backend/models/classifier_feedback.py` | User-specific training log appended after classifier reviews. |
| `data/ground_truth/classifier.pkl` | `backend/models/classifier.py` | Optional trained classifier. Fresh users should usually start without it. |
| `mlx-community/Qwen2.5-14B-Instruct-4bit` | `backend/models/generator.py` | Optional local generator when `GENERATOR_DISABLED=0`. |
| `mlx-community/Qwen2.5-1.5B-Instruct-4bit` | `backend/llm/router.py` | Local tiny routing target for short completions. |
| `mlx-community/Qwen2.5-3B-Instruct-4bit` | `backend/llm/router.py` | Local JSON/schema-friendly routing target. |
| `mlx-community/Qwen2.5-7B-Instruct-4bit` | `backend/llm/router.py` | Local reasoning routing target. |
| `llama-3.1-8b-instant` | `backend/llm/router.py` | Groq fast cloud target. |
| `llama-3.3-70b-versatile` | `backend/llm/router.py` | Groq stronger cloud fallback target. |
| `gemini-2.5-flash` | `backend/llm/router.py` | Gemini structured-output fallback target. |

Cloud clients are only created when corresponding API keys are present.

## Data And Privacy

Private local data:

- `data/ground_truth/`
- `data/jobpilot.db`
- `data/browser-profile/`
- `data/logs/`
- `data/outputs/`
- `data/platform-sessions/`

These should stay local. Push source code, tests, templates, and documentation to GitHub, not personal profile data or browser/session state.

## Development

Run all tests:

```bash
uv run pytest
```

Run focused tests:

```bash
uv run pytest tests/test_ground_truth_setup.py tests/test_history_modes.py
```

Useful checks after automation changes:

```bash
uv run python -m compileall backend
```

When changing browser or form automation, add focused tests around apply-button detection, required-field audits, validation retry behavior, per-mode history persistence, CAPTCHA/manual takeover escalation, and adapter field enumeration.

## Limitations

JobPilot cannot solve CAPTCHA challenges. It only retries once for transient/skippable CAPTCHA-like states, then asks for human help.

Job boards and ATS platforms change frequently. Selectors and adapters may need updates for specific sites.
