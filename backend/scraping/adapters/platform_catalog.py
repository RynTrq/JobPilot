from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse


DEFAULT_LISTING_SELECTORS = (
    "a[href*='/job/']",
    "a[href*='/jobs/']",
    "a[href*='/careers/']",
    "a[href*='/position/']",
    "a[href*='/positions/']",
    "a[href*='jobid=']",
    "a[href*='job_id=']",
    "a[href*='gh_jid=']",
    "[data-testid*='job'] a[href]",
    "[data-automation-id*='job'] a[href]",
)

DEFAULT_DIRECT_PATH_MARKERS = (
    "/job/",
    "/jobs/",
    "/careers/",
    "/career/",
    "/position/",
    "/positions/",
    "/opening/",
    "/openings/",
    "/vacancy/",
    "/vacancies/",
    "/recruit/",
    "/apply/",
)

DEFAULT_APPLY_SELECTORS = (
    "button:has-text('Apply')",
    "a:has-text('Apply')",
    "button:has-text('Apply now')",
    "a:has-text('Apply now')",
    "button:has-text('Start application')",
    "a:has-text('Start application')",
    "button:has-text('Submit application')",
    "a[href*='apply']",
)

DEFAULT_INACTIVE_MARKERS = (
    "application deadline has passed",
    "applications are closed",
    "apply deadline has passed",
    "closed for applications",
    "closed to applications",
    "currently not accepting applications",
    "deadline has passed",
    "expired",
    "filled",
    "job has been closed",
    "job has expired",
    "job is closed",
    "job is no longer available",
    "job listing has expired",
    "job posting has closed",
    "job posting has expired",
    "no longer accepting applications",
    "no longer available",
    "not accepting applications",
    "position has been filled",
    "posting has closed",
    "posting has expired",
    "requisition closed",
    "role has been filled",
    "this job has been closed",
    "this job has expired",
    "this job is no longer available",
    "this position has been closed",
    "this position has been filled",
    "vacancy has closed",
)

DEFAULT_FIELDS_COVERED = (
    "candidate identity",
    "contact details",
    "resume/cv upload",
    "cover letter upload when present",
    "profile links",
    "work authorization",
    "custom screening questions",
    "multi-step browser forms",
)


@dataclass(frozen=True)
class PlatformConfig:
    key: str
    name: str
    region: str
    platform_type: str
    domains: tuple[str, ...]
    integration_method: str = "browser_form"
    auth_required: bool = False
    public_api_docs: str | None = None
    listing_selectors: tuple[str, ...] = DEFAULT_LISTING_SELECTORS
    direct_path_markers: tuple[str, ...] = DEFAULT_DIRECT_PATH_MARKERS
    apply_selectors: tuple[str, ...] = DEFAULT_APPLY_SELECTORS
    inactive_markers: tuple[str, ...] = DEFAULT_INACTIVE_MARKERS
    fields_covered: tuple[str, ...] = DEFAULT_FIELDS_COVERED
    known_limitations: tuple[str, ...] = field(default_factory=tuple)
    username_env: str | None = None
    password_env: str | None = None

    @property
    def adapter_class_name(self) -> str:
        return "".join(part.capitalize() for part in self.key.replace(".", "-").split("-")) + "Adapter"


def cfg(
    key: str,
    name: str,
    region: str,
    platform_type: str,
    domains: tuple[str, ...] | list[str],
    *,
    integration_method: str = "browser_form",
    auth_required: bool = False,
    public_api_docs: str | None = None,
    known_limitations: tuple[str, ...] = (),
    listing_selectors: tuple[str, ...] = DEFAULT_LISTING_SELECTORS,
    direct_path_markers: tuple[str, ...] = DEFAULT_DIRECT_PATH_MARKERS,
    apply_selectors: tuple[str, ...] = DEFAULT_APPLY_SELECTORS,
) -> PlatformConfig:
    prefix = "JOBPILOT_" + key.upper().replace("-", "_")
    return PlatformConfig(
        key=key,
        name=name,
        region=region,
        platform_type=platform_type,
        domains=tuple(domains),
        integration_method=integration_method,
        auth_required=auth_required,
        public_api_docs=public_api_docs,
        known_limitations=known_limitations,
        listing_selectors=listing_selectors,
        direct_path_markers=direct_path_markers,
        apply_selectors=apply_selectors,
        username_env=f"{prefix}_USERNAME" if auth_required else None,
        password_env=f"{prefix}_PASSWORD" if auth_required else None,
    )


PLATFORM_CONFIGS: tuple[PlatformConfig, ...] = (
    # Japan
    cfg("rikunabi", "Rikunabi NEXT", "Japan", "regional_job_board", ("next.rikunabi.com", "job.rikunabi.com"), auth_required=True),
    cfg("mynavi", "Mynavi Tenshoku", "Japan", "regional_job_board", ("tenshoku.mynavi.jp", "job.mynavi.jp"), auth_required=True),
    cfg("doda", "Doda", "Japan", "regional_job_board", ("doda.jp",), auth_required=True),
    cfg("indeed-japan", "Indeed Japan", "Japan", "aggregator", ("jp.indeed.com",), auth_required=True),
    cfg("hello-work", "Hello Work Internet Service", "Japan", "government_portal", ("hellowork.mhlw.go.jp",)),
    cfg("engage", "Engage", "Japan", "regional_job_board", ("en-gage.net",)),
    cfg("green-japan", "Green Japan", "Japan", "tech_job_board", ("green-japan.com",), auth_required=True),
    cfg("wantedly", "Wantedly", "Japan", "talent_marketplace", ("wantedly.com",), auth_required=True),
    cfg("bizreach", "BizReach", "Japan", "executive_job_board", ("bizreach.jp",), auth_required=True),
    cfg("en-japan", "en Japan", "Japan", "regional_job_board", ("employment.en-japan.com", "en-japan.com"), auth_required=True),
    cfg("type-jp", "type", "Japan", "regional_job_board", ("type.jp",), auth_required=True),
    cfg("daijob", "Daijob", "Japan", "bilingual_job_board", ("daijob.com",), auth_required=True),
    cfg("careercross", "CareerCross", "Japan", "bilingual_job_board", ("careercross.com",), auth_required=True),
    cfg("gaijinpot", "GaijinPot Jobs", "Japan", "international_job_board", ("jobs.gaijinpot.com", "gaijinpot.com"), auth_required=True),
    cfg("levtech-career", "LevTech Career", "Japan", "tech_job_board", ("career.levtech.jp",), auth_required=True),
    cfg("forkwell", "Forkwell Jobs", "Japan", "tech_job_board", ("jobs.forkwell.com",), auth_required=True),
    cfg("paiza", "Paiza", "Japan", "tech_job_board", ("paiza.jp",), auth_required=True),
    cfg("tokyodev", "TokyoDev", "Japan", "international_tech_job_board", ("tokyodev.com",), auth_required=True),
    cfg("japan-dev", "Japan Dev", "Japan", "international_tech_job_board", ("japan-dev.com",), auth_required=True),
    cfg("workport", "Workport", "Japan", "regional_job_board", ("workport.co.jp",), auth_required=True),
    # United Kingdom
    cfg("reed", "Reed", "United Kingdom", "regional_job_board", ("reed.co.uk",), auth_required=True),
    cfg("totaljobs", "Totaljobs", "United Kingdom", "regional_job_board", ("totaljobs.com",), auth_required=True),
    cfg("cv-library", "CV-Library", "United Kingdom", "regional_job_board", ("cv-library.co.uk",), auth_required=True),
    cfg("guardian-jobs", "Guardian Jobs", "United Kingdom", "newspaper_job_board", ("jobs.theguardian.com",)),
    cfg("nhs-jobs", "NHS Jobs", "United Kingdom", "government_healthcare_portal", ("jobs.nhs.uk",), auth_required=True),
    cfg("cwjobs", "CWJobs", "United Kingdom", "tech_job_board", ("cwjobs.co.uk",), auth_required=True),
    cfg("adzuna-uk", "Adzuna UK", "United Kingdom", "aggregator", ("adzuna.co.uk",), public_api_docs="https://developer.adzuna.com/"),
    cfg("indeed-uk", "Indeed UK", "United Kingdom", "aggregator", ("uk.indeed.com",), auth_required=True),
    cfg("linkedin-uk", "LinkedIn UK", "United Kingdom", "aggregator", ("uk.linkedin.com",), auth_required=True),
    cfg("glassdoor-uk", "Glassdoor UK", "United Kingdom", "aggregator", ("glassdoor.co.uk",), auth_required=True),
    cfg("prospects", "Prospects", "United Kingdom", "graduate_job_board", ("prospects.ac.uk",)),
    cfg("civil-service-jobs", "Civil Service Jobs", "United Kingdom", "government_portal", ("civilservicejobs.service.gov.uk",), auth_required=True),
    cfg("jobsite-uk", "Jobsite", "United Kingdom", "regional_job_board", ("jobsite.co.uk",), auth_required=True),
    cfg("monster-uk", "Monster UK", "United Kingdom", "aggregator", ("monster.co.uk",), auth_required=True),
    cfg("otta", "Otta", "United Kingdom", "startup_job_board", ("otta.com",), auth_required=True),
    cfg("workinstartups", "WorkInStartups", "United Kingdom", "startup_job_board", ("workinstartups.com",), auth_required=True),
    cfg("escape-the-city", "Escape the City", "United Kingdom", "mission_driven_job_board", ("escapethecity.org",), auth_required=True),
    cfg("hays-uk", "Hays UK", "United Kingdom", "staffing_job_board", ("hays.co.uk",), auth_required=True),
    cfg("michael-page-uk", "Michael Page UK", "United Kingdom", "staffing_job_board", ("michaelpage.co.uk",), auth_required=True),
    cfg("robert-walters-uk", "Robert Walters UK", "United Kingdom", "staffing_job_board", ("robertwalters.co.uk",), auth_required=True),
    cfg("technojobs", "Technojobs", "United Kingdom", "tech_job_board", ("technojobs.co.uk",), auth_required=True),
    cfg("charityjob", "CharityJob", "United Kingdom", "mission_driven_job_board", ("charityjob.co.uk",), auth_required=True),
    cfg("bbc-careers", "BBC Careers", "United Kingdom", "corporate_careers", ("careers.bbc.co.uk",), auth_required=True),
    # Australia and New Zealand
    cfg("seek-au", "SEEK Australia", "Australia", "regional_job_board", ("seek.com.au",), auth_required=True),
    cfg("jora", "Jora", "Australia", "aggregator", ("jora.com", "jora.com.au")),
    cfg("careerone", "CareerOne", "Australia", "regional_job_board", ("careerone.com.au",), auth_required=True),
    cfg("ethical-jobs", "Ethical Jobs", "Australia", "mission_driven_job_board", ("ethicaljobs.com.au",), auth_required=True),
    cfg("apsjobs", "APSJobs", "Australia", "government_portal", ("apsjobs.gov.au",), auth_required=True),
    cfg("nsw-government-jobs", "I Work for NSW", "Australia", "government_portal", ("iworkfor.nsw.gov.au",), auth_required=True),
    cfg("victoria-government-careers", "Careers Victoria", "Australia", "government_portal", ("careers.vic.gov.au",), auth_required=True),
    cfg("queensland-smartjobs", "Queensland Smart Jobs", "Australia", "government_portal", ("smartjobs.qld.gov.au",), auth_required=True),
    cfg("wa-government-jobs", "WA Government Jobs", "Australia", "government_portal", ("search.jobs.wa.gov.au",), auth_required=True),
    cfg("sa-government-jobs", "I Work for SA", "Australia", "government_portal", ("iworkfor.sa.gov.au",), auth_required=True),
    cfg("tasmania-government-jobs", "Tasmanian Government Jobs", "Australia", "government_portal", ("jobs.tas.gov.au",), auth_required=True),
    cfg("act-government-jobs", "ACT Government Jobs", "Australia", "government_portal", ("jobs.act.gov.au",), auth_required=True),
    cfg("nt-government-jobs", "Northern Territory Government Jobs", "Australia", "government_portal", ("jobs.nt.gov.au",), auth_required=True),
    cfg("workforce-australia", "Workforce Australia", "Australia", "government_portal", ("workforceaustralia.gov.au",), auth_required=True),
    cfg("gradconnection-au", "GradConnection Australia", "Australia", "graduate_job_board", ("gradconnection.com.au", "au.gradconnection.com"), auth_required=True),
    cfg("sportspeople", "Sportspeople", "Australia", "industry_job_board", ("sportspeople.com.au",), auth_required=True),
    cfg("artshub", "ArtsHub", "Australia", "industry_job_board", ("artshub.com.au",), auth_required=True),
    cfg("glassdoor-au", "Glassdoor Australia", "Australia", "aggregator", ("glassdoor.com.au",), auth_required=True),
    cfg("hatch", "Hatch", "Australia", "talent_marketplace", ("hatch.team",), auth_required=True),
    # Middle East and North Africa
    cfg("bayt", "Bayt", "Middle East", "regional_job_board", ("bayt.com",), auth_required=True),
    cfg("naukrigulf", "Naukrigulf", "Middle East", "regional_job_board", ("naukrigulf.com",), auth_required=True),
    cfg("gulftalent", "GulfTalent", "Middle East", "regional_job_board", ("gulftalent.com",), auth_required=True),
    cfg("wuzzuf", "Wuzzuf", "Middle East", "regional_job_board", ("wuzzuf.net",), auth_required=True),
    cfg("tanqeeb", "Tanqeeb", "Middle East", "aggregator", ("tanqeeb.com",), auth_required=True),
    cfg("akhtaboot", "Akhtaboot", "Middle East", "regional_job_board", ("akhtaboot.com",), auth_required=True),
    cfg("dubizzle-jobs", "Dubizzle Jobs", "Middle East", "classifieds_job_board", ("dubizzle.com",), auth_required=True),
    cfg("gulf-news-jobs", "Gulf News Jobs", "Middle East", "newspaper_job_board", ("getthat.com",), auth_required=True),
    cfg("laimoon", "Laimoon", "Middle East", "regional_job_board", ("laimoon.com",), auth_required=True),
    cfg("mihnati", "Mihnati", "Middle East", "regional_job_board", ("mihnati.com",), auth_required=True),
    cfg("foundit-gulf", "foundit Gulf", "Middle East", "regional_job_board", ("founditgulf.com",), auth_required=True),
    cfg("qatar-living-jobs", "Qatar Living Jobs", "Middle East", "classifieds_job_board", ("qatarliving.com",), auth_required=True),
    cfg("jobs-in-dubai", "Jobs in Dubai", "Middle East", "regional_job_board", ("jobsindubai.com",), auth_required=True),
    cfg("drjobs", "Drjobs", "Middle East", "regional_job_board", ("drjobs.ae",), auth_required=True),
    cfg("forasna", "Forasna", "Middle East", "regional_job_board", ("forasna.com",), auth_required=True),
    cfg("khaleej-times-jobs", "Khaleej Times Jobs", "Middle East", "newspaper_job_board", ("jobs.khaleejtimes.com",), auth_required=True),
    cfg("oliv", "Oliv", "Middle East", "early_career_job_board", ("oliv.com",), auth_required=True),
    cfg("gulfjobs", "GulfJobs", "Middle East", "regional_job_board", ("gulfjobs.com",), auth_required=True),
    # India
    cfg("naukri", "Naukri", "India", "regional_job_board", ("naukri.com",), auth_required=True),
    cfg("shine", "Shine", "India", "regional_job_board", ("shine.com",), auth_required=True),
    cfg("freshersworld", "Freshersworld", "India", "entry_level_job_board", ("freshersworld.com",), auth_required=True),
    cfg("instahyre", "Instahyre", "India", "talent_marketplace", ("instahyre.com",), auth_required=True),
    cfg("iimjobs", "iimjobs", "India", "specialist_job_board", ("iimjobs.com",), auth_required=True),
    cfg("hirist", "Hirist", "India", "tech_job_board", ("hirist.tech", "hirist.com"), auth_required=True),
    cfg("apna", "Apna", "India", "regional_job_board", ("apna.co",), auth_required=True),
    cfg("foundit-india", "foundit India", "India", "regional_job_board", ("foundit.in",), auth_required=True),
    cfg("timesjobs", "TimesJobs", "India", "regional_job_board", ("timesjobs.com",), auth_required=True),
    cfg("cutshort", "Cutshort", "India", "tech_job_board", ("cutshort.io",), auth_required=True),
    cfg("wellfound-india", "Wellfound India", "India", "startup_job_board", ("in.wellfound.com",), auth_required=True),
    cfg("internshala", "Internshala", "India", "internship_job_board", ("internshala.com",), auth_required=True),
    cfg("linkedin-india", "LinkedIn India", "India", "aggregator", ("in.linkedin.com",), auth_required=True),
    cfg("indeed-india", "Indeed India", "India", "aggregator", ("in.indeed.com",), auth_required=True),
    cfg("glassdoor-india", "Glassdoor India", "India", "aggregator", ("glassdoor.co.in",), auth_required=True),
    cfg("teamlease", "TeamLease", "India", "staffing_job_board", ("teamlease.com",), auth_required=True),
    cfg("workindia", "WorkIndia", "India", "regional_job_board", ("workindia.in",), auth_required=True),
    cfg("herkey", "HerKey", "India", "diversity_job_board", ("herkey.com",), auth_required=True),
    cfg("hirect", "Hirect", "India", "talent_marketplace", ("hirect.in",), auth_required=True),
    cfg("fresherslive", "Fresherslive", "India", "entry_level_job_board", ("fresherslive.com",), auth_required=True),
    cfg("national-career-service", "National Career Service", "India", "government_portal", ("ncs.gov.in",), auth_required=True),
    # Global ATS
    cfg("workday", "Workday", "Global", "ats", ("myworkdayjobs.com", "workdayjobs.com"), auth_required=True, known_limitations=("Per-employer Workday tenants can require separate accounts.",)),
    cfg("greenhouse", "Greenhouse", "Global", "ats", ("greenhouse.io",), integration_method="job_board_api_plus_browser_form", public_api_docs="https://developers.greenhouse.io/job-board.html"),
    cfg("lever", "Lever", "Global", "ats", ("lever.co",), integration_method="postings_api_plus_browser_form", public_api_docs="https://github.com/lever/postings-api"),
    cfg("taleo", "Oracle Taleo", "Global", "ats", ("taleo.net",), auth_required=True, known_limitations=("Taleo application flows vary by tenant and career section.",)),
    cfg("icims", "iCIMS", "Global", "ats", ("icims.com",), integration_method="job_portal_api_plus_browser_form", public_api_docs="https://developer-community.icims.com/applications/applicant-tracking/job-portal", auth_required=True),
    cfg("smartrecruiters", "SmartRecruiters", "Global", "ats", ("smartrecruiters.com",), integration_method="posting_api_plus_browser_form", public_api_docs="https://developers.smartrecruiters.com/docs/posting-api"),
    cfg("jobvite", "Jobvite", "Global", "ats", ("jobvite.com",), integration_method="xml_json_feed_or_iframe_plus_browser_form", public_api_docs="https://careers.jobvite.com/careersites/CareerSite_Options.pdf", auth_required=True),
    cfg("bamboohr", "BambooHR", "Global", "ats", ("bamboohr.com",), integration_method="applicant_tracking_api_plus_browser_form", public_api_docs="https://documentation.bamboohr.com/reference/applicant-tracking", auth_required=True),
    cfg("ashby", "Ashby", "Global", "ats", ("ashbyhq.com",), integration_method="posting_api_plus_browser_form"),
    cfg("dover", "Dover", "Global", "ats", ("app.dover.com",), integration_method="public_application_api"),
    cfg("recruitee", "Recruitee", "Global", "ats", ("recruitee.com",), integration_method="careers_api_plus_browser_form", auth_required=True),
    cfg("teamtailor", "Teamtailor", "Global", "ats", ("teamtailor.com",), integration_method="connect_api_plus_browser_form", auth_required=True),
    cfg("breezyhr", "Breezy HR", "Global", "ats", ("breezy.hr",), auth_required=True),
    cfg("jazzhr", "JazzHR", "Global", "ats", ("applytojob.com", "jazzhr.com"), auth_required=True),
    cfg("pinpoint", "Pinpoint", "Global", "ats", ("pinpointhq.com",), auth_required=True),
    cfg(
        "workable",
        "Workable",
        "Global",
        "ats",
        ("workable.com",),
        listing_selectors=(*DEFAULT_LISTING_SELECTORS, "a[href*='/j/']"),
        direct_path_markers=(*DEFAULT_DIRECT_PATH_MARKERS, "/j/"),
    ),
    cfg("personio", "Personio", "Global", "ats", ("personio.de",), auth_required=True),
    cfg("comeet", "Comeet", "Global", "ats", ("comeet.com",), auth_required=True),
    cfg("rippling", "Rippling Recruiting", "Global", "ats", ("rippling.com",), auth_required=True),
    cfg("oracle-recruiting", "Oracle Recruiting", "Global", "ats", ("oraclecloud.com",), auth_required=True),
    cfg("successfactors", "SAP SuccessFactors", "Global", "ats", ("successfactors.com",), auth_required=True),
    cfg("bullhorn", "Bullhorn", "Global", "ats", ("bullhornstaffing.com",), auth_required=True),
    cfg("zoho-recruit", "Zoho Recruit", "Global", "ats", ("zohorecruit.com",), auth_required=True),
    cfg("freshteam", "Freshteam", "Global", "ats", ("freshteam.com",), auth_required=True),
    cfg("trakstar-hire", "Trakstar Hire", "Global", "ats", ("trakstar.com",), auth_required=True),
    cfg("recruiterbox", "Recruiterbox", "Global", "ats", ("recruiterbox.com",), auth_required=True),
    cfg("peoplehr", "PeopleHR", "Global", "ats", ("peoplehr.net",), auth_required=True),
    cfg("homerun", "Homerun", "Global", "ats", ("homerun.co",), auth_required=True),
    cfg("fountain", "Fountain", "Global", "ats", ("fountain.com",), auth_required=True),
    cfg("applicantpro", "ApplicantPro", "Global", "ats", ("applicantpro.com",), auth_required=True),
    cfg("ukg", "UKG Recruiting", "Global", "ats", ("ukg.com",), auth_required=True),
    cfg("pageup", "PageUp", "Global", "ats", ("pageuppeople.com",), auth_required=True),
    cfg("avature", "Avature", "Global", "ats", ("avature.net",), auth_required=True),
    cfg("cornerstone", "Cornerstone Recruiting", "Global", "ats", ("csod.com",), auth_required=True),
    cfg("silkroad", "SilkRoad Recruiting", "Global", "ats", ("silkroad.com",), auth_required=True),
    cfg("paylocity", "Paylocity Recruiting", "Global", "ats", ("paylocity.com",), auth_required=True),
    cfg("adp-recruiting", "ADP Recruiting", "Global", "ats", ("adp.com",), auth_required=True),
    # Global aggregators and remote boards
    cfg("linkedin", "LinkedIn Jobs", "Global", "aggregator", ("linkedin.com",), auth_required=True),
    cfg("indeed", "Indeed", "Global", "aggregator", ("indeed.com",), auth_required=True),
    cfg("glassdoor", "Glassdoor", "Global", "aggregator", ("glassdoor.com",), auth_required=True),
    cfg("simplyhired", "SimplyHired", "Global", "aggregator", ("simplyhired.com",), auth_required=True),
    cfg("ziprecruiter", "ZipRecruiter", "Global", "aggregator", ("ziprecruiter.com",), auth_required=True),
    cfg("monster", "Monster", "Global", "aggregator", ("monster.com",), auth_required=True),
    cfg("careerbuilder", "CareerBuilder", "Global", "aggregator", ("careerbuilder.com",), auth_required=True),
    cfg("google-careers", "Google Careers", "Global", "corporate_careers", ("google.com",), auth_required=True),
    cfg("microsoft-careers", "Microsoft Careers", "Global", "corporate_careers", ("jobs.careers.microsoft.com",), auth_required=True),
    cfg("adzuna-global", "Adzuna Global", "Global", "aggregator", ("adzuna.com",), public_api_docs="https://developer.adzuna.com/"),
    cfg("talent-com", "Talent.com", "Global", "aggregator", ("talent.com",), auth_required=True),
    cfg("jooble", "Jooble", "Global", "aggregator", ("jooble.org",), auth_required=True),
    cfg("lensa", "Lensa", "Global", "aggregator", ("lensa.com",), auth_required=True),
    cfg("the-muse", "The Muse", "Global", "career_site", ("themuse.com",), auth_required=True),
    cfg("startup-jobs", "Startup Jobs", "Global", "startup_job_board", ("startup.jobs",), auth_required=True),
    cfg("usajobs", "USAJOBS", "United States", "government_portal", ("usajobs.gov",), public_api_docs="https://developer.usajobs.gov/"),
    cfg("dice", "Dice", "United States", "tech_job_board", ("dice.com",), auth_required=True),
    cfg("builtin", "Built In", "United States", "tech_job_board", ("builtin.com",), auth_required=True),
    cfg("wellfound", "Wellfound", "Global", "startup_job_board", ("wellfound.com",), auth_required=True),
    cfg("yc-work-at-a-startup", "Y Combinator Work at a Startup", "Global", "startup_job_board", ("workatastartup.com",), auth_required=True),
    cfg("we-work-remotely", "We Work Remotely", "Global", "remote_job_board", ("weworkremotely.com",)),
    cfg("remote-ok", "Remote OK", "Global", "remote_job_board", ("remoteok.com",)),
    cfg("remote-co", "Remote.co", "Global", "remote_job_board", ("remote.co",), auth_required=True),
    cfg("arc-dev", "Arc.dev", "Global", "remote_job_board", ("arc.dev",), auth_required=True),
    cfg("turing", "Turing", "Global", "remote_job_board", ("turing.com",), auth_required=True),
    cfg("remote-rocketship", "Remote Rocketship", "Global", "remote_job_board", ("remoterocketship.com",), auth_required=True),
    cfg("flexjobs", "FlexJobs", "Global", "remote_job_board", ("flexjobs.com",), auth_required=True),
    cfg("theladders", "The Ladders", "United States", "executive_job_board", ("theladders.com",), auth_required=True),
    cfg("hired", "Hired", "Global", "talent_marketplace", ("hired.com",), auth_required=True),
    cfg("idealist", "Idealist", "Global", "mission_driven_job_board", ("idealist.org",), auth_required=True),
    cfg("higheredjobs", "HigherEdJobs", "United States", "education_job_board", ("higheredjobs.com",), auth_required=True),
    cfg("governmentjobs", "GovernmentJobs", "United States", "government_portal", ("governmentjobs.com",), auth_required=True),
    cfg("calcareers", "CalCareers", "United States", "government_portal", ("calcareers.ca.gov",), auth_required=True),
    cfg("canada-job-bank", "Canada Job Bank", "Canada", "government_portal", ("jobbank.gc.ca",), auth_required=True),
    cfg("eluta", "Eluta", "Canada", "aggregator", ("eluta.ca",)),
    cfg("workopolis", "Workopolis", "Canada", "regional_job_board", ("workopolis.com",), auth_required=True),
    cfg("jobboom", "Jobboom", "Canada", "regional_job_board", ("jobboom.com",), auth_required=True),
    cfg("talentegg", "TalentEgg", "Canada", "entry_level_job_board", ("talentegg.ca",), auth_required=True),
    cfg("power-to-fly", "PowerToFly", "Global", "diversity_job_board", ("powertofly.com",), auth_required=True),
    cfg("tech-ladies", "Tech Ladies", "Global", "diversity_job_board", ("hiretechladies.com",), auth_required=True),
    # Europe
    cfg("welcome-to-the-jungle", "Welcome to the Jungle", "Europe", "regional_job_board", ("welcometothejungle.com",), auth_required=True),
    cfg("stepstone-de", "StepStone Germany", "Germany", "regional_job_board", ("stepstone.de",), auth_required=True),
    cfg("xing-jobs", "XING Jobs", "Germany", "professional_network", ("xing.com",), auth_required=True),
    cfg("arbeitsagentur", "Bundesagentur fuer Arbeit", "Germany", "government_portal", ("arbeitsagentur.de",), auth_required=True),
    cfg("apec", "APEC", "France", "professional_job_board", ("apec.fr",), auth_required=True),
    cfg("france-travail", "France Travail", "France", "government_portal", ("francetravail.fr", "pole-emploi.fr"), auth_required=True),
    cfg("infojobs-spain", "InfoJobs Spain", "Spain", "regional_job_board", ("infojobs.net",), auth_required=True),
    cfg("tecnoempleo", "Tecnoempleo", "Spain", "tech_job_board", ("tecnoempleo.com",), auth_required=True),
    cfg("eures", "EURES", "Europe", "government_portal", ("eures.europa.eu",)),
    cfg("jobs-ie", "Jobs.ie", "Ireland", "regional_job_board", ("jobs.ie",), auth_required=True),
    cfg("irishjobs", "IrishJobs", "Ireland", "regional_job_board", ("irishjobs.ie",), auth_required=True),
    cfg("jobs-ch", "jobs.ch", "Switzerland", "regional_job_board", ("jobs.ch",), auth_required=True),
    cfg("jobup", "Jobup", "Switzerland", "regional_job_board", ("jobup.ch",), auth_required=True),
    cfg("nofluffjobs", "No Fluff Jobs", "Europe", "tech_job_board", ("nofluffjobs.com",), auth_required=True),
    cfg("pracuj", "Pracuj.pl", "Poland", "regional_job_board", ("pracuj.pl",), auth_required=True),
    cfg("justjoinit", "Just Join IT", "Poland", "tech_job_board", ("justjoin.it",), auth_required=True),
    cfg("djinni", "Djinni", "Europe", "tech_job_board", ("djinni.co",), auth_required=True),
    cfg("karriere-at", "karriere.at", "Austria", "regional_job_board", ("karriere.at",), auth_required=True),
    cfg("jobindex", "Jobindex", "Denmark", "regional_job_board", ("jobindex.dk",), auth_required=True),
    cfg("jobbnorge", "Jobbnorge", "Norway", "regional_job_board", ("jobbnorge.no",), auth_required=True),
    cfg("arbetsformedlingen", "Arbetsformedlingen", "Sweden", "government_portal", ("arbetsformedlingen.se",), auth_required=True),
    # Latin America, Africa, and Asia-Pacific
    cfg("computrabajo", "Computrabajo", "Latin America", "regional_job_board", ("computrabajo.com",), auth_required=True),
    cfg("bumeran", "Bumeran", "Latin America", "regional_job_board", ("bumeran.com",), auth_required=True),
    cfg("zonajobs", "ZonaJobs", "Latin America", "regional_job_board", ("zonajobs.com.ar",), auth_required=True),
    cfg("infojobs-brazil", "InfoJobs Brazil", "Brazil", "regional_job_board", ("infojobs.com.br",), auth_required=True),
    cfg("catho", "Catho", "Brazil", "regional_job_board", ("catho.com.br",), auth_required=True),
    cfg("gupy", "Gupy", "Brazil", "ats", ("gupy.io",), auth_required=True),
    cfg("vagas", "Vagas.com.br", "Brazil", "regional_job_board", ("vagas.com.br",), auth_required=True),
    cfg("occ-mundial", "OCCMundial", "Mexico", "regional_job_board", ("occ.com.mx",), auth_required=True),
    cfg("konzerta", "Konzerta", "Panama", "regional_job_board", ("konzerta.com",), auth_required=True),
    cfg("elempleo", "Elempleo", "Colombia", "regional_job_board", ("elempleo.com",), auth_required=True),
    cfg("pnet", "PNet", "South Africa", "regional_job_board", ("pnet.co.za",), auth_required=True),
    cfg("careers24", "Careers24", "South Africa", "regional_job_board", ("careers24.com",), auth_required=True),
    cfg("jobberman", "Jobberman", "Africa", "regional_job_board", ("jobberman.com",), auth_required=True),
    cfg("brightermonday", "BrighterMonday", "Africa", "regional_job_board", ("brightermonday.com",), auth_required=True),
    cfg("myjobmag", "MyJobMag", "Africa", "regional_job_board", ("myjobmag.com",), auth_required=True),
    cfg("ethiojobs", "Ethiojobs", "Ethiopia", "regional_job_board", ("ethiojobs.net",), auth_required=True),
    cfg("jobstreet", "JobStreet", "Asia-Pacific", "regional_job_board", ("jobstreet.com",), auth_required=True),
    cfg("jobsdb", "JobsDB", "Asia-Pacific", "regional_job_board", ("jobsdb.com",), auth_required=True),
    cfg("kalibrr", "Kalibrr", "Asia-Pacific", "regional_job_board", ("kalibrr.com",), auth_required=True),
    cfg("vietnamworks", "VietnamWorks", "Vietnam", "regional_job_board", ("vietnamworks.com",), auth_required=True),
    cfg("glints", "Glints", "Asia-Pacific", "regional_job_board", ("glints.com",), auth_required=True),
    cfg("techinasia-jobs", "Tech in Asia Jobs", "Asia-Pacific", "tech_job_board", ("techinasia.com",), auth_required=True),
    cfg("bossjob", "Bossjob", "Asia-Pacific", "regional_job_board", ("bossjob.com",), auth_required=True),
)

PLATFORMS_BY_KEY = {platform.key: platform for platform in PLATFORM_CONFIGS}


def platform_count() -> int:
    return len(PLATFORM_CONFIGS)


def find_platform_config(url: str) -> PlatformConfig | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    for platform in PLATFORM_CONFIGS:
        if any(host_matches(host, domain) for domain in platform.domains):
            return platform
    return None


def host_matches(host: str, domain: str) -> bool:
    normalized = domain.lower().lstrip(".")
    return host == normalized or host.endswith(f".{normalized}")


def coverage_rows() -> list[dict[str, str]]:
    rows = []
    for platform in PLATFORM_CONFIGS:
        rows.append(
            {
                "key": platform.key,
                "name": platform.name,
                "region": platform.region,
                "type": platform.platform_type,
                "domains": ", ".join(platform.domains),
                "integration_method": platform.integration_method,
                "auth": "required" if platform.auth_required else "not required for public search",
                "fields": "; ".join(platform.fields_covered),
                "limitations": "; ".join(platform.known_limitations) if platform.known_limitations else "Uses shared browser workflow; tenant-specific changes reported at runtime.",
                "api_docs": platform.public_api_docs or "",
            }
        )
    return rows
