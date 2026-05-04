from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class ApplicationBase(BaseModel):
    job_id: int
    fit_score: Optional[float] = None
    fit_analysis: Optional[str] = None
    matching_skills: Optional[str] = None  # JSON string: '["Java", "Spring Boot"]'
    missing_skills: Optional[str] = None   # JSON string: '["Kotlin", "Kafka"]'

    tailored_resume_json_path: Optional[str] = None
    tailored_resume_tex_path: Optional[str] = None
    tailored_resume_pdf_path: Optional[str] = None
    resume_validation_json_path: Optional[str] = None
    submission_log_json_path: Optional[str] = None
    screenshot_path: Optional[str] = None

    status: str = "NEW"

class ApplicationCreate(ApplicationBase):
    pass

class ApplicationUpdate(BaseModel):
    status: Optional[str] = None
    fit_score: Optional[float] = None
    fit_analysis: Optional[str] = None
    matching_skills: Optional[str] = None
    missing_skills: Optional[str] = None
    tailored_resume_json_path: Optional[str] = None
    tailored_resume_tex_path: Optional[str] = None
    tailored_resume_pdf_path: Optional[str] = None
    resume_validation_json_path: Optional[str] = None
    submission_log_json_path: Optional[str] = None
    screenshot_path: Optional[str] = None

class ApplicationResponse(ApplicationBase):
    id: int
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True
