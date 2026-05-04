from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
import time

from app.database import engine, Base, get_db, SessionLocal
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
# In-memory match-all state (single worker process)                   #
# ------------------------------------------------------------------ #

match_status = {
    "running": False,
    "matched": 0,
    "remaining": 0,
    "total": 0,
    "message": "idle"
}


def _run_match_all(base_resume: str, batch_size: int, delay: int):
    """Background task: loop until all unmatched jobs are processed."""
    global match_status
    db = SessionLocal()
    try:
        match_status["running"] = True
        match_status["matched"] = 0
        match_status["message"] = "starting"

        # Count total unmatched upfront
        total = (
            db.query(JobPosting)
            .outerjoin(Application)
            .filter(Application.id == None)
            .count()
        )
        match_status["total"] = total
        match_status["remaining"] = total

        while True:
            unmatched = (
                db.query(JobPosting)
                .outerjoin(Application)
                .filter(Application.id == None)
                .limit(batch_size)
                .all()
            )
            if not unmatched:
                break

            for job in unmatched:
                print(f"[match-all] Matching job {job.id}: {job.title} at {job.company}")
                match_job(db, job, base_resume)
                match_status["matched"] += 1
                match_status["remaining"] = max(0, match_status["remaining"] - 1)
                match_status["message"] = f"Matched {match_status['matched']} / {match_status['total']}"

            time.sleep(delay)

        match_status["running"] = False
        match_status["message"] = f"Done! Matched {match_status['matched']} jobs."
    except Exception as e:
        match_status["running"] = False
        match_status["message"] = f"Error: {str(e)}"
        print(f"[match-all] Error: {e}")
    finally:
        db.close()


# ------------------------------------------------------------------ #
# Request bodies                                                       #
# ------------------------------------------------------------------ #

class MatchRequest(BaseModel):
    base_resume: str
    batch_size: int = 5

class MatchAllRequest(BaseModel):
    base_resume: str
    batch_size: int = 10
    delay: int = 3

class IngestRequest(BaseModel):
    greenhouse: Optional[List[str]] = []
    lever: Optional[List[str]] = []


# ------------------------------------------------------------------ #
# Jobs                                                                 #
# ------------------------------------------------------------------ #

@app.get("/jobs", response_model=List[JobPostingResponse])
def read_jobs(
    skip: int = 0,
    limit: int = 100,
    company: Optional[str] = Query(None),
    portal: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    q = db.query(JobPosting)
    if company:
        q = q.filter(JobPosting.company.ilike(f"%{company}%"))
    if portal:
        q = q.filter(JobPosting.portal.ilike(f"%{portal}%"))
    return q.offset(skip).limit(limit).all()


@app.post("/jobs/ingest")
def trigger_ingest(req: IngestRequest, db: Session = Depends(get_db)):
    greenhouse_tokens = req.greenhouse or []
    lever_tokens = req.lever or []
    if not greenhouse_tokens and not lever_tokens:
        greenhouse_tokens = ["gitlab"]
    ingestors = build_ingestors(
        greenhouse_tokens=greenhouse_tokens,
        lever_tokens=lever_tokens
    )
    added = ingest_jobs(db, ingestors)
    return {
        "message": f"Successfully ingested {added} new jobs",
        "companies_scraped": greenhouse_tokens + lever_tokens
    }


# ------------------------------------------------------------------ #
# Matching                                                             #
# ------------------------------------------------------------------ #

@app.post("/applications/match")
def trigger_match(req: MatchRequest, db: Session = Depends(get_db)):
    """Single batch match (5 jobs by default). UI-safe, returns remaining count."""
    batch_size = max(1, min(req.batch_size, 50))
    unmatched = (
        db.query(JobPosting)
        .outerjoin(Application)
        .filter(Application.id == None)
        .all()
    )
    total_remaining = len(unmatched)
    to_process = unmatched[:batch_size]
    matched_count = 0
    for job in to_process:
        print(f"Matching job {job.id}: {job.title} at {job.company}...")
        match_job(db, job, req.base_resume)
        matched_count += 1
    return {
        "message": f"Matched {matched_count} jobs",
        "matched_count": matched_count,
        "remaining_unmatched": total_remaining - matched_count
    }


@app.post("/applications/match-all")
def trigger_match_all(req: MatchAllRequest, background_tasks: BackgroundTasks):
    """Start background match-all loop. Returns immediately so UI stays free."""
    global match_status
    if match_status["running"]:
        return {"message": "Match-all already running", "status": match_status}
    batch_size = max(1, min(req.batch_size, 50))
    delay = max(1, min(req.delay, 60))
    background_tasks.add_task(_run_match_all, req.base_resume, batch_size, delay)
    return {"message": "Match-all started in background", "status": match_status}


@app.get("/applications/match-all/status")
def get_match_all_status():
    """Poll this to get live progress of the background match-all task."""
    return match_status


# ------------------------------------------------------------------ #
# Applications                                                         #
# ------------------------------------------------------------------ #

@app.get("/applications", response_model=List[ApplicationResponse])
def read_applications(
    skip: int = 0,
    limit: int = 100,
    status: Optional[str] = Query(None),
    min_score: Optional[float] = Query(None),
    company: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    q = db.query(Application)
    if status:
        q = q.filter(Application.status == status.upper())
    if min_score is not None:
        q = q.filter(Application.fit_score >= min_score)
    if company:
        q = q.join(JobPosting, Application.job_id == JobPosting.id)
        q = q.filter(JobPosting.company.ilike(f"%{company}%"))
    q = q.order_by(Application.fit_score.desc().nullslast())
    return q.offset(skip).limit(limit).all()


# ------------------------------------------------------------------ #
# Tailor & Apply                                                       #
# ------------------------------------------------------------------ #

@app.post("/applications/{app_id}/tailor")
def trigger_tailor(app_id: int, base_resume_data: dict, db: Session = Depends(get_db)):
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
