from sqlalchemy.orm import Session
from app.models.job import JobPosting
from app.ingestors.greenhouse import GreenhouseIngestor
from app.ingestors.lever import LeverIngestor

def ingest_jobs(db: Session, ingestors: list):
    total_added = 0
    for ingestor in ingestors:
        print(f"Running ingestor: {ingestor.portal_name}")
        normalized_jobs = ingestor.run()
        for job_data in normalized_jobs:
            # Dedupe based on portal and portal_job_id
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
    
    # Just an example with some public boards
    ingestors = [
        GreenhouseIngestor("gitlab"),
        LeverIngestor("netflix")
    ]
    
    ingest_jobs(db, ingestors)
