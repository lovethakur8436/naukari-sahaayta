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

    prompt = f"""
You are an expert resume writer following the strict guidelines from techinterviewhandbook.org/resume/.
Tailor the base resume data to BEST fit the job description below.

CRITICAL GUIDELINES:
1. ONE PAGE STRICTLY & WELL-BALANCED: The resume MUST fill exactly one page. Provide enough bullets and detail to avoid whitespace, but do not exceed one page.
2. ENHANCE AND EXTRAPOLATE (Realistic Enhancements): Realistically enhance the candidate's experience by adding skills, projects, or stories required by the JD. You can strengthen the story as long as it stays justifiable in an interview.
3. HEADLINE/SUMMARY: Include a strong, targeted headline/summary at the top indicating the target role.
4. STRONG BACKEND SIGNALS: Bullets MUST mention: testing, design patterns, API performance optimization, complex DB queries, schema design, security, and production debugging.
5. ELEVATE PROJECTS: Rewrite projects as strong, production-grade backend case studies. Remove 'In Progress' or 'Hackathon' labels.
6. FOCUSED SKILLS: Group and filter skills to establish a strong backend identity.
7. Use STAR method for all bullets. Start with a strong action verb and quantify achievements.
8. Output VALID LaTeX code using the provided template structure.
9. Escape special LaTeX characters (%, &, $, #, _).
10. DO NOT use any markdown code blocks. Output raw LaTeX only.

Job Title: {job.title}
Job Description: {job.description[:2000]}

Base Resume JSON:
{json.dumps(base_resume_data)}

LaTeX Template:
{latex_template}
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
    validation_prompt = f"""
Review the following tailored resume LaTeX and validate it against these TechInterviewHandbook guidelines:
1. Strictly one page, well-balanced (~400-500 words), no excessive whitespace.
2. Strong backend signals (testing, DB schema, performance, APIs).
3. Uses STAR method and action verbs, quantified achievements.
4. Projects sound like production-grade backend case studies.
5. Content is realistically enhanced for a mid-level backend engineer.

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
