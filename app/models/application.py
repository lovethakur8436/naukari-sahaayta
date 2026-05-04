from sqlalchemy import Column, Integer, String, Text, DateTime, JSON, Float, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base

class Application(Base):
    __tablename__ = "applications"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("job_postings.id"))

    fit_score = Column(Float, nullable=True)
    fit_analysis = Column(Text, nullable=True)
    matching_skills = Column(Text, nullable=True)  # JSON string: ["Java", "Spring Boot"]
    missing_skills = Column(Text, nullable=True)   # JSON string: ["Kotlin", "Kafka"]

    # Paths to artifacts
    tailored_resume_json_path = Column(String, nullable=True)
    tailored_resume_tex_path = Column(String, nullable=True)
    tailored_resume_pdf_path = Column(String, nullable=True)
    resume_validation_json_path = Column(String, nullable=True)
    submission_log_json_path = Column(String, nullable=True)
    screenshot_path = Column(String, nullable=True)

    status = Column(String, default="NEW")  # NEW, SKIPPED, REVIEW_AND_PREFILL, AUTO_APPLY_PENDING, AUTO_APPLIED, FAILED

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    job = relationship("JobPosting")
