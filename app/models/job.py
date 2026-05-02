from sqlalchemy import Column, Integer, String, Text, DateTime, JSON, Float
from sqlalchemy.sql import func
from app.database import Base

class JobPosting(Base):
    __tablename__ = "job_postings"

    id = Column(Integer, primary_key=True, index=True)
    portal = Column(String, index=True) # e.g., Instahyre, Wellfound, Greenhouse
    portal_job_id = Column(String, index=True) # ID from the portal
    title = Column(String)
    company = Column(String, index=True)
    location = Column(String)
    description = Column(Text)
    url = Column(String, unique=True)
    raw_data = Column(JSON) # Store raw JSON from API
    
    # Normalized fields
    min_experience = Column(Float, nullable=True)
    max_experience = Column(Float, nullable=True)
    skills = Column(JSON, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
