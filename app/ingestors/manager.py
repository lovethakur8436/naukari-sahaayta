from sqlalchemy.orm import Session
from app.models.job import JobPosting
from app.ingestors.greenhouse import GreenhouseIngestor
from app.ingestors.lever import LeverIngestor


def build_ingestors(greenhouse_tokens: list[str] = None, lever_tokens: list[str] = None):
    """
    Build ingestor instances from board token lists.
    Pass greenhouse_tokens and/or lever_tokens to specify which companies to ingest.
    Example:
        build_ingestors(
            greenhouse_tokens=["gitlab", "stripe", "figma"],
            lever_tokens=["linear", "vercel"]
        )
    """
    ingestors = []
    for token in (greenhouse_tokens or []):
        ingestors.append(GreenhouseIngestor(token))
    for token in (lever_tokens or []):
        ingestors.append(LeverIngestor(token))
    return ingestors


def ingest_jobs(db: Session, ingestors: list):
    total_added = 0
    for ingestor in ingestors:
        print(f"Running ingestor: {ingestor.portal_name} / {ingestor.board_token}")
        normalized_jobs = ingestor.run()
        for job_data in normalized_jobs:
            existing = db.query(JobPosting).filter(
                JobPosting.portal == job_data.portal,
                JobPosting.portal_job_id == job_data.portal_job_id
            ).first()
            if not existing:
                db_job = JobPosting(**job_data.model_dump())
                db.add(db_job)
                total_added += 1
        db.commit()
    print(f"Total jobs added: {total_added}")
    return total_added


if __name__ == "__main__":
    from app.database import SessionLocal
    db = SessionLocal()

    # Edit these lists to add more companies
    ingestors = build_ingestors(
        greenhouse_tokens=["gitlab", "stripe", "figma", "notion"],
        lever_tokens=["linear", "vercel"]
    )
    ingest_jobs(db, ingestors)
