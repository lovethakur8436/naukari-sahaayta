import os
import json
import google.generativeai as genai
from app.models.job import JobPosting
from sqlalchemy.orm import Session
from dotenv import load_dotenv

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY", os.getenv("OPENAI_API_KEY")))

def normalize_job_with_llm(db: Session, job: JobPosting):
    """
    Extracts min_experience, max_experience, and skills from job description using LLM.
    """
    if not job.description:
        return
        
    prompt = f"""
    Extract the following information from the job description:
    - Minimum years of experience required (float, null if not specified)
    - Maximum years of experience required (float, null if not specified)
    - List of technical skills (array of strings)

    Job Description:
    {job.description[:3000]} # Limit to save tokens

    Return ONLY a JSON object with keys: min_experience, max_experience, skills.
    """

    try:
        model = genai.GenerativeModel('gemini-2.5-flash', generation_config={"response_mime_type": "application/json"})
        response = model.generate_content(
            f"You are a helpful assistant that extracts structured data from job descriptions. Return only valid JSON.\n\n{prompt}"
        )
        
        result_str = response.text
        result = json.loads(result_str)
        
        job.min_experience = result.get("min_experience")
        job.max_experience = result.get("max_experience")
        job.skills = result.get("skills", [])
        
        db.commit()
    except Exception as e:
        print(f"Error normalizing job {job.id}: {e}")
