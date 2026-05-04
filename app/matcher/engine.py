import os
import json
import google.generativeai as genai
from sqlalchemy.orm import Session
from app.models.job import JobPosting
from app.models.application import Application
from dotenv import load_dotenv

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY", os.getenv("OPENAI_API_KEY")))

def match_job(db: Session, job: JobPosting, base_resume: str) -> Application:
    """
    Compare job with base resume to calculate fit score and analysis.
    Uses a structured rubric for consistent, meaningful scoring.
    """
    prompt = f"""
You are a senior technical recruiter screening candidates for software engineering roles.

Score this candidate's resume against the job description on a scale of 0-100 using these criteria:

1. Tech Stack Match (40 pts): Do the candidate's primary languages/frameworks match what the JD requires?
   - Exact match (e.g. Java JD + Java resume) -> full points
   - Related but different (e.g. Java JD + Python resume) -> partial points
   - Unrelated stack -> 0 points

2. Seniority Match (20 pts): Does the candidate's experience level match the role?
   - JD says SDE2/Mid-level and candidate has 3-5 years -> full points
   - Over/under-qualified by more than 2 years -> deduct points

3. Domain Match (20 pts): Does the candidate's industry/domain experience match?
   - E.g. Fintech JD + Fintech background -> full points
   - Adjacent domain -> partial points

4. Key Requirements Coverage (20 pts): How many of the JD's required skills are explicitly present in the resume?
   - Count required skills mentioned in the JD vs found in the resume

Job Title: {job.title}
Job Description:
{job.description[:4000]}

Candidate Resume:
{base_resume[:4000]}

Return ONLY valid JSON with exactly these keys:
{{
  "fit_score": <integer 0-100>,
  "fit_analysis": "<2-3 sentences referencing specific matches and gaps>",
  "matching_skills": ["<skill1>", "<skill2>"],
  "missing_skills": ["<skill1>", "<skill2>"]
}}
"""

    try:
        model = genai.GenerativeModel('gemini-2.5-flash', generation_config={"response_mime_type": "application/json"})
        response = model.generate_content(
            f"You are an expert technical recruiter analyzing resume fit. Return only valid JSON.\n\n{prompt}"
        )

        result_str = response.text
        result = json.loads(result_str)

        app_entry = Application(
            job_id=job.id,
            fit_score=result.get("fit_score", 0),
            fit_analysis=result.get("fit_analysis", ""),
            matching_skills=json.dumps(result.get("matching_skills", [])),
            missing_skills=json.dumps(result.get("missing_skills", [])),
            status="NEW"
        )
        db.add(app_entry)
        db.commit()
        db.refresh(app_entry)

        return app_entry
    except Exception as e:
        print(f"Error matching job {job.id}: {e}")
        return None
