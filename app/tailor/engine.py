import os
import json
import subprocess
from groq import Groq
from sqlalchemy.orm import Session
from app.models.application import Application
from app.models.job import JobPosting
from dotenv import load_dotenv

load_dotenv()

# Single Groq client reused for all calls
_groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))


def _groq_text(prompt: str, system: str = "") -> str:
    """Call Groq and return the response text."""
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
    """Call Groq with json_object mode and return parsed dict."""
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


def generate_tailored_resume(db: Session, application: Application, base_resume_data: dict):
    """
    Generate a tailored resume LaTeX using Groq (llama-3.3-70b-versatile),
    then compile to PDF with pdflatex.
    """
    job = application.job

    with open('app/tailor/template.tex', 'r') as f:
        latex_template = f.read()

    prompt = f"""You are an expert ATS-focused resume writer. Your task is to tailor the candidate's resume
for the specific job below. The output must be a SINGLE-PAGE LaTeX resume — this is non-negotiable.

=== STRICT ONE-PAGE RULES (LaTeX layout) ===
- Add \\usepackage[top=0.4in, bottom=0.4in, left=0.5in, right=0.5in]{{geometry}} in the preamble
- Use \\setlength{{\\itemsep}}{{0pt}} and \\setlength{{\\parskip}}{{0pt}} on every itemize/enumerate
- Maximum 5 bullets for the primary job (Wells Fargo)
- Maximum 1 bullet for the internship role
- Maximum 2 bullets per project
- Maximum 4 skill category rows, each row under 80 characters total
- Summary: 2 sentences max
- If content still risks overflowing, shorten bullet text — NEVER let content go to page 2

=== SUMMARY RULES ===
- The summary opening title MUST be "Software Engineer" or "Backend Engineer"
- NEVER use the job's title verbatim (e.g. do NOT write "Customer Success Engineer" or "Solutions Architect")
- Good example: "Backend Engineer with 3.5+ years at Wells Fargo..."
- Bad example: "Customer Success Engineer with 3.5+ years..."

=== BULLET QUALITY RULES ===
- Every bullet must be UNIQUE — never repeat the same sentence structure or phrasing across bullets
- Every bullet must follow STAR format: Strong action verb → What you built/did → Technology used → Quantified result
- Do NOT use generic filler phrases like "Utilized design patterns such as Singleton and Factory" unless the JD specifically requires it
- Tailor bullets to naturally match the JD's keywords — do not force irrelevant technologies
- Remove "In Progress" and "Hackathon" labels from projects — present all projects as completed production work

=== SKILLS SECTION RULES ===
- Include ONLY skills directly relevant to this specific JD
- Maximum 4 categories
- Each category line must fit within 80 characters (including the category label)
- Remove entire categories that have zero relevance to the JD

=== JOB TO TAILOR FOR ===
Job Title: {job.title}
Job Description:
{job.description[:2500]}

=== BASE RESUME DATA ===
{json.dumps(base_resume_data)}

=== LATEX TEMPLATE ===
{latex_template}

Output ONLY raw valid LaTeX. No markdown, no code fences, no explanation.
"""

    try:
        rendered_tex = _groq_text(
            prompt,
            system="You are an expert ATS-friendly resume tailor. Output strictly valid LaTeX code, no markdown, no explanation."
        )
        rendered_tex = (
            rendered_tex
            .replace("```latex", "")
            .replace("```tex", "")
            .replace("```", "")
            .strip()
        )

        company_abbr = ''.join(c for c in job.company if c.isalnum())[:8].upper()
        application.tailored_resume_json_path = None

        tex_path = f"data/resume_{application.id}_{job.id}_{company_abbr}.tex"
        with open(tex_path, 'w', encoding='utf-8') as f:
            f.write(rendered_tex)
        application.tailored_resume_tex_path = tex_path

        # Validate
        validate_resume(application, rendered_tex)

        # Compile PDF
        try:
            subprocess.run(
                ['pdflatex', '-output-directory=data', '-interaction=nonstopmode', tex_path],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            application.tailored_resume_pdf_path = (
                f"data/resume_{application.id}_{job.id}_{company_abbr}.pdf"
            )
            print(f"PDF compiled: {application.tailored_resume_pdf_path}")
        except subprocess.CalledProcessError as e:
            print(f"PDF compile failed for app {application.id}: {e}")

        db.commit()
        return None

    except Exception as e:
        print(f"Error tailoring resume for application {application.id}: {e}")
        return None


def validate_resume(application: Application, tailored_tex: str):
    """
    Validate the tailored resume against ATS-safe and FAANG guidelines using Groq.
    """
    validation_prompt = f"""Review the following tailored resume LaTeX and validate it against these guidelines:
1. Strictly one page — no content overflow to page 2.
2. Summary uses "Software Engineer" or "Backend Engineer" as the title, NOT the job title verbatim.
3. Wells Fargo has max 5 bullets, internship has max 1 bullet, each project has max 2 bullets.
4. Every bullet is unique — no repeated sentence patterns.
5. Strong backend signals: testing, DB schema, API performance, security, production debugging.
6. Uses STAR method and strong action verbs with quantified achievements.
7. Skills section has max 4 categories, each under 80 characters.
8. Projects presented as completed production work (no "In Progress" or "Hackathon" labels).

Resume LaTeX:
{tailored_tex[:6000]}

Return a JSON object with:
- passed (boolean)
- feedback (array of strings listing improvements or violations)
"""
    try:
        result = _groq_json(
            validation_prompt,
            system="You are a FAANG recruiter validating a resume. Return valid JSON only."
        )
        val_path = f"data/validation_{application.id}.json"
        with open(val_path, 'w') as f:
            json.dump(result, f, indent=2)
        application.resume_validation_json_path = val_path
    except Exception as e:
        print(f"Validation error for app {application.id}: {e}")
