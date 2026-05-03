import os
import json
import subprocess
import google.generativeai as genai
from sqlalchemy.orm import Session
from app.models.application import Application
from app.models.job import JobPosting
from dotenv import load_dotenv

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY", os.getenv("OPENAI_API_KEY")))

def generate_tailored_resume(db: Session, application: Application, base_resume_data: dict):
    """
    Generate tailored resume LaTeX using LLM, then compile PDF.
    """
    job = application.job
    
    with open('app/tailor/template.tex', 'r') as f:
        latex_template = f.read()
    
    prompt = f"""
    You are an expert resume writer following the strict guidelines from techinterviewhandbook.org/resume/.
    Tailor the base resume data to BEST fit the job description below.
    
    CRITICAL GUIDELINES:
    1. ONE PAGE STRICTLY & WELL-BALANCED: The resume MUST fill exactly one page perfectly. Provide enough bullet points and detail to avoid empty white space, but do not exceed one page.
    2. ENHANCE AND EXTRAPOLATE (Realistic Enhancements): Enhance the candidate's experience by realistically adding skills, projects, or stories required by the job description. You can "cook" the story slightly to show strong backend experience that fits the JD, as long as it remains realistic and justifiable in an interview based on the base skills.
    3. HEADLINE/SUMMARY: Include a strong, targeted headline/summary at the top (e.g., "Backend Engineer | Java | Spring Boot | Microservices") indicating the target role immediately.
    4. STRONG BACKEND SIGNALS: Your generated experience bullets MUST mention key backend evaluation signals: testing, design patterns, API performance optimization, complex database queries, schema design, security, and production debugging.
    5. ELEVATE PROJECTS: Rewrite the projects so they sound like strong, production-grade backend case studies. Remove "In Progress" or "Hackathon" labels. Give them concrete architecture and backend achievements.
    6. FOCUSED SKILLS: Group and filter the skills section to establish a strong, focused Backend identity. Do not dilute it with too many unrelated frontend or random skills.
    7. Use the STAR method (Situation, Task, Action, Result) for all bullets. Start with a strong Action Verb and quantify achievements.
    8. You must output VALID LaTeX code using the provided template structure.
    9. Escape special LaTeX characters (like %, &, $, #, _).
    10. DO NOT use any markdown code blocks (e.g. ```latex). Just output the raw LaTeX code.
    
    Job Title: {job.title}
    Job Description: {job.description[:2000]}
    
    Base Resume JSON:
    {json.dumps(base_resume_data)}
    
    LaTeX Template Format to follow:
    {latex_template}
    """
    
    try:
        model = genai.GenerativeModel('gemini-2.5-pro')
        response = model.generate_content(
            f"You are an expert ATS-friendly resume tailor. Output strictly valid LaTeX code.\n\n{prompt}"
        )
        
        rendered_tex = response.text.replace("```latex", "").replace("```tex", "").replace("```", "").strip()
        
        company_abbr = ''.join(c for c in job.company if c.isalnum())[:8].upper()
        
        application.tailored_resume_json_path = None
        
        tex_path = f"data/resume_{application.id}_{job.id}_{company_abbr}.tex"
        with open(tex_path, 'w', encoding='utf-8') as f:
            f.write(rendered_tex)
        
        application.tailored_resume_tex_path = tex_path
        
        # Validation Step
        validate_resume(application, rendered_tex)
        
        # Compile PDF (Requires pdflatex installed on the system)
        try:
            # Note: For MiKTeX, adding -max-print-line=10000 or similar isn't strictly necessary, 
            # but sometimes MiKTeX prompts for missing packages. 
            # If the user hasn't configured MiKTeX to install packages on the fly without asking, it will hang.
            subprocess.run(
                ['pdflatex', '-output-directory=data', '-interaction=nonstopmode', tex_path],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            application.tailored_resume_pdf_path = f"data/resume_{application.id}_{job.id}_{company_abbr}.pdf"
        except subprocess.CalledProcessError as e:
            print(f"Failed to compile PDF for {application.id}: {e}")
            
        db.commit()
        return None
        
    except Exception as e:
        print(f"Error tailoring resume for application {application.id}: {e}")
        return None

def validate_resume(application: Application, tailored_tex: str):
    """
    Validate the tailored resume against ATS-safe and FAANG guidelines.
    """
    validation_prompt = f"""
    Review the following tailored resume LaTeX and validate it against these TechInterviewHandbook guidelines:
    1. Strictly one page, well-balanced (approx 400-500 words) without excessive whitespace.
    2. Strong backend signals (testing, db schema, performance, APIs).
    3. Uses STAR method and Action Verbs, quantified achievements.
    4. Projects sound like production-grade backend case studies.
    5. Content is realistically enhanced for a mid-level backend engineer, justified based on core base skills.
    
    Resume LaTeX: {tailored_tex}
    
    Return a JSON object with:
    - passed (boolean)
    - feedback (array of strings, listing improvements or violations)
    """
    try:
        model = genai.GenerativeModel('gemini-2.5-pro', generation_config={"response_mime_type": "application/json"})
        response = model.generate_content(
            f"You are a FAANG recruiter validating a resume. Return valid JSON only.\n\n{validation_prompt}"
        )
        validation_result = json.loads(response.text)
        
        val_path = f"data/validation_{application.id}.json"
        with open(val_path, 'w') as f:
            json.dump(validation_result, f, indent=2)
            
        application.resume_validation_json_path = val_path
    except Exception as e:
        print(f"Validation error: {e}")
