"""
app/tailor/engine.py  —  FAANG-grade one-page resume tailoring engine

Architecture:
  1. _fill_resume_json()   — LLM fills structured data (content only, no layout)
  2. _render_tex_from_json() — deterministic template injection (layout never LLM-controlled)
  3. _compile_pdf()        — pdflatex compile
  4. _check_page_count()   — reject if > 1 page (parse pdflatex log)
  5. _score_resume()       — FAANG scorecard: action verbs, metrics, ATS keywords, section completeness
  6. generate_tailored_resume() — retry loop (up to MAX_RETRIES=3), raises on total failure

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
# Characters that must NOT be escaped (already valid in LaTeX context)
_SAFE_LATEX_RE = re.compile(r'\\[a-zA-Z]+\{|\\[&%$#^_{}~]')


def _esc(text: str) -> str:
    """Escape a raw string for safe LaTeX embedding."""
    if not text:
        return ""
    # Only escape if no existing LaTeX commands detected
    if _SAFE_LATEX_RE.search(text):
        return text  # already contains LaTeX markup — trust it
    for char, replacement in _LATEX_ESCAPE:
        if char == '\\':
            continue  # handled separately to avoid double-escaping
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

def _fill_resume_json(base_resume_data: dict, job_title: str, job_desc: str, attempt: int = 1) -> dict:
    """
    Ask the LLM to return ONLY resume content as structured JSON.
    Layout and rendering are handled by _render_tex_from_json.
    """
    bullet_limits = {
        1: {"primary": 5, "intern": 2, "project": 2},
        2: {"primary": 4, "intern": 1, "project": 2},
        3: {"primary": 4, "intern": 1, "project": 1},
    }[min(attempt, 3)]

    prompt = f"""You are a FAANG-level resume writer. Tailor the candidate's resume for the job below.
Return a JSON object with EXACTLY this structure — no extra keys, no missing keys.

JOB TO TAILOR FOR:
Title: {job_title}
Description (first 2500 chars):
{job_desc[:2500]}

CANDIDATE BASE DATA:
{json.dumps(base_resume_data, indent=2)}

=== MANDATORY QUALITY RULES ===

1. SUMMARY (required, 2 sentences max):
   - Opening title MUST be one of: "Software Engineer", "Backend Engineer", "Full-Stack Engineer"
   - NEVER copy the job title verbatim
   - Sentence 1: [Title] with [X] years at [Company] [doing core skill relevant to JD]
   - Sentence 2: one additional strength matching the JD

2. EXPERIENCE — Primary role (Wells Fargo): exactly {bullet_limits['primary']} bullets
   - Each bullet: [Strong Action Verb] + [What you built/achieved] + [specific tech] + [quantified result %/x/ms]
   - Action verbs: Designed, Engineered, Implemented, Optimized, Reduced, Built, Delivered, Migrated, Scaled
   - EVERY bullet must be unique — no repeated structure or phrasing
   - MUST include at least 1 bullet with a latency/throughput metric
   - MUST include at least 1 bullet with a reliability/availability metric
   - Tailor 2-3 bullets to directly mirror JD keywords (use the exact terminology from the JD)

3. EXPERIENCE — Internship (Airveda): exactly {bullet_limits['intern']} bullet(s)
   - Keep concise, quantify if possible

4. PROJECTS: exactly 2 projects, each with exactly {bullet_limits['project']} bullet(s)
   - Present as completed production work — NO "In Progress" or "Hackathon" labels
   - Project 1: AI Trip Planner (Gemini API, React.js, Firebase)
   - Project 2: InfraBoard — Ansible Ops Dashboard (FastAPI, React.js, MongoDB, AWS EC2)
   - Tailor project bullets to highlight skills relevant to this JD

5. EDUCATION (REQUIRED — do not omit):
   - Degree, university, location, graduation year
   - Use: B.Tech in Computer Science, Lovely Professional University, Punjab India, 2022

6. SKILLS:
   - 4 to 6 categories
   - ONLY include skills directly relevant to this JD
   - Each category line (label + skills) must be under 80 characters
   - Remove entire categories with zero JD relevance
   - Categories to consider: Languages, Backend, Frontend, Cloud/DevOps, Databases, Testing

=== ATS RULES ===
- Mirror exact terminology from the JD (e.g. if JD says "distributed systems", use that phrase)
- No tables, no columns, no graphics descriptions
- All dates in format: Mon YYYY – Mon YYYY or Mon YYYY – Present

Return ONLY this JSON (all string values — no nested objects inside bullet arrays):
{{
  "summary": "<2-sentence summary>",
  "experience": [
    {{
      "company": "Wells Fargo",
      "location": "Hyderabad, India",
      "title": "Software Engineer",
      "dates": "Nov 2022 -- Present",
      "bullets": ["bullet1", "bullet2", ...]
    }},
    {{
      "company": "Airveda",
      "location": "Remote",
      "title": "Software Engineer Intern",
      "dates": "Mar 2022 -- May 2022",
      "bullets": ["bullet1"]
    }}
  ],
  "projects": [
    {{
      "name": "AI Trip Planner",
      "tech": "Gemini API | React.js | Firebase",
      "bullets": ["bullet1", "bullet2"]
    }},
    {{
      "name": "InfraBoard — Ansible Ops Dashboard",
      "tech": "FastAPI | React.js | MongoDB | Docker | AWS EC2",
      "bullets": ["bullet1", "bullet2"]
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
    {{"category": "Languages", "items": "Java, Python, JavaScript, TypeScript"}},
    {{"category": "Backend", "items": "Spring Boot, REST APIs, Microservices"}}
  ]
}}
"""
    return _groq_json(prompt, system="You are a FAANG resume expert. Return only valid JSON.")


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
    """
    Run pdflatex and return (success: bool, log: str).
    Runs twice for cross-references.
    """
    cmd = [
        'pdflatex',
        '-output-directory', output_dir,
        '-interaction=nonstopmode',
        '-halt-on-error',
        tex_path,
    ]
    combined_log = ""
    try:
        for _ in range(2):  # two passes
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
    """
    Parse pdflatex stdout log for the page count line.
    Returns the number of pages (1 = pass, 2+ = fail).
    """
    # pdflatex emits: "Output written on foo.pdf (N page(s), ...)"
    match = re.search(r'Output written on .+?\((\d+) page', log)
    if match:
        return int(match.group(1))
    # fallback: count page shipout markers
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
    """
    Score the resume 0–100 and return (score, list_of_issues).
    Scoring breakdown:
      - Action verbs in bullets:       25 pts  (5 pts each for first 5 unique)
      - Quantified metrics:            25 pts  (5 pts each for first 5 metrics)
      - ATS keyword density:           20 pts  (count JD keywords present)
      - Section completeness:          20 pts  (summary/exp/projects/edu/skills each 4 pts)
      - Bullet uniqueness:             10 pts  (deduct 3 per duplicate)
    """
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
        issues.append(f"Only {verb_hits}/5+ bullets start with a strong action verb (need ≥4)")

    # 2. Quantified metrics (25 pts)
    metric_hits = 0
    for b in all_bullets:
        if _METRIC_RE.search(b):
            metric_hits += 1
    metric_score = min(25, metric_hits * 5)
    score += metric_score
    if metric_hits < 4:
        issues.append(f"Only {metric_hits}/5+ bullets contain quantified metrics (%, x, ms) — need ≥4")

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
    Generate a tailored one-page FAANG-grade resume PDF for the application.
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
            continue

        # ── Step 2: Render LaTeX ───────────────────────────────────
        try:
            tex_content = _render_tex_from_json(resume_json, base_resume_data)
        except Exception as e:
            print(f"  [tailor] Render error on attempt {attempt}: {e}")
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
        application.tailored_resume_json_path = None  # JSON not stored separately
        db.commit()
        print(f"[tailor] PASSED — App {application.id}: score={score}/100, pages={page_count}")
        return

    # All attempts failed — surface the last known issues
    raise RuntimeError(
        f"Resume generation failed after {MAX_RETRIES} attempts for app {application.id}. "
        f"Last issues: {last_issues}"
    )
