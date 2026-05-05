"""
app/ingestors/filter.py

Skill-based relevance filtering and domain-level deduplication for
fetched job postings.

Goal: Quality over quantity.
  - Keep only jobs relevant to the candidate's skill set.
  - Deduplicate by role domain (at most MAX_PER_DOMAIN jobs per domain per company).
  - Return at most MAX_JOBS_PER_COMPANY jobs per company.
  - Prefer India / Remote locations when available.

Usage (inside an ingestor or manager):
    from app.ingestors.filter import filter_and_diversify
    kept = filter_and_diversify(raw_jobs, company="GitLab")
"""

import re
from typing import List

# ------------------------------------------------------------------ #
# Tunable constants                                                    #
# ------------------------------------------------------------------ #
MAX_JOBS_PER_COMPANY = 10
MAX_PER_DOMAIN = 2          # at most 2 jobs per domain bucket per company

# ------------------------------------------------------------------ #
# Candidate skill keywords                                            #
# Extend this list freely — lowercase, no spaces required.           #
# ------------------------------------------------------------------ #
SKILL_KEYWORDS: list[str] = [
    # Languages
    "java", "python", "javascript", "typescript", "go", "golang",
    "kotlin", "scala", "ruby", "rust", "c++", "c#",
    # Frameworks / libs
    "spring", "spring boot", "springboot", "django", "fastapi", "flask",
    "react", "angular", "vue", "next.js", "nextjs", "node", "nodejs",
    "express", "graphql", "rest api", "grpc",
    # Cloud / DevOps
    "aws", "gcp", "azure", "kubernetes", "k8s", "docker", "terraform",
    "ansible", "helm", "ci/cd", "jenkins", "github actions", "argocd",
    "devops", "sre", "platform engineering",
    # Data
    "sql", "postgresql", "mysql", "mongodb", "redis", "kafka", "spark",
    "elasticsearch", "data engineering", "etl",
    # General SWE
    "backend", "full stack", "fullstack", "api", "microservices",
    "distributed systems", "system design", "software engineer",
    "software development", "sde", "swe",
    # QA / Test
    "selenium", "playwright", "cypress", "automation testing", "qa engineer",
    # Frontend
    "frontend", "ui engineer", "web developer",
]

# ------------------------------------------------------------------ #
# Domain classifier                                                   #
# Maps a job title / description snippet to a broad domain bucket.   #
# ------------------------------------------------------------------ #
# Order matters: first match wins.
_DOMAIN_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("devops",    re.compile(r"devops|sre|platform eng|infra|cloud eng", re.I)),
    ("data",      re.compile(r"data eng|data scientist|ml eng|machine learning|analytics eng|etl", re.I)),
    ("frontend",  re.compile(r"frontend|front-end|ui eng|react|angular|vue", re.I)),
    ("qa",        re.compile(r"qa eng|quality assurance|test eng|sdet|automation test", re.I)),
    ("backend",   re.compile(r"backend|back-end|api eng|server.side", re.I)),
    ("fullstack", re.compile(r"full.?stack", re.I)),
    ("mobile",    re.compile(r"android|ios|mobile eng|react native|flutter", re.I)),
    ("security",  re.compile(r"security eng|appsec|devsecops|pen test", re.I)),
    ("swe",       re.compile(r"software eng|software dev|sde|swe", re.I)),  # catch-all SWE
]

# Location preference: jobs matching these are scored higher
_PREFERRED_LOCATIONS = re.compile(
    r"india|remote|hyderabad|bangalore|bengaluru|mumbai|chennai|pune|delhi|noida|gurugram",
    re.I
)


def _classify_domain(title: str, description: str = "") -> str:
    """Return a domain bucket for the role, e.g. 'backend', 'devops', 'swe'."""
    text = f"{title} {description[:500]}"
    for domain, pattern in _DOMAIN_PATTERNS:
        if pattern.search(text):
            return domain
    return "other"


def _relevance_score(title: str, description: str) -> int:
    """
    Count how many skill keywords appear in the title + description.
    Title matches count 3x (more specific signal).
    Returns 0 if no match at all.
    """
    text_lower = (title + " " + description[:1000]).lower()
    title_lower = title.lower()
    score = 0
    for kw in SKILL_KEYWORDS:
        if kw in title_lower:
            score += 3
        elif kw in text_lower:
            score += 1
    return score


def _prefers_india(location: str) -> bool:
    return bool(_PREFERRED_LOCATIONS.search(location or ""))


def filter_and_diversify(
    raw_jobs: List[dict],
    company: str = "",
    title_key: str = "title",
    description_key: str = "description",
    location_key: str = "location",
    max_jobs: int = MAX_JOBS_PER_COMPANY,
    max_per_domain: int = MAX_PER_DOMAIN,
    min_score: int = 1,
) -> List[dict]:
    """
    Filter and diversify a list of raw job dicts from a single company.

    Steps:
      1. Score every job for skill relevance.
      2. Drop jobs with score < min_score (not relevant to candidate).
      3. Sort by: India/Remote first, then score descending.
      4. Walk sorted list; pick a job only if its domain bucket is not full yet.
      5. Stop once max_jobs is reached.

    Args:
        raw_jobs:         raw job list from ingestor.fetch_jobs()
        company:          company name (for logging only)
        title_key:        dict key for job title
        description_key:  dict key for job description
        location_key:     dict key for location string
        max_jobs:         cap per company (default MAX_JOBS_PER_COMPANY = 10)
        max_per_domain:   max picks per domain bucket (default MAX_PER_DOMAIN = 2)
        min_score:        minimum skill keyword hits to pass relevance gate (default 1)

    Returns:
        Filtered & diversified list of raw job dicts (at most max_jobs).
    """
    if not raw_jobs:
        return []

    scored: list[tuple[int, bool, dict]] = []
    for job in raw_jobs:
        title = job.get(title_key, "") or ""
        desc  = job.get(description_key, "") or ""
        loc   = job.get(location_key, "") or ""

        score = _relevance_score(title, desc)
        if score < min_score:
            continue  # not relevant

        prefers = _prefers_india(loc)
        scored.append((score, prefers, job))

    # Sort: India/Remote preferred (True > False), then score descending
    scored.sort(key=lambda x: (x[1], x[0]), reverse=True)

    domain_counts: dict[str, int] = {}
    selected: list[dict] = []

    for score, prefers_india, job in scored:
        if len(selected) >= max_jobs:
            break

        title = job.get(title_key, "") or ""
        desc  = job.get(description_key, "") or ""
        domain = _classify_domain(title, desc)

        current = domain_counts.get(domain, 0)
        if current >= max_per_domain:
            continue  # domain already full

        domain_counts[domain] = current + 1
        selected.append(job)

    dropped = len(raw_jobs) - len(selected)
    print(
        f"[filter] {company}: {len(raw_jobs)} raw → {len(selected)} kept "
        f"(dropped {dropped}). Domains: {domain_counts}"
    )
    return selected
