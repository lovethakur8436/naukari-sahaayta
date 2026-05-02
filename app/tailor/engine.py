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
    You are an expert resume writer. Tailor the base resume data to best fit the job description below.
    Guidelines:
    - Truthfully represent the candidate (do not hallucinate).
    - Rewrite the summary to be concise and targeted to this role.
    - Reorder and rewrite bullet points to be impact-first, using quantified achievements where available.
    - Highlight the most relevant skills.
    - You must output VALID LaTeX code using the provided template structure.
    - Escape special LaTeX characters (like %, &, $, #, _).
    - DO NOT use any markdown code blocks (e.g. ```latex). Just output the raw LaTeX code.
    
    Job Title: {job.title}
    Job Description: {job.description[:2000]}
    
    Base Resume JSON:
    {json.dumps(base_resume_data)}
    
    LaTeX Template Format to follow (replace the {{...}} and {{%...%}} blocks with the actual tailored LaTeX code):
    {latex_template}
    """
    
    try:
        model = genai.GenerativeModel('gemini-2.5-flash')
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
    Review the following tailored resume LaTeX and validate it against these guidelines:
    1. Concise summary.
    2. Impact-first bullets.
    3. Quantified achievements when available.
    4. Strongest bullets first.
    5. No hallucinated content (assume base content is correct).
    
    Resume LaTeX: {tailored_tex}
    
    Return a JSON object with:
    - passed (boolean)
    - feedback (array of strings, listing improvements or violations)
    """
    try:
        model = genai.GenerativeModel('gemini-2.5-flash', generation_config={"response_mime_type": "application/json"})
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
