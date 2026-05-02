from pydantic import BaseModel, HttpUrl
from typing import Optional, List, Dict, Any
from datetime import datetime

class JobPostingBase(BaseModel):
    portal: str
    portal_job_id: str
    title: str
    company: str
    location: Optional[str] = None
    description: str
    url: str
    raw_data: Optional[Dict[str, Any]] = None
    
    min_experience: Optional[float] = None
    max_experience: Optional[float] = None
    skills: Optional[List[str]] = None

class JobPostingCreate(JobPostingBase):
    pass

class JobPostingResponse(JobPostingBase):
    id: int
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True
