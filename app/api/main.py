from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
import os
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

MAX_RETRIES = 5
RETRY_BASE_DELAY = 30  # seconds; doubles each attempt: 30, 60, 120, 240, 480

# ------------------------------------------------------------------ #
# Status taxonomy                                                      #
# ------------------------------------------------------------------ #
#
# Terminal SUCCESS — never touch again, hidden from dashboard by default
_APPLIED_STATUSES = {"AUTO_APPLIED", "APPLIED"}

# Needs tailoring before applying (no valid PDF on disk yet)
_NEEDS_TAILOR_STATUSES = {
    "PENDING",        # fresh app, never tailored
    "MATCHED",        # matched but not yet tailored
    "RESUME_FAILED",  # tailor engine failed — must retailor, PDF was never produced
}

# Already tailored (PDF exists), only the apply step is needed
_NEEDS_APPLY_STATUSES = {
    "TAILORED",           # tailored successfully, not yet applied
    "FAILED",             # apply step failed after a successful tailor
    "VALIDATION_FAILED",  # form validation failed after tailor
    "SKIPPED",            # was skipped earlier, retry apply now
}

# Everything process-all should act on (union of the two sets above)
_ACTIONABLE_STATUSES = list(_NEEDS_TAILOR_STATUSES | _NEEDS_APPLY_STATUSES)


# ------------------------------------------------------------------ #
# In-memory state                                                      #
# ------------------------------------------------------------------ #

match_status = {
    "running": False,
    "matched": 0,
    "remaining": 0,
    "total": 0,
    "skipped": 0,
    "message": "idle"
}

process_status = {
    "running": False,
    "processed": 0,
    "failed": 0,
    "remaining": 0,
    "total": 0,
    "current_job": "",
    "message": "idle"
}


# ------------------------------------------------------------------ #
# Background workers                                                   #
# ------------------------------------------------------------------ #

def _match_job_with_retry(db, job, base_resume):
    """Call match_job with exponential backoff on rate-limit errors."""
    for attempt in range(MAX_RETRIES):
        try:
            match_job(db, job, base_resume)
            return True
        except Exception as e:
            err = str(e).lower()
            if "429" in err or "rate limit" in err or "rate_limit" in err:
                wait = RETRY_BASE_DELAY * (2 ** attempt)
                match_status["message"] = (
                    f"Rate limited — waiting {wait}s (attempt {attempt + 1}/{MAX_RETRIES})..."
                )
                print(f"[match-all] Rate limited on job {job.id}. Waiting {wait}s...")
                time.sleep(wait)
            else:
                print(f"[match-all] Non-retryable error on job {job.id}: {e}")
                return False
    print(f"[match-all] Exhausted retries for job {job.id}. Skipping.")
    return False


def _run_match_all(base_resume: str, batch_size: int, delay: int):
    """Background task: loop until all unmatched jobs are processed."""
    global match_status
    db = SessionLocal()
    try:
        match_status.update({"running": True, "matched": 0, "skipped": 0, "message": "starting"})

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
                match_status["message"] = f"Matching: {job.title} @ {job.company}"
                success = _match_job_with_retry(db, job, base_resume)
                if success:
                    match_status["matched"] += 1
                else:
                    match_status["skipped"] += 1
                match_status["remaining"] = max(0, match_status["remaining"] - 1)
                match_status["message"] = (
                    f"Matched {match_status['matched']} / {match_status['total']}"
                    f" ({match_status['skipped']} skipped)"
                )

            time.sleep(delay)

        match_status["running"] = False
        match_status["message"] = (
            f"Done! Matched {match_status['matched']} jobs"
            + (f", {match_status['skipped']} skipped" if match_status["skipped"] else ".")
        )
    except Exception as e:
        match_status["running"] = False
        match_status["message"] = f"Error: {str(e)}"
        print(f"[match-all] Error: {e}")
    finally:
        db.close()


def _run_process_all(
    base_resume_json: dict,
    candidate_profile: dict,
    fit_threshold: float,
    delay: int
):
    """
    Background task: tailor then apply every qualifying actionable application.

    Status routing:
      PENDING, MATCHED, RESUME_FAILED  →  tailor + apply
      TAILORED, FAILED,
      VALIDATION_FAILED, SKIPPED       →  apply only (PDF already on disk)

    Successfully applied (AUTO_APPLIED / APPLIED) are never touched.
    """
    global process_status
    db = SessionLocal()
    try:
        process_status.update({
            "running": True, "processed": 0, "failed": 0,
            "current_job": "", "message": "starting"
        })

        apps = (
            db.query(Application)
            .join(JobPosting, Application.job_id == JobPosting.id)
            .filter(
                Application.fit_score >= fit_threshold,
                Application.status.in_(_ACTIONABLE_STATUSES)
            )
            .order_by(Application.fit_score.desc())
            .all()
        )

        process_status["total"] = len(apps)
        process_status["remaining"] = len(apps)

        # Early exit with a clear, informative message instead of "Applied to 0 jobs"
        if not apps:
            process_status["running"] = False
            process_status["message"] = (
                f"No actionable applications found. "
                f"All jobs are either already applied (AUTO_APPLIED/APPLIED) "
                f"or below the score threshold ({fit_threshold}). "
                f"Check dashboard with ?include_applied=true to see full history."
            )
            print(f"[process-all] {process_status['message']}")
            return

        print(f"[process-all] Found {len(apps)} actionable apps to process.")

        for app_entry in apps:
            job = db.query(JobPosting).filter(JobPosting.id == app_entry.job_id).first()
            job_label = f"{job.title} @ {job.company}" if job else f"App #{app_entry.id}"

            # ----------------------------------------------------------
            # Step 1: Decide whether to tailor
            # ----------------------------------------------------------
            needs_tailor = app_entry.status in _NEEDS_TAILOR_STATUSES

            # Safety net: status says TAILORED/FAILED but PDF is missing on disk
            if not needs_tailor:
                pdf_ok = (
                    app_entry.tailored_resume_pdf_path
                    and os.path.isfile(app_entry.tailored_resume_pdf_path)
                )
                if not pdf_ok:
                    print(
                        f"[process-all] App {app_entry.id}: status={app_entry.status} "
                        f"but PDF missing on disk — forcing retailor."
                    )
                    needs_tailor = True

            if needs_tailor:
                try:
                    process_status["current_job"] = f"Tailoring: {job_label}"
                    process_status["message"] = process_status["current_job"]
                    print(f"[process-all] Tailoring {job_label}")
                    generate_tailored_resume(db, app_entry, base_resume_json)
                except Exception as e:
                    err = str(e).lower()
                    if "429" in err or "rate limit" in err or "rate_limit" in err:
                        wait = RETRY_BASE_DELAY
                        process_status["message"] = f"Rate limited during tailor — waiting {wait}s..."
                        print(f"[process-all] Rate limited tailoring {job_label}. Waiting {wait}s.")
                        time.sleep(wait)
                        try:
                            generate_tailored_resume(db, app_entry, base_resume_json)
                        except Exception as e2:
                            print(f"[process-all] Tailor retry failed for {job_label}: {e2}")
                            process_status["failed"] += 1
                            process_status["remaining"] -= 1
                            continue
                    else:
                        print(f"[process-all] Tailor error for {job_label}: {e}")
                        process_status["failed"] += 1
                        process_status["remaining"] -= 1
                        continue

            # ----------------------------------------------------------
            # Step 2: Apply
            # ----------------------------------------------------------
            try:
                process_status["current_job"] = f"Applying: {job_label}"
                process_status["message"] = process_status["current_job"]
                print(f"[process-all] Applying {job_label}")
                process_application(db, app_entry, candidate_profile)
                process_status["processed"] += 1
            except Exception as e:
                print(f"[process-all] Apply error for {job_label}: {e}")
                process_status["failed"] += 1

            process_status["remaining"] = max(0, process_status["remaining"] - 1)
            process_status["message"] = (
                f"Processed {process_status['processed']} / {process_status['total']}"
                f" ({process_status['failed']} failed)"
            )
            time.sleep(delay)

        process_status["running"] = False
        process_status["current_job"] = ""
        process_status["message"] = (
            f"Done! Applied to {process_status['processed']} jobs"
            + (f", {process_status['failed']} failed." if process_status["failed"] else ".")
        )
    except Exception as e:
        process_status["running"] = False
        process_status["current_job"] = ""
        process_status["message"] = f"Error: {str(e)}"
        print(f"[process-all] Fatal error: {e}")
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
    delay: int = 5

class ProcessAllRequest(BaseModel):
    base_resume_json: dict
    candidate_profile: dict
    fit_threshold: float = 60.0
    delay: int = 5

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
    ingestors = build_ingestors(greenhouse_tokens=greenhouse_tokens, lever_tokens=lever_tokens)
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
    """Single batch match. Returns remaining count."""
    batch_size = max(1, min(req.batch_size, 50))
    unmatched = (
        db.query(JobPosting)
        .outerjoin(Application)
        .filter(Application.id == None)
        .all()
    )
    total_remaining = len(unmatched)
    matched_count = 0
    for job in unmatched[:batch_size]:
        print(f"Matching job {job.id}: {job.title} at {job.company}...")
        success = _match_job_with_retry(db, job, req.base_resume)
        if success:
            matched_count += 1
    return {
        "message": f"Matched {matched_count} jobs",
        "matched_count": matched_count,
        "remaining_unmatched": total_remaining - matched_count
    }


@app.post("/applications/match-all")
def trigger_match_all(req: MatchAllRequest, background_tasks: BackgroundTasks):
    """Start background match-all loop. Returns immediately."""
    global match_status
    if match_status["running"]:
        return {"message": "Match-all already running", "status": match_status}
    batch_size = max(1, min(req.batch_size, 50))
    delay = max(1, min(req.delay, 60))
    background_tasks.add_task(_run_match_all, req.base_resume, batch_size, delay)
    return {"message": "Match-all started in background", "status": match_status}


@app.get("/applications/match-all/status")
def get_match_all_status():
    return match_status


@app.post("/applications/process-all")
def trigger_process_all(req: ProcessAllRequest, background_tasks: BackgroundTasks):
    """Start background tailor+apply loop for all qualifying apps. Returns immediately."""
    global process_status
    if process_status["running"]:
        return {"message": "Process-all already running", "status": process_status}
    delay = max(1, min(req.delay, 60))
    background_tasks.add_task(
        _run_process_all,
        req.base_resume_json,
        req.candidate_profile,
        req.fit_threshold,
        delay
    )
    return {"message": "Process-all started in background", "status": process_status}


@app.get("/applications/process-all/status")
def get_process_all_status():
    return process_status


@app.post("/applications/process-all/reset")
def reset_process_all_status():
    """
    Reset the in-memory process_status back to idle defaults.
    Call this from the dashboard BEFORE starting a new process-all run
    so the previous run's stale message is cleared immediately.
    Only allowed when a run is not currently active.
    """
    global process_status
    if process_status["running"]:
        return {"message": "Cannot reset — process-all is currently running.", "status": process_status}
    process_status = {
        "running": False,
        "processed": 0,
        "failed": 0,
        "remaining": 0,
        "total": 0,
        "current_job": "",
        "message": "idle"
    }
    return {"message": "Process-all status reset to idle.", "status": process_status}


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
    include_applied: bool = Query(
        False,
        description="Set to true to also show successfully applied jobs (AUTO_APPLIED / APPLIED). "
                    "Default is false — the dashboard hides completed applications."
    ),
    db: Session = Depends(get_db)
):
    """
    List applications.

    Default behaviour (include_applied=false):
      Returns every application still needing attention — PENDING, MATCHED,
      TAILORED, FAILED, VALIDATION_FAILED, SKIPPED, RESUME_FAILED.
      Successfully applied jobs (AUTO_APPLIED / APPLIED) are hidden to keep
      the dashboard clean and focused on actionable items.

    Pass ?include_applied=true to see full history including applied jobs.
    Pass ?status=RESUME_FAILED to filter to a specific status.
    """
    q = db.query(Application)

    if status:
        q = q.filter(Application.status == status.upper())
    elif not include_applied:
        q = q.filter(Application.status.notin_(list(_APPLIED_STATUSES)))

    if min_score is not None:
        q = q.filter(Application.fit_score >= min_score)
    if company:
        q = q.join(JobPosting, Application.job_id == JobPosting.id)
        q = q.filter(JobPosting.company.ilike(f"%{company}%"))
    q = q.order_by(Application.fit_score.desc().nullslast())
    return q.offset(skip).limit(limit).all()


# ------------------------------------------------------------------ #
# Tailor & Apply (individual)                                          #
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
