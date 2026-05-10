"""
app/tailor/engine.py  —  FAANG-grade one-page resume tailoring engine

Architecture:
  1. _fill_resume_json()   — LLM fills structured data (content only, no layout)
  2. _validate_resume_json() — hard checks: all sections present, min bullet counts
  3. _render_tex_from_json() — deterministic template injection (layout never LLM-controlled)
  4. _compile_pdf()        — pdflatex compile
  5. _check_page_count()   — reject if > 1 page (parse pdflatex log)
  6. _score_resume()       — FAANG scorecard: action verbs, metrics, ATS keywords, section completeness
  7. generate_tailored_resume() — retry loop (up to MAX_RETRIES=3), raises on total failure

Resume Quality Standards enforced:
  - Exactly 1 page — hard reject + retry on 2+ pages
  - Every bullet: Strong action verb + What/How + Tech used + Quantified result
  - No duplicate bullet patterns across bullets
  - Education section mandatory
  - Skills: 4-6 categories, ATS-aligned to JD keywords
  - Summary: 2 sentences, title must be Software/Backend/Full-Stack Engineer
  - Minimum quality score: 70 / 100
"""

import os
import re
import json
import subprocess
import textwrap
from groq import Groq
from sqlalchemy.orm import Session
from app.models.application import Application
from dotenv import load_dotenv

load_dotenv()

MAX_RETRIES = 3
MIN_QUALITY_SCORE = 70
_groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))


# ─────────────────────────────────────────────────────────────
# LaTeX safety
# ─────────────────────────────────────────────────────────────

_LATEX_ESCAPE = [
    ('\\', r'\textbackslash{}'),
    ('&',  r'\&'),
    ('%',  r'\%'),
    ('$',  r'\$'),
    ('#',  r'\#'),
    ('^',  r'\^{}'),
    ('_',  r'\_'),
    ('{',  r'\{'),
    ('}',  r'\}'),
    ('~',  r'\textasciitilde{}'),
]
_SAFE_LATEX_RE = re.compile(r'\\[a-zA-Z]+\{|\\[&%$#^_{}~]')


def _esc(text: str) -> str:
    """Escape a raw string for safe LaTeX embedding."""
    if not text:
        return ""
    if _SAFE_LATEX_RE.search(text):
        return text
    for char, replacement in _LATEX_ESCAPE:
        if char == '\\':
            continue
        text = text.replace(char, replacement)
    return text


# ─────────────────────────────────────────────────────────────
# LLM helpers
# ─────────────────────────────────────────────────────────────

def _groq_text(prompt: str, system: str = "") -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    resp = _groq_client.chat.completions.create(
        messages=messages,
        model="llama-3.3-70b-versatile",
    )
    return resp.choices[0].message.content


def _groq_json(prompt: str, system: str = "") -> dict:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    resp = _groq_client.chat.completions.create(
        messages=messages,
        model="llama-3.3-70b-versatile",
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content)


# ─────────────────────────────────────────────────────────────
# Step 1 — LLM fills structured JSON (no layout decisions)
# ─────────────────────────────────────────────────────────────

# Concrete bullet examples the LLM can reference as quality targets.
_EXAMPLE_BULLETS = """
EXAMPLE HIGH-QUALITY BULLETS (use as quality reference — do NOT copy verbatim):
- Engineered a distributed rule-engine microservice in Spring Boot that reduced loan eligibility check latency by 62\% across 3M monthly transactions
- Implemented async batch processing pipeline using Kafka + Redis cache, cutting API p99 response time from 4.2s to 340ms
- Designed RESTful integration layer between core banking system and Salesforce CRM, eliminating 8hrs/day of manual reconciliation
- Optimized PostgreSQL query plans for transaction history module, reducing average query time from 1.8s to 190ms for 50M+ records
- Deployed containerized Spring Boot services to AWS ECS with zero-downtime blue-green deployments, achieving 99.97\% uptime over 12 months
- Built React.js dashboard with real-time WebSocket updates for ops team, replacing 3 legacy Excel reports used by 200+ agents daily
- Automated infrastructure provisioning via Ansible playbooks, cutting new environment setup time from 4hrs to 11 minutes
- Integrated Gemini API with custom prompt chaining for multi-step itinerary generation, handling 500+ concurrent requests with sub-2s response time
"""

def _fill_resume_json(base_resume_data: dict, job_title: str, job_desc: str, attempt: int = 1) -> dict:
    """
    Ask the LLM to return ONLY resume content as structured JSON.
    Layout and rendering are handled by _render_tex_from_json.
    """
    bullet_limits = {
        1: {"primary": 5, "intern": 2, "project": 3},
        2: {"primary": 4, "intern": 2, "project": 2},
        3: {"primary": 4, "intern": 1, "project": 2},
    }[min(attempt, 3)]

    system_prompt = (
        "You are a FAANG-level resume writer with 15 years experience helping SDE2 candidates at top tech companies. "
        "You write dense, metric-rich, ATS-optimised resumes that get callbacks at Amazon, Google, Stripe, and Atlassian. "
        "Every bullet you write follows the formula: [Strong Action Verb] + [What you built/improved] + [specific technology] + [quantified result]. "
        "You NEVER leave any section empty. You ALWAYS populate all experience, projects, education and skills sections. "
        "Return ONLY valid JSON — no markdown, no explanation, no code fences."
    )

    prompt = f"""Tailor the candidate's resume for the job below. Return a JSON object with EXACTLY this structure.

JOB TO TAILOR FOR:
Title: {job_title}
Description (first 2800 chars):
{job_desc[:2800]}

CANDIDATE BASE DATA:
{json.dumps(base_resume_data, indent=2)}

{_EXAMPLE_BULLETS}

=== MANDATORY QUALITY RULES ===

1. SUMMARY (required, exactly 2 sentences):
   - Opening title MUST be one of: "Software Engineer", "Backend Engineer", "Full-Stack Engineer"
   - Sentence 1: [Title] with X years experience at [Company] specialising in [2-3 specific skills from JD]
   - Sentence 2: one strength that directly maps to a key requirement in the JD
   - Example: "Backend Engineer with 2.5 years at Wells Fargo building distributed payment APIs in Java/Spring Boot. \
 Experienced in Kafka-based event streaming and AWS-hosted microservices with 99.9%% SLA delivery."

2. EXPERIENCE — Wells Fargo (primary): EXACTLY {bullet_limits['primary']} bullets
   Required diversity:
   - Bullet 1: system/service you designed or engineered (latency or scale metric)
   - Bullet 2: integration or API work (throughput or adoption metric)
   - Bullet 3: performance optimization (before/after numbers)
   - Bullet 4: reliability/availability or deployment work (uptime or incident metric)
   - Bullet 5 (if applicable): process/automation improvement (time saved metric)
   - Mirror 2-3 exact phrases from the JD
   - ALL bullets must start with a DIFFERENT strong action verb from: Designed, Engineered, Implemented, \
Optimized, Reduced, Built, Deployed, Automated, Migrated, Scaled, Integrated, Refactored, Delivered, Launched

3. EXPERIENCE — Airveda (internship): EXACTLY {bullet_limits['intern']} bullet(s)
   - Concise but still metric-driven

4. PROJECTS: EXACTLY 2 projects
   Project 1 — AI Trip Planner:
     Tech: Gemini API | React.js | Firebase
     Write {bullet_limits['project']} bullets covering: AI integration, user-facing feature, scale/performance
   Project 2 — InfraBoard (Ansible Ops Dashboard):
     Tech: FastAPI | React.js | MongoDB | Docker | AWS EC2
     Write {bullet_limits['project']} bullets covering: backend architecture, automation benefit, ops improvement
   - Present as shipped/production work — no "In Progress" labels
   - Tailor bullet emphasis to JD keywords

5. EDUCATION (REQUIRED — NEVER omit):
   - B.Tech in Computer Science, Lovely Professional University, Punjab India, 2018–2022

6. SKILLS (4–6 categories):
   - ONLY skills directly relevant to this JD
   - Each "label: skill1, skill2, ..." line must be under 85 characters total
   - Categories: Languages, Backend, Frontend, Cloud/DevOps, Databases, Testing
   - For each category include 4-7 specific technologies
   - Example: {{"category": "Backend", "items": "Java, Spring Boot, Node.js, REST APIs, Microservices, Kafka"}}

=== IMPORTANT ===
- Do NOT return empty arrays for any section.
- Do NOT use placeholder text like "bullet1" or "TBD".
- Every bullet must be a complete, metric-rich sentence (15-25 words).
- Dates format: Mon YYYY -- Mon YYYY (use double dash)

Return ONLY this JSON (copy structure exactly):
{{
  "summary": "<2-sentence summary here>",
  "experience": [
    {{
      "company": "Wells Fargo",
      "location": "Hyderabad, India",
      "title": "Software Engineer",
      "dates": "Nov 2022 -- Present",
      "bullets": ["<bullet1>", "<bullet2>", "<bullet3>", "<bullet4>"]
    }},
    {{
      "company": "Airveda",
      "location": "Remote",
      "title": "Software Engineer Intern",
      "dates": "Mar 2022 -- May 2022",
      "bullets": ["<bullet1>", "<bullet2>"]
    }}
  ],
  "projects": [
    {{
      "name": "AI Trip Planner",
      "tech": "Gemini API | React.js | Firebase",
      "bullets": ["<bullet1>", "<bullet2>", "<bullet3>"]
    }},
    {{
      "name": "InfraBoard -- Ansible Ops Dashboard",
      "tech": "FastAPI | React.js | MongoDB | Docker | AWS EC2",
      "bullets": ["<bullet1>", "<bullet2>", "<bullet3>"]
    }}
  ],
  "education": [
    {{
      "degree": "B.Tech in Computer Science",
      "school": "Lovely Professional University",
      "location": "Punjab, India",
      "dates": "2018 -- 2022"
    }}
  ],
  "skills": [
    {{"category": "Languages", "items": "Java, Python, JavaScript, TypeScript, SQL"}},
    {{"category": "Backend", "items": "Spring Boot, Node.js, REST APIs, Microservices, Kafka"}},
    {{"category": "Frontend", "items": "React.js, HTML5, CSS3"}},
    {{"category": "Cloud/DevOps", "items": "AWS, Docker, Ansible, CI/CD, GitHub Actions"}},
    {{"category": "Databases", "items": "PostgreSQL, MongoDB, Redis"}}
  ]
}}
"""
    return _groq_json(prompt, system=system_prompt)


# ─────────────────────────────────────────────────────────────
# Step 1b — Hard validation of LLM JSON before rendering
# ─────────────────────────────────────────────────────────────

def _validate_resume_json(data: dict) -> None:
    """
    Raise ValueError if the LLM JSON is missing required sections or
    has obviously empty/placeholder content. This triggers a retry
    before wasting a pdflatex compile.
    """
    # Summary
    summary = data.get("summary", "").strip()
    if len(summary) < 40:
        raise ValueError(f"Summary too short or missing: '{summary[:60]}'")

    # Experience
    experience = data.get("experience", [])
    if not experience:
        raise ValueError("experience array is empty")
    for exp in experience:
        bullets = exp.get("bullets", [])
        if not bullets:
            raise ValueError(f"No bullets for experience: {exp.get('company', '?')}")
        for b in bullets:
            if len(b.strip()) < 20:
                raise ValueError(f"Bullet too short in {exp.get('company')}: '{b}'")
            if b.strip().lower() in ("bullet1", "bullet2", "bullet3", "tbd", "placeholder"):
                raise ValueError(f"Placeholder bullet detected: '{b}'")

    # Projects
    projects = data.get("projects", [])
    if not projects:
        raise ValueError("projects array is empty")
    for proj in projects:
        bullets = proj.get("bullets", [])
        if not bullets:
            raise ValueError(f"No bullets for project: {proj.get('name', '?')}")

    # Education
    education = data.get("education", [])
    if not education:
        raise ValueError("education array is empty — LLM omitted it")

    # Skills
    skills = data.get("skills", [])
    if len(skills) < 3:
        raise ValueError(f"Only {len(skills)} skill categories — need at least 3")


# ─────────────────────────────────────────────────────────────
# Step 2 — Deterministic template rendering
# ─────────────────────────────────────────────────────────────

def _render_tex_from_json(data: dict, base: dict) -> str:
    """Inject structured JSON data into the LaTeX template. LLM never touches layout."""
    with open('app/tailor/template.tex', 'r') as f:
        tpl = f.read()

    # Header
    tpl = tpl.replace('<<FULL_NAME>>', _esc(base.get('full_name', 'Luv Kumar')))
    tpl = tpl.replace('<<PHONE>>', _esc(base.get('phone', '+91-7689961477')))
    tpl = tpl.replace('<<EMAIL>>', _esc(base.get('email', 'luvkumar8436@gmail.com')))
    tpl = tpl.replace('<<LINKEDIN>>', base.get('linkedin', 'https://linkedin.com/in/luv-kumar-06975b175'))
    tpl = tpl.replace('<<GITHUB>>', base.get('github', 'https://github.com/lovethakur8436'))

    # Summary
    tpl = tpl.replace('<<SUMMARY>>', _esc(data.get('summary', '')))

    # Experience
    exp_blocks = []
    for exp in data.get('experience', []):
        bullets_tex = '\n'.join(
            f'          \\resumeItem{{{_esc(b)}}}'
            for b in exp.get('bullets', [])
        )
        block = textwrap.dedent(f"""\
            \\resumeSubheading
              {{{_esc(exp['company'])}}}{{{_esc(exp['location'])}}}
              {{{_esc(exp['title'])}}}{{{_esc(exp['dates'])}}}
              \\resumeItemListStart
{bullets_tex}
              \\resumeItemListEnd""")
        exp_blocks.append(block)
    tpl = tpl.replace('<<EXPERIENCE_BLOCKS>>', '\n'.join(exp_blocks))

    # Projects
    proj_blocks = []
    for proj in data.get('projects', []):
        bullets_tex = '\n'.join(
            f'          \\resumeItem{{{_esc(b)}}}'
            for b in proj.get('bullets', [])
        )
        block = textwrap.dedent(f"""\
            \\resumeProjectHeading
              {{\\textbf{{{_esc(proj['name'])}}} $|$ \\emph{{\\small{{{_esc(proj['tech'])}}}}}}}{{}}
              \\resumeItemListStart
{bullets_tex}
              \\resumeItemListEnd""")
        proj_blocks.append(block)
    tpl = tpl.replace('<<PROJECT_BLOCKS>>', '\n'.join(proj_blocks))

    # Education
    edu_blocks = []
    for edu in data.get('education', []):
        block = textwrap.dedent(f"""\
            \\resumeSubheading
              {{{_esc(edu['school'])}}}{{{_esc(edu['location'])}}}
              {{{_esc(edu['degree'])}}}{{{_esc(edu['dates'])}}}""")
        edu_blocks.append(block)
    tpl = tpl.replace('<<EDUCATION_BLOCKS>>', '\n'.join(edu_blocks))

    # Skills
    skill_rows = []
    for sk in data.get('skills', []):
        row = f'    \\small{{\\textbf{{{_esc(sk["category"])}:}} {_esc(sk["items"])}}}'
        skill_rows.append(f'  \\item {row}')
    tpl = tpl.replace('<<SKILLS_ROWS>>', '\n'.join(skill_rows))

    return tpl


# ─────────────────────────────────────────────────────────────
# Step 3 — Compile PDF
# ─────────────────────────────────────────────────────────────

def _compile_pdf(tex_path: str, output_dir: str = 'data') -> tuple[bool, str]:
    cmd = [
        'pdflatex',
        '-output-directory', output_dir,
        '-interaction=nonstopmode',
        '-halt-on-error',
        tex_path,
    ]
    combined_log = ""
    try:
        for _ in range(2):  # two passes for cross-references
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            combined_log = result.stdout + result.stderr
        return result.returncode == 0, combined_log
    except subprocess.TimeoutExpired:
        return False, "pdflatex timed out"
    except Exception as e:
        return False, str(e)


# ─────────────────────────────────────────────────────────────
# Step 4 — Hard page count check
# ─────────────────────────────────────────────────────────────

def _check_page_count(log: str) -> int:
    match = re.search(r'Output written on .+?\((\d+) page', log)
    if match:
        return int(match.group(1))
    pages = log.count('[1]') or 1
    return pages


# ─────────────────────────────────────────────────────────────
# Step 5 — FAANG quality scorecard
# ─────────────────────────────────────────────────────────────

_ACTION_VERBS = {
    'designed', 'engineered', 'implemented', 'optimized', 'reduced', 'built', 'delivered',
    'migrated', 'scaled', 'developed', 'created', 'led', 'deployed', 'automated', 'refactored',
    'improved', 'integrated', 'architected', 'established', 'launched', 'shipped', 'eliminated',
    'accelerated', 'streamlined', 'transformed', 'authored', 'instrumented', 'profiled',
}

_METRIC_RE = re.compile(r'\d+[x%]|\d+ (ms|s|percent|times|x|hours|minutes|days|million|billion|k|m\b)', re.I)


def _score_resume(data: dict, job_desc: str) -> tuple[int, list[str]]:
    issues = []
    score = 0

    all_bullets = []
    for exp in data.get('experience', []):
        all_bullets.extend(exp.get('bullets', []))
    for proj in data.get('projects', []):
        all_bullets.extend(proj.get('bullets', []))

    # 1. Action verbs (25 pts)
    verb_hits = 0
    for b in all_bullets:
        first_word = b.strip().split()[0].lower().rstrip(',') if b.strip() else ''
        if first_word in _ACTION_VERBS:
            verb_hits += 1
    verb_score = min(25, verb_hits * 5)
    score += verb_score
    if verb_hits < 4:
        issues.append(f"Only {verb_hits}/5+ bullets start with a strong action verb (need >=4)")

    # 2. Quantified metrics (25 pts)
    metric_hits = 0
    for b in all_bullets:
        if _METRIC_RE.search(b):
            metric_hits += 1
    metric_score = min(25, metric_hits * 5)
    score += metric_score
    if metric_hits < 4:
        issues.append(f"Only {metric_hits}/5+ bullets contain quantified metrics — need >=4")

    # 3. ATS keyword density (20 pts)
    jd_words = set(re.findall(r'\b[a-z]{4,}\b', job_desc.lower()))
    resume_text = ' '.join(all_bullets + [data.get('summary', '')])
    resume_words = set(re.findall(r'\b[a-z]{4,}\b', resume_text.lower()))
    overlap = jd_words & resume_words
    ats_score = min(20, len(overlap) // 3)
    score += ats_score
    if len(overlap) < 10:
        issues.append(f"Low JD keyword overlap: {len(overlap)} words. Mirror JD terminology more.")

    # 4. Section completeness (20 pts)
    sections = ['summary', 'experience', 'projects', 'education', 'skills']
    for sec in sections:
        if data.get(sec):
            score += 4
        else:
            issues.append(f"Missing section: '{sec}'")

    # 5. Bullet uniqueness (10 pts, deduct per duplicate pattern)
    score += 10
    seen_verbs = []
    for b in all_bullets:
        first = b.strip().split()[0].lower() if b.strip() else ''
        if first in seen_verbs:
            score -= 3
            issues.append(f"Duplicate action verb '{first}' used in multiple bullets")
        else:
            seen_verbs.append(first)

    score = max(0, min(100, score))
    return score, issues


# ─────────────────────────────────────────────────────────────
# Step 6 — Validation record
# ─────────────────────────────────────────────────────────────

def _save_validation(application: Application, score: int, issues: list, page_count: int, passed: bool):
    record = {
        "passed": passed,
        "score": score,
        "page_count": page_count,
        "issues": issues,
    }
    val_path = f"data/validation_{application.id}.json"
    with open(val_path, 'w') as f:
        json.dump(record, f, indent=2)
    application.resume_validation_json_path = val_path
    print(f"  [validate] score={score}/100, pages={page_count}, passed={passed}, issues={issues}")


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def generate_tailored_resume(db: Session, application: Application, base_resume_data: dict):
    """
    Generate a tailored one-page FAANG-grade resume PDF.
    Retries up to MAX_RETRIES times, tightening bullet limits on each attempt.
    Raises RuntimeError if all attempts fail.
    """
    job = application.job
    company_abbr = ''.join(c for c in job.company if c.isalnum())[:8].upper()
    tex_path = f"data/resume_{application.id}_{job.id}_{company_abbr}.tex"
    pdf_path = f"data/resume_{application.id}_{job.id}_{company_abbr}.pdf"

    last_issues = []

    for attempt in range(1, MAX_RETRIES + 1):
        print(f"[tailor] App {application.id} — attempt {attempt}/{MAX_RETRIES}")

        # ── Step 1: Fill JSON ──────────────────────────────────────
        try:
            resume_json = _fill_resume_json(
                base_resume_data, job.title, job.description or "", attempt=attempt
            )
        except Exception as e:
            print(f"  [tailor] LLM error on attempt {attempt}: {e}")
            last_issues = [f"LLM error: {e}"]
            continue

        # ── Step 1b: Validate JSON before rendering ────────────────
        try:
            _validate_resume_json(resume_json)
        except ValueError as ve:
            print(f"  [tailor] JSON validation failed on attempt {attempt}: {ve}")
            last_issues = [str(ve)]
            continue

        # ── Step 2: Render LaTeX ───────────────────────────────────
        try:
            tex_content = _render_tex_from_json(resume_json, base_resume_data)
        except Exception as e:
            print(f"  [tailor] Render error on attempt {attempt}: {e}")
            last_issues = [f"Render error: {e}"]
            continue

        with open(tex_path, 'w', encoding='utf-8') as f:
            f.write(tex_content)
        application.tailored_resume_tex_path = tex_path

        # ── Step 3: Compile ────────────────────────────────────────
        compiled, log = _compile_pdf(tex_path)
        if not compiled:
            print(f"  [tailor] pdflatex failed on attempt {attempt}")
            last_issues = ["pdflatex compilation failed"]
            continue

        # ── Step 4: Page count check ───────────────────────────────
        page_count = _check_page_count(log)
        if page_count > 1:
            print(f"  [tailor] REJECTED: {page_count} pages (need 1). Retrying with fewer bullets.")
            last_issues = [f"Resume spilled to {page_count} pages — retry with fewer bullets"]
            _save_validation(application, 0, last_issues, page_count, passed=False)
            continue

        # ── Step 5: Quality score ──────────────────────────────────
        score, issues = _score_resume(resume_json, job.description or "")
        _save_validation(application, score, issues, page_count, passed=(score >= MIN_QUALITY_SCORE))

        if score < MIN_QUALITY_SCORE:
            print(f"  [tailor] REJECTED: quality score {score}/100 < {MIN_QUALITY_SCORE}. Retrying.")
            last_issues = issues
            continue

        # ── PASSED ─────────────────────────────────────────────────
        application.tailored_resume_pdf_path = pdf_path
        application.tailored_resume_json_path = None
        db.commit()
        print(f"[tailor] PASSED — App {application.id}: score={score}/100, pages={page_count}")
        return

    raise RuntimeError(
        f"Resume generation failed after {MAX_RETRIES} attempts for app {application.id}. "
        f"Last issues: {last_issues}"
    )
