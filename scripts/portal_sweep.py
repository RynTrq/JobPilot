"""
Portal sweep — runs the full JobPilot pipeline against 100+ career portal types
across India, Japan, Australia, Europe, and GCC.

Usage:
    # Start the backend first:
    #   cd jobpilot && uv run python -m backend.main
    #
    # Then in another terminal:
    #   uv run python scripts/portal_sweep.py [--dry-run] [--region REGION] [--limit N]

Flags:
    --dry-run          Use DRY_RUN mode (no real form submission). Default: True.
    --live             Enable real submission (adds --dry-run=false to API call).
    --region REGION    Filter to one region: india|japan|australia|europe|gcc|global
    --limit N          Max jobs to process per portal. Default: 5
    --timeout N        Seconds to wait for each portal run. Default: 300
    --base-url URL     Backend API base URL. Default: http://127.0.0.1:8765
    --log-file PATH    Write structured results to file. Default: data/logs/sweep_TIMESTAMP.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Portal URL registry — 100+ career pages across all regions
# ---------------------------------------------------------------------------

PORTALS: list[dict[str, Any]] = [
    # -------------------------------------------------------------------------
    # India (25 portals)
    # -------------------------------------------------------------------------
    {"region": "india", "key": "freshersworld",        "url": "https://www.freshersworld.com/jobs/computer-science-engineering",       "type": "regional_job_board"},
    {"region": "india", "key": "internshala",          "url": "https://internshala.com/internships/software-development-internship",   "type": "internship_board"},
    {"region": "india", "key": "hirist",               "url": "https://hirist.tech/jobs/software-developer",                          "type": "tech_job_board"},
    {"region": "india", "key": "cutshort",             "url": "https://cutshort.io/jobs?q=fresher+software+engineer",                 "type": "tech_job_board"},
    {"region": "india", "key": "wellfound-india",      "url": "https://wellfound.com/jobs?role=software-engineer&experience=0-1",     "type": "startup_job_board"},
    {"region": "india", "key": "tcs-campus",           "url": "https://campus.tcs.com/",                                              "type": "corporate_careers"},
    {"region": "india", "key": "infosys-careers",      "url": "https://career.infosys.com/jobdesc?jobReferenceCode=INFSYS-EXTERNAL-00241891", "type": "corporate_careers"},
    {"region": "india", "key": "wipro-careers",        "url": "https://careers.wipro.com/careers-home/",                             "type": "corporate_careers"},
    {"region": "india", "key": "razorpay-lever",       "url": "https://jobs.lever.co/razorpay",                                      "type": "ats_lever"},
    {"region": "india", "key": "swiggy-lever",         "url": "https://jobs.lever.co/swiggy",                                        "type": "ats_lever"},
    {"region": "india", "key": "cred-lever",           "url": "https://jobs.lever.co/cred",                                          "type": "ats_lever"},
    {"region": "india", "key": "meesho-lever",         "url": "https://jobs.lever.co/meesho",                                        "type": "ats_lever"},
    {"region": "india", "key": "phonepe-greenhouse",   "url": "https://boards.greenhouse.io/phonepe",                                "type": "ats_greenhouse"},
    {"region": "india", "key": "sharechat-greenhouse", "url": "https://boards.greenhouse.io/sharechat",                              "type": "ats_greenhouse"},
    {"region": "india", "key": "sprinklr-greenhouse",  "url": "https://boards.greenhouse.io/sprinklr",                               "type": "ats_greenhouse"},
    {"region": "india", "key": "zomato-careers",       "url": "https://www.zomato.com/jobs",                                        "type": "corporate_careers"},
    {"region": "india", "key": "naukri-fresher",       "url": "https://www.naukri.com/fresher-jobs",                                 "type": "regional_job_board"},
    {"region": "india", "key": "indeed-india",         "url": "https://in.indeed.com/jobs?q=software+engineer+fresher&l=Bangalore",  "type": "aggregator"},
    {"region": "india", "key": "freshersworld-cs",     "url": "https://www.freshersworld.com/jobs/computer-science",                 "type": "entry_level"},
    {"region": "india", "key": "internshala-ml",       "url": "https://internshala.com/internships/machine-learning-internship",     "type": "internship"},
    {"region": "india", "key": "workindia",            "url": "https://www.workindia.in/jobs/software-engineer",                     "type": "regional_job_board"},
    {"region": "india", "key": "herkey",               "url": "https://www.herkey.com/jobs",                                        "type": "diversity_board"},
    {"region": "india", "key": "foundit-india",        "url": "https://www.foundit.in/seeker/searchjobs?query=fresher+developer",    "type": "regional_job_board"},
    {"region": "india", "key": "apna",                 "url": "https://apna.co/jobs/software-developer",                            "type": "regional_job_board"},
    {"region": "india", "key": "iiit-delhi-careers",   "url": "https://www.iiitd.ac.in/placement",                                  "type": "campus"},

    # -------------------------------------------------------------------------
    # Japan (15 portals)
    # -------------------------------------------------------------------------
    {"region": "japan", "key": "tokyodev",             "url": "https://www.tokyodev.com/jobs",                                       "type": "intl_tech_board"},
    {"region": "japan", "key": "japan-dev",            "url": "https://japan-dev.com/jobs",                                         "type": "intl_tech_board"},
    {"region": "japan", "key": "gaijinpot",            "url": "https://jobs.gaijinpot.com/jobs/",                                   "type": "intl_job_board"},
    {"region": "japan", "key": "en-gage",              "url": "https://en-gage.net/",                                               "type": "regional_job_board"},
    {"region": "japan", "key": "mercari-greenhouse",   "url": "https://boards.greenhouse.io/mercari",                               "type": "ats_greenhouse"},
    {"region": "japan", "key": "cyberagent-greenhouse","url": "https://boards.greenhouse.io/cyberagent",                            "type": "ats_greenhouse"},
    {"region": "japan", "key": "moneyforward-gh",      "url": "https://boards.greenhouse.io/moneyforward",                         "type": "ats_greenhouse"},
    {"region": "japan", "key": "line-lever",           "url": "https://jobs.lever.co/linecorp",                                     "type": "ats_lever"},
    {"region": "japan", "key": "freee-greenhouse",     "url": "https://boards.greenhouse.io/freee",                                 "type": "ats_greenhouse"},
    {"region": "japan", "key": "forkwell",             "url": "https://jobs.forkwell.com/",                                         "type": "tech_job_board"},
    {"region": "japan", "key": "paiza",                "url": "https://paiza.jp/works/jobs",                                        "type": "tech_job_board"},
    {"region": "japan", "key": "daijob",               "url": "https://www.daijob.com/en/jobs",                                     "type": "bilingual_board"},
    {"region": "japan", "key": "careercross",          "url": "https://www.careercross.com/en/jobs",                                "type": "bilingual_board"},
    {"region": "japan", "key": "wantedly",             "url": "https://www.wantedly.com/projects?occupation_types%5B%5D=engineer",  "type": "talent_marketplace"},
    {"region": "japan", "key": "indeed-japan",         "url": "https://jp.indeed.com/jobs?q=software+engineer+new+grad",           "type": "aggregator"},

    # -------------------------------------------------------------------------
    # Australia (15 portals)
    # -------------------------------------------------------------------------
    {"region": "australia", "key": "gradconnection",   "url": "https://au.gradconnection.com/graduate-jobs/information-technology/", "type": "graduate_board"},
    {"region": "australia", "key": "jora-au",          "url": "https://au.jora.com/j?q=software+engineer+graduate",                "type": "aggregator"},
    {"region": "australia", "key": "atlassian-gh",     "url": "https://boards.greenhouse.io/atlassian",                            "type": "ats_greenhouse"},
    {"region": "australia", "key": "canva-lever",      "url": "https://jobs.lever.co/canva",                                       "type": "ats_lever"},
    {"region": "australia", "key": "seek-au",          "url": "https://www.seek.com.au/software-developer-jobs/in-All-Australia?classification=1209&subclassification=6281", "type": "regional_job_board"},
    {"region": "australia", "key": "xero-lever",       "url": "https://jobs.lever.co/xero",                                        "type": "ats_lever"},
    {"region": "australia", "key": "culture-amp-gh",   "url": "https://boards.greenhouse.io/cultureamp",                           "type": "ats_greenhouse"},
    {"region": "australia", "key": "envato-gh",        "url": "https://boards.greenhouse.io/envato",                               "type": "ats_greenhouse"},
    {"region": "australia", "key": "buildkite-gh",     "url": "https://boards.greenhouse.io/buildkite",                            "type": "ats_greenhouse"},
    {"region": "australia", "key": "hatch",            "url": "https://hatch.team/jobs",                                           "type": "talent_marketplace"},
    {"region": "australia", "key": "seek-grad",        "url": "https://www.seek.com.au/jobs?keywords=graduate+software&where=All+Australia", "type": "aggregator"},
    {"region": "australia", "key": "indeed-au",        "url": "https://au.indeed.com/jobs?q=graduate+software+engineer&l=Australia", "type": "aggregator"},
    {"region": "australia", "key": "nab-careers",      "url": "https://careers.nab.com.au/",                                       "type": "corporate_careers"},
    {"region": "australia", "key": "redbubble-gh",     "url": "https://boards.greenhouse.io/redbubble",                            "type": "ats_greenhouse"},
    {"region": "australia", "key": "apsjobs",          "url": "https://www.apsjobs.gov.au/s/job-search?query=developer",          "type": "government_portal"},

    # -------------------------------------------------------------------------
    # GCC / Middle East (15 portals)
    # -------------------------------------------------------------------------
    {"region": "gcc",     "key": "bayt",               "url": "https://www.bayt.com/en/uae/jobs/software-engineer-jobs/",           "type": "regional_job_board"},
    {"region": "gcc",     "key": "naukrigulf",          "url": "https://www.naukrigulf.com/fresher-jobs-in-uae",                    "type": "regional_job_board"},
    {"region": "gcc",     "key": "gulftalent",          "url": "https://www.gulftalent.com/jobs/software",                         "type": "regional_job_board"},
    {"region": "gcc",     "key": "wuzzuf",              "url": "https://wuzzuf.net/jobs/p/search?q=software+engineer+fresh&a=fresher", "type": "regional_job_board"},
    {"region": "gcc",     "key": "drjobs",              "url": "https://drjobs.ae/jobs/software-engineer",                         "type": "regional_job_board"},
    {"region": "gcc",     "key": "careem-gh",           "url": "https://boards.greenhouse.io/careem",                              "type": "ats_greenhouse"},
    {"region": "gcc",     "key": "deliveryhero-gh",     "url": "https://boards.greenhouse.io/deliveryhero",                        "type": "ats_greenhouse"},
    {"region": "gcc",     "key": "namshi-gh",           "url": "https://boards.greenhouse.io/namshi",                              "type": "ats_greenhouse"},
    {"region": "gcc",     "key": "property-finder-gh",  "url": "https://boards.greenhouse.io/propertyfinder",                      "type": "ats_greenhouse"},
    {"region": "gcc",     "key": "noon-lever",          "url": "https://jobs.lever.co/noon",                                       "type": "ats_lever"},
    {"region": "gcc",     "key": "talabat-gh",          "url": "https://boards.greenhouse.io/talabat",                             "type": "ats_greenhouse"},
    {"region": "gcc",     "key": "dubizzle-careers",    "url": "https://careers.dubizzle.com/",                                    "type": "corporate_careers"},
    {"region": "gcc",     "key": "laimoon",             "url": "https://laimoon.com/jobs/software-engineer",                       "type": "regional_job_board"},
    {"region": "gcc",     "key": "gulfjobs",            "url": "https://gulfjobs.com/jobs/software-developer",                     "type": "regional_job_board"},
    {"region": "gcc",     "key": "akhtaboot",           "url": "https://www.akhtaboot.com/jobs/technology-it",                     "type": "regional_job_board"},

    # -------------------------------------------------------------------------
    # Europe (20 portals)
    # -------------------------------------------------------------------------
    {"region": "europe",  "key": "welcometothejungle",  "url": "https://www.welcometothejungle.com/en/jobs?query=junior+developer",  "type": "regional_job_board"},
    {"region": "europe",  "key": "nofluffjobs",         "url": "https://nofluffjobs.com/jobs/junior",                               "type": "tech_job_board"},
    {"region": "europe",  "key": "justjoinit",          "url": "https://justjoin.it/?experience=junior",                           "type": "tech_job_board"},
    {"region": "europe",  "key": "prospects-uk",        "url": "https://www.prospects.ac.uk/jobs-and-work-experience/job-sectors/information-technology", "type": "graduate_board"},
    {"region": "europe",  "key": "spotify-gh",          "url": "https://boards.greenhouse.io/spotify",                             "type": "ats_greenhouse"},
    {"region": "europe",  "key": "klarna-lever",        "url": "https://jobs.lever.co/klarna",                                     "type": "ats_lever"},
    {"region": "europe",  "key": "deliveroo-gh",        "url": "https://boards.greenhouse.io/deliveroo",                           "type": "ats_greenhouse"},
    {"region": "europe",  "key": "booking-gh",          "url": "https://boards.greenhouse.io/bookingcom",                          "type": "ats_greenhouse"},
    {"region": "europe",  "key": "n26-lever",           "url": "https://jobs.lever.co/n26",                                        "type": "ats_lever"},
    {"region": "europe",  "key": "adyen-gh",            "url": "https://boards.greenhouse.io/adyen",                               "type": "ats_greenhouse"},
    {"region": "europe",  "key": "mollie-gh",           "url": "https://boards.greenhouse.io/mollie",                              "type": "ats_greenhouse"},
    {"region": "europe",  "key": "contentful-gh",       "url": "https://boards.greenhouse.io/contentful",                          "type": "ats_greenhouse"},
    {"region": "europe",  "key": "soundcloud-gh",       "url": "https://boards.greenhouse.io/soundcloud",                          "type": "ats_greenhouse"},
    {"region": "europe",  "key": "zendesk-gh",          "url": "https://boards.greenhouse.io/zendesk",                             "type": "ats_greenhouse"},
    {"region": "europe",  "key": "otta",                "url": "https://app.otta.com/jobs/search?industry=software-engineering",    "type": "startup_board"},
    {"region": "europe",  "key": "stepstone-de",        "url": "https://www.stepstone.de/jobs/Softwareentwickler/in-Deutschland.html", "type": "regional_job_board"},
    {"region": "europe",  "key": "guardian-jobs",       "url": "https://jobs.theguardian.com/jobs/it-tech-software/",              "type": "newspaper_board"},
    {"region": "europe",  "key": "djinni",              "url": "https://djinni.co/jobs/?exp_level=no_exp",                         "type": "tech_job_board"},
    {"region": "europe",  "key": "eures",               "url": "https://eures.europa.eu/jobs-and-ats/jobs_en?page=1&keywords=software+engineer+junior", "type": "eu_government"},
    {"region": "europe",  "key": "adzuna-uk",           "url": "https://www.adzuna.co.uk/search?q=graduate+software+engineer&w=London", "type": "aggregator"},

    # -------------------------------------------------------------------------
    # Global ATS / Remote (15 portals)
    # -------------------------------------------------------------------------
    {"region": "global",  "key": "openai-gh",           "url": "https://boards.greenhouse.io/openai",                              "type": "ats_greenhouse"},
    {"region": "global",  "key": "stripe-gh",           "url": "https://boards.greenhouse.io/stripe",                             "type": "ats_greenhouse"},
    {"region": "global",  "key": "figma-gh",            "url": "https://boards.greenhouse.io/figma",                              "type": "ats_greenhouse"},
    {"region": "global",  "key": "notion-gh",           "url": "https://boards.greenhouse.io/notion",                             "type": "ats_greenhouse"},
    {"region": "global",  "key": "airbnb-gh",           "url": "https://boards.greenhouse.io/airbnb",                             "type": "ats_greenhouse"},
    {"region": "global",  "key": "github-lever",        "url": "https://jobs.lever.co/github",                                    "type": "ats_lever"},
    {"region": "global",  "key": "shopify-gh",          "url": "https://boards.greenhouse.io/shopify",                            "type": "ats_greenhouse"},
    {"region": "global",  "key": "weworkremotely",      "url": "https://weworkremotely.com/remote-jobs#job-listings",             "type": "remote_board"},
    {"region": "global",  "key": "remoteok",            "url": "https://remoteok.com/remote-junior-software-jobs",                "type": "remote_board"},
    {"region": "global",  "key": "smartrecruiters",     "url": "https://jobs.smartrecruiters.com/?keyword=software+engineer+junior", "type": "ats_smartrecruiters"},
    {"region": "global",  "key": "workable-jobs",       "url": "https://jobs.workable.com/view/4HH9sDX74n9tqGZuoNBFV8/junior-software-engineer-in-athens-at-workable", "type": "ats_workable"},
    {"region": "global",  "key": "ashby-sample",        "url": "https://jobs.ashbyhq.com/linear",                                 "type": "ats_ashby"},
    {"region": "global",  "key": "dover-sample",        "url": "https://app.dover.com/jobs/sample-company/software-engineer",     "type": "ats_dover"},
    {"region": "global",  "key": "dover-api",           "url": "https://app.dover.com/jobs",                                      "type": "ats_dover"},
    {"region": "global",  "key": "yc-work-at-startup",  "url": "https://www.workatastartup.com/jobs?role=eng&yoe_min=0&yoe_max=1", "type": "startup_board"},
]


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _api(base_url: str, method: str, path: str, body: dict | None = None) -> dict:
    url = base_url.rstrip("/") + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return {"error": exc.reason, "code": exc.code, "body": exc.read().decode()[:200]}
    except Exception as exc:
        return {"error": str(exc)}


def _wait_for_run(base_url: str, timeout: int, poll_interval: float = 5.0) -> dict:
    """Poll /status until the run is no longer active or timeout is reached."""
    deadline = time.time() + timeout
    last_stage = None
    while time.time() < deadline:
        status = _api(base_url, "GET", "/status")
        state = status.get("state", "")
        stage = status.get("current_stage")
        if stage != last_stage:
            last_stage = stage
            _log(f"  stage={stage}  state={state}", level="INFO")
        if state not in ("running", "starting"):
            return status
        time.sleep(poll_interval)
    # Timeout — stop the run
    _api(base_url, "POST", "/run/stop")
    return {"state": "timeout", "current_stage": last_stage}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_log_file = None


def _log(msg: str, level: str = "INFO") -> None:
    ts = datetime.utcnow().strftime("%H:%M:%S")
    print(f"[{ts}] {level:5s}  {msg}", flush=True)


def _log_result(result: dict, log_path: Path) -> None:
    with log_path.open("a") as fh:
        fh.write(json.dumps(result) + "\n")


# ---------------------------------------------------------------------------
# Main sweep logic
# ---------------------------------------------------------------------------

def sweep(args: argparse.Namespace) -> None:
    base_url = args.base_url
    portals = PORTALS
    if args.region:
        portals = [p for p in portals if p["region"] == args.region.lower()]
    if not portals:
        _log(f"No portals for region={args.region!r}", level="ERROR")
        sys.exit(1)

    log_dir = ROOT / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    ts_str = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"sweep_{ts_str}.jsonl"
    _log(f"Logging results to {log_path}")

    # Verify backend is reachable
    health = _api(base_url, "GET", "/status")
    if "error" in health:
        _log(f"Backend not reachable at {base_url}: {health['error']}", level="ERROR")
        _log("Start the backend with:  uv run python -m backend.main", level="ERROR")
        sys.exit(1)
    _log(f"Backend reachable. state={health.get('state', 'unknown')}")

    results: dict[str, list] = {"pass": [], "fail": [], "skip": [], "timeout": []}

    for idx, portal in enumerate(portals, 1):
        region = portal["region"]
        key = portal["key"]
        url = portal["url"]
        ptype = portal["type"]
        _log(f"[{idx:3d}/{len(portals)}] {region:10s}  {key:30s}  {url[:70]}")

        # Stop any existing run first
        _api(base_url, "POST", "/run/stop")
        time.sleep(1)

        # Start the run (bypass_classifier=False so we actually test classifier)
        start_resp = _api(base_url, "POST", "/run/start", {
            "career_url": url,
            "limit": args.limit,
            "force_reprocess": False,
            "bypass_classifier": False,
        })

        if "error" in start_resp:
            _log(f"  START FAILED: {start_resp['error']}", level="WARN")
            rec = {"portal": key, "region": region, "url": url, "type": ptype, "outcome": "start_failed", "detail": start_resp["error"]}
            results["fail"].append(rec)
            _log_result(rec, log_path)
            continue

        run_id = start_resp.get("run_id")
        _log(f"  run_id={run_id}  waiting up to {args.timeout}s ...")

        final = _wait_for_run(base_url, timeout=args.timeout)
        state = final.get("state", "unknown")
        stage = final.get("current_stage", "unknown")
        last_failure = final.get("last_failure")

        if state == "timeout":
            outcome = "timeout"
            results["timeout"].append({"portal": key, "region": region, "url": url, "type": ptype, "outcome": outcome, "last_stage": stage})
            _log(f"  TIMEOUT  last_stage={stage}", level="WARN")
        elif state in ("completed", "idle") and not last_failure:
            outcome = "completed"
            results["pass"].append({"portal": key, "region": region, "url": url, "type": ptype, "outcome": outcome})
            _log(f"  COMPLETED  stage={stage}", level="INFO")
        elif state in ("failed", "error") or last_failure:
            outcome = "failed"
            detail = str(last_failure or final.get("error", ""))
            results["fail"].append({"portal": key, "region": region, "url": url, "type": ptype, "outcome": outcome, "detail": detail[:200]})
            _log(f"  FAILED  stage={stage}  {detail[:100]}", level="WARN")
        else:
            outcome = "unknown"
            results["skip"].append({"portal": key, "region": region, "url": url, "type": ptype, "outcome": outcome, "state": state})
            _log(f"  UNKNOWN state={state}", level="WARN")

        rec = {
            "portal": key, "region": region, "url": url, "type": ptype,
            "outcome": outcome, "final_state": state, "final_stage": stage,
            "run_id": run_id,
        }
        _log_result(rec, log_path)

        # Brief pause between portals to be polite
        time.sleep(2)

    # ---- Summary ----
    total = len(portals)
    _log("=" * 70)
    _log(f"SWEEP SUMMARY  total={total}")
    _log(f"  completed : {len(results['pass'])}")
    _log(f"  failed    : {len(results['fail'])}")
    _log(f"  timeout   : {len(results['timeout'])}")
    _log(f"  unknown   : {len(results['skip'])}")
    _log(f"  log       : {log_path}")

    if results["fail"]:
        _log("\nFailed portals:")
        for rec in results["fail"]:
            _log(f"  {rec['region']:10s}  {rec['portal']:30s}  {rec.get('detail', '')[:80]}", level="WARN")

    if results["timeout"]:
        _log("\nTimedout portals:")
        for rec in results["timeout"]:
            _log(f"  {rec['region']:10s}  {rec['portal']:30s}  last_stage={rec.get('last_stage', '?')}", level="WARN")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="JobPilot portal sweep")
    p.add_argument("--dry-run", action="store_true", default=True, help="Don't really submit (default)")
    p.add_argument("--live", action="store_true", help="Enable real submission")
    p.add_argument("--region", default="", help="Filter by region (india|japan|australia|europe|gcc|global)")
    p.add_argument("--limit", type=int, default=5, help="Max jobs per portal (default: 5)")
    p.add_argument("--timeout", type=int, default=300, help="Seconds to wait per portal (default: 300)")
    p.add_argument("--base-url", default="http://127.0.0.1:8765", dest="base_url")
    return p


if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()
    sweep(args)
