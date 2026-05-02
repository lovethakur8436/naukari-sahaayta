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
    """
    prompt = f"""
    Compare the following job description with the base resume.
    Provide a fit score between 0 and 100, and a brief analysis explaining the score.
    
    Job Title: {job.title}
    Job Description: {job.description[:3000]}
    
    Base Resume:
    {base_resume[:3000]}
    
    Return ONLY a JSON object with keys: fit_score (integer), fit_analysis (string).
    """

    try:
        model = genai.GenerativeModel('gemini-2.5-flash', generation_config={"response_mime_type": "application/json"})
        response = model.generate_content(
            f"You are an expert technical recruiter analyzing resume fit. Return only valid JSON.\n\n{prompt}"
        )
        
        result_str = response.text
        result = json.loads(result_str)
        
        # Create application entry
        app_entry = Application(
            job_id=job.id,
            fit_score=result.get("fit_score", 0),
            fit_analysis=result.get("fit_analysis", ""),
            status="NEW"
        )
        db.add(app_entry)
        db.commit()
        db.refresh(app_entry)
        
        return app_entry
    except Exception as e:
        print(f"Error matching job {job.id}: {e}")
        return None
