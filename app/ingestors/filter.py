"""
app/ingestors/filter.py

Skill-based relevance filtering and domain-level deduplication for
fetched job postings.

Goal: Quality over quantity.
  - Keep only jobs relevant to the candidate’s skill set.
  - Deduplicate by role domain (at most MAX_PER_DOMAIN jobs per domain per company).
  - Return at most MAX_JOBS_PER_COMPANY jobs per company.
  - Prefer India / Remote locations when available.

Usage (inside an ingestor or manager):
    from app.ingestors.filter import filter_and_diversify
    kept = filter_and_diversify(raw_jobs, company="GitLab")
"""

import html as _html
import logging
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = ["filter_and_diversify", "SKILL_KEYWORDS", "MAX_JOBS_PER_COMPANY", "MAX_PER_DOMAIN"]

# ------------------------------------------------------------------ #
# Tunable constants                                                    #
# ------------------------------------------------------------------ #
MAX_JOBS_PER_COMPANY: int = 10
MAX_PER_DOMAIN: int = 2
DESC_SCAN_CHARS: int = 600

# ------------------------------------------------------------------ #
# Candidate skill keywords (lowercase)                                #
# ------------------------------------------------------------------ #
SKILL_KEYWORDS: List[str] = [
    # Languages
    "java", "python", "javascript", "typescript", "go", "golang",
    "kotlin", "scala", "ruby", "rust",
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
# Domain classifier (first match wins)                                #
# ------------------------------------------------------------------ #
_DOMAIN_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("devops",    re.compile(r"devops|\bsre\b|platform eng|infra eng|cloud eng", re.I)),
    ("data",      re.compile(r"data eng|data scientist|\bml eng|machine learning|analytics eng|\betl\b", re.I)),
    ("frontend",  re.compile(r"frontend|front-end|\bui eng|\breact\b|\bangular\b|\bvue\b", re.I)),
    ("qa",        re.compile(r"qa eng|quality assurance|test eng|\bsdet\b|automation test", re.I)),
    ("backend",   re.compile(r"backend|back-end|api eng|server.side", re.I)),
    ("fullstack", re.compile(r"full.?stack", re.I)),
    ("mobile",    re.compile(r"android|\bios\b|mobile eng|react native|flutter", re.I)),
    ("security",  re.compile(r"security eng|appsec|devsecops|pen test", re.I)),
    ("swe",       re.compile(r"software eng|software dev|\bsde\b|\bswe\b", re.I)),
]

_PREFERRED_LOCATIONS: re.Pattern = re.compile(
    r"india|remote|worldwide|global|hyderabad|bangalore|bengaluru|mumbai"
    r"|chennai|pune|delhi|noida|gurugram|gurgaon",
    re.I,
)

# Simple HTML tag stripper used before scoring HTML descriptions
_HTML_TAG_RE: re.Pattern = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Strip HTML tags and unescape entities. Safe to call on plain text."""
    if "<" in text:
        text = _HTML_TAG_RE.sub(" ", _html.unescape(text))
    return text


def _classify_domain(title: str, description: str = "") -> str:
    """Return a broad domain bucket for the role."""
    text = "{} {}".format(title, description[:DESC_SCAN_CHARS])
    for domain, pattern in _DOMAIN_PATTERNS:
        if pattern.search(text):
            return domain
    return "other"


def _relevance_score(title: str, description: str) -> int:
    """
    Count skill keyword hits across title + description.

    Fix: previously, keywords found in title_lower were *also* found in
    text_lower (since text_lower = title + desc), causing double-counting.
    Now: title hits get +3, desc-only hits get +1 (mutually exclusive).
    """
    title_lower = title.lower()
    # Only the description portion, not the full combined text
    desc_lower = description[:DESC_SCAN_CHARS].lower()

    score = 0
    for kw in SKILL_KEYWORDS:
        if kw in title_lower:
            score += 3          # title match only
        elif kw in desc_lower:  # elif — avoids double-counting
            score += 1
    return score


def _prefers_india(location: str) -> bool:
    """True if the location string matches an India/Remote preference."""
    return bool(_PREFERRED_LOCATIONS.search(location or ""))


def filter_and_diversify(
    raw_jobs: List[Optional[dict]],
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
      1. Strip HTML from descriptions before scoring.
      2. Score every job for skill relevance (title 3x, desc 1x, no double-count).
      3. Drop jobs with score < min_score.
      4. Sort: India/Remote first, then score descending (fixed sort key).
      5. Walk sorted list; pick a job only if its domain bucket is not full.
      6. Stop once max_jobs is reached.
    """
    if not raw_jobs:
        return []

    scored: List[Tuple[int, bool, dict]] = []
    for job in raw_jobs:
        if not isinstance(job, dict):   # guard against None / malformed entries
            continue

        title = job.get(title_key) or ""
        raw_desc = job.get(description_key) or ""
        desc = _strip_html(raw_desc)     # strip HTML before keyword matching
        loc = job.get(location_key) or ""

        score = _relevance_score(title, desc)
        if score < min_score:
            continue

        prefers = _prefers_india(loc)
        scored.append((score, prefers, job))

    # Fix: previously used sort(..., reverse=True) on a (score, prefers_india) tuple.
    # Both fields flip direction together, which is wrong when prefers_india=False
    # jobs with high scores beat prefers_india=True jobs with lower scores.
    # Fix: use a key that sorts prefers_india descending and score descending independently.
    scored.sort(key=lambda x: (not x[1], -x[0]))  # False>True inverted; score negated

    domain_counts: Dict[str, int] = {}
    selected: List[dict] = []

    for score, prefers_india_flag, job in scored:
        if len(selected) >= max_jobs:
            break

        title = job.get(title_key) or ""
        raw_desc = job.get(description_key) or ""
        desc = _strip_html(raw_desc)
        domain = _classify_domain(title, desc)

        current = domain_counts.get(domain, 0)
        if current >= max_per_domain:
            continue

        domain_counts[domain] = current + 1
        selected.append(job)

    dropped = len(raw_jobs) - len(selected)
    logger.info(
        "[filter] %s: %d raw -> %d kept (dropped %d). Domains: %s",
        company or "(unknown)", len(raw_jobs), len(selected), dropped, domain_counts,
    )
    return selected
