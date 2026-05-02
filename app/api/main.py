from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from typing import List
from pydantic import BaseModel

from app.database import engine, Base, get_db
from app.models.job import JobPosting
from app.models.application import Application
from app.schemas.job import JobPostingResponse
from app.schemas.application import ApplicationResponse
from app.ingestors.manager import ingest_jobs
from app.ingestors.greenhouse import GreenhouseIngestor
from app.ingestors.lever import LeverIngestor
from app.matcher.engine import match_job
from app.tailor.engine import generate_tailored_resume
from app.apply_agent.manager import process_application

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Job Application Automation API")

class MatchRequest(BaseModel):
    base_resume: str

@app.get("/jobs", response_model=List[JobPostingResponse])
def read_jobs(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    jobs = db.query(JobPosting).offset(skip).limit(limit).all()
    return jobs

@app.post("/jobs/ingest")
def trigger_ingest(db: Session = Depends(get_db)):
    # Example hardcoded ingestors for demonstration
    ingestors = [
        GreenhouseIngestor("gitlab"),
        LeverIngestor("netflix")
    ]
    added = ingest_jobs(db, ingestors)
    return {"message": f"Successfully ingested {added} jobs"}

@app.post("/applications/match")
def trigger_match(req: MatchRequest, db: Session = Depends(get_db)):
    """Match unmatched jobs against the base resume"""
    # Limit to 5 at a time so it doesn't hang the server or hit rate limits immediately
    jobs = db.query(JobPosting).outerjoin(Application).filter(Application.id == None).limit(5).all()
    matched_count = 0
    for job in jobs:
        print(f"Matching job {job.id}: {job.title} at {job.company}...")
        match_job(db, job, req.base_resume)
        matched_count += 1
    return {"message": f"Matched {matched_count} jobs (Processed 5 at a time for demonstration)"}

@app.get("/applications", response_model=List[ApplicationResponse])
def read_applications(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    apps = db.query(Application).offset(skip).limit(limit).all()
    return apps

@app.post("/applications/{app_id}/tailor")
def trigger_tailor(app_id: int, base_resume_data: dict, db: Session = Depends(get_db)):
    app_entry = db.query(Application).filter(Application.id == app_id).first()
    if not app_entry:
        raise HTTPException(status_code=404, detail="Application not found")
        
    generate_tailored_resume(db, app_entry, base_resume_data)
    return {"message": "Tailored resume generated"}

@app.post("/applications/{app_id}/apply")
def trigger_apply(app_id: int, candidate_profile: dict, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    app_entry = db.query(Application).filter(Application.id == app_id).first()
    if not app_entry:
        raise HTTPException(status_code=404, detail="Application not found")
        
    background_tasks.add_task(process_application, db, app_entry, candidate_profile)
    return {"message": "Application apply task started in background"}
