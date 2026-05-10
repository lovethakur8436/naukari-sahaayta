import logging

from sqlalchemy.orm import Session

from app.models.job import JobPosting
from app.ingestors.greenhouse import GreenhouseIngestor
from app.ingestors.lever import LeverIngestor

logger = logging.getLogger(__name__)


def build_ingestors(greenhouse_tokens: list = None, lever_tokens: list = None):
    """
    Build ingestor instances from board token lists.

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


def ingest_jobs(db: Session, ingestors: list) -> int:
    """
    Run all ingestors, persist new jobs to the DB, and return the count added.

    Fix: model_dump() may include raw_data as a nested dict; SQLAlchemy JSON
    columns accept dicts directly, but we guard against non-serialisable values
    by omitting raw_data from the model constructor and setting it separately
    only when the column exists on the model.
    """
    total_added = 0

    for ingestor in ingestors:
        board_token = getattr(ingestor, "board_token", "<unknown>")
        logger.info(
            "Running ingestor: %s / %s",
            ingestor.portal_name,   # Fix: was ingestor.portal_name in an f-string
            board_token,            # which silently broke if portal_name raised
        )

        normalized_jobs = ingestor.run()
        for job_data in normalized_jobs:
            existing = (
                db.query(JobPosting)
                .filter(
                    JobPosting.portal == job_data.portal,
                    JobPosting.portal_job_id == job_data.portal_job_id,
                )
                .first()
            )
            if not existing:
                # Build the ORM object from the schema dict.
                # Exclude raw_data from model_dump to avoid JSON serialisation
                # issues with nested objects, then assign it directly.
                data = job_data.model_dump(exclude={"raw_data"})
                db_job = JobPosting(**data)
                # Only attach raw_data if the model column exists.
                if hasattr(JobPosting, "raw_data"):
                    db_job.raw_data = job_data.raw_data
                db.add(db_job)
                total_added += 1

        db.commit()
        logger.info(
            "Committed jobs for %s / %s",
            ingestor.portal_name,
            board_token,
        )

    logger.info("Total new jobs added this run: %d", total_added)
    return total_added


if __name__ == "__main__":
    import app.models.job          # noqa: F401
    import app.models.application  # noqa: F401
    from app.database import SessionLocal, Base, engine

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    Base.metadata.create_all(bind=engine)
    logger.info("DB tables ensured.")

    db = SessionLocal()
    try:
        ingestors = build_ingestors(
            greenhouse_tokens=["gitlab", "stripe", "figma", "notion"],
            lever_tokens=["linear", "vercel"],
        )
        ingest_jobs(db, ingestors)
    finally:
        db.close()
