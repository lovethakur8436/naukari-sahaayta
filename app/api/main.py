from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel

from app.database import engine, Base, get_db
from app.models.job import JobPosting
from app.models.application import Application
from app.schemas.job import JobPostingResponse
from app.schemas.application import ApplicationResponse
from app.ingestors.manager import ingest_jobs, build_ingestors
from app.matcher.engine import match_job
from app.tailor.engine import generate_tailored_resume
from app.apply_agent.manager import process_application

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Naukari Sahaayta — Job Application Automation API")


# ------------------------------------------------------------------ #
# Request bodies                                                       #
# ------------------------------------------------------------------ #

class MatchRequest(BaseModel):
    base_resume: str

class IngestRequest(BaseModel):
    """
    Pass lists of board tokens to ingest from multiple companies.
    Example body:
    {
        "greenhouse": ["gitlab", "stripe", "figma"],
        "lever": ["linear", "vercel"]
    }
    """
    greenhouse: Optional[List[str]] = []
    lever: Optional[List[str]] = []


# ------------------------------------------------------------------ #
# Jobs                                                                 #
# ------------------------------------------------------------------ #

@app.get("/jobs", response_model=List[JobPostingResponse])
def read_jobs(
    skip: int = 0,
    limit: int = 100,
    company: Optional[str] = Query(None, description="Filter by company name (case-insensitive)"),
    portal: Optional[str] = Query(None, description="Filter by portal e.g. Greenhouse, Lever"),
    db: Session = Depends(get_db)
):
    """
    List all ingested jobs.
    Optional filters: ?company=GitLab  or  ?portal=Greenhouse
    """
    q = db.query(JobPosting)
    if company:
        q = q.filter(JobPosting.company.ilike(f"%{company}%"))
    if portal:
        q = q.filter(JobPosting.portal.ilike(f"%{portal}%"))
    return q.offset(skip).limit(limit).all()


@app.post("/jobs/ingest")
def trigger_ingest(req: IngestRequest, db: Session = Depends(get_db)):
    """
    Ingest jobs from multiple companies.
    POST /jobs/ingest
    Body: { "greenhouse": ["gitlab", "stripe"], "lever": ["linear"] }
    If body is empty {}, falls back to a default set.
    """
    greenhouse_tokens = req.greenhouse or []
    lever_tokens = req.lever or []

    # Default set if caller sends empty body
    if not greenhouse_tokens and not lever_tokens:
        greenhouse_tokens = ["gitlab"]
        lever_tokens = []

    ingestors = build_ingestors(
        greenhouse_tokens=greenhouse_tokens,
        lever_tokens=lever_tokens
    )
    added = ingest_jobs(db, ingestors)
    companies = greenhouse_tokens + lever_tokens
    return {
        "message": f"Successfully ingested {added} new jobs",
        "companies_scraped": companies
    }


# ------------------------------------------------------------------ #
# Matching                                                             #
# ------------------------------------------------------------------ #

@app.post("/applications/match")
def trigger_match(req: MatchRequest, db: Session = Depends(get_db)):
    """Match unmatched jobs against the base resume (processes 5 at a time)."""
    jobs = (
        db.query(JobPosting)
        .outerjoin(Application)
        .filter(Application.id == None)
        .limit(5)
        .all()
    )
    matched_count = 0
    for job in jobs:
        print(f"Matching job {job.id}: {job.title} at {job.company}...")
        match_job(db, job, req.base_resume)
        matched_count += 1
    return {"message": f"Matched {matched_count} jobs"}


# ------------------------------------------------------------------ #
# Applications dashboard                                               #
# ------------------------------------------------------------------ #

@app.get("/applications", response_model=List[ApplicationResponse])
def read_applications(
    skip: int = 0,
    limit: int = 100,
    status: Optional[str] = Query(None, description="Filter by status: NEW, TAILORED, APPLIED, FAILED"),
    min_score: Optional[float] = Query(None, description="Only return applications with fit_score >= this value"),
    company: Optional[str] = Query(None, description="Filter by company name (case-insensitive)"),
    db: Session = Depends(get_db)
):
    """
    List applications with optional filters.
    Examples:
      GET /applications?status=NEW
      GET /applications?min_score=0.7
      GET /applications?company=GitLab&min_score=0.6
    """
    q = db.query(Application)
    if status:
        q = q.filter(Application.status == status.upper())
    if min_score is not None:
        q = q.filter(Application.fit_score >= min_score)
    if company:
        # Join with JobPosting to filter by company name
        q = q.join(JobPosting, Application.job_id == JobPosting.id)
        q = q.filter(JobPosting.company.ilike(f"%{company}%"))
    q = q.order_by(Application.fit_score.desc().nullslast())
    return q.offset(skip).limit(limit).all()


# ------------------------------------------------------------------ #
# Tailor & Apply                                                       #
# ------------------------------------------------------------------ #

@app.post("/applications/{app_id}/tailor")
def trigger_tailor(
    app_id: int,
    base_resume_data: dict,
    db: Session = Depends(get_db)
):
    app_entry = db.query(Application).filter(Application.id == app_id).first()
    if not app_entry:
        raise HTTPException(status_code=404, detail="Application not found")
    generate_tailored_resume(db, app_entry, base_resume_data)
    return {"message": "Tailored resume generated"}


@app.post("/applications/{app_id}/apply")
def trigger_apply(
    app_id: int,
    candidate_profile: dict,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    app_entry = db.query(Application).filter(Application.id == app_id).first()
    if not app_entry:
        raise HTTPException(status_code=404, detail="Application not found")
    background_tasks.add_task(process_application, db, app_entry, candidate_profile)
    return {"message": "Application apply task started in background"}
