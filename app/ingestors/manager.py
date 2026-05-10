import logging

from sqlalchemy.orm import Session

from app.models.job import JobPosting
from app.ingestors.greenhouse import GreenhouseIngestor
from app.ingestors.lever import LeverIngestor

logger = logging.getLogger(__name__)


def build_ingestors(greenhouse_tokens: list = None, lever_tokens: list = None):
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
        # Use getattr so this never raises AttributeError if the base
        # contract changes or a custom ingestor omits board_token.
        board_token = getattr(ingestor, "board_token", "<unknown>")
        logger.info("Running ingestor: %s / %s", ingestor.portal_name, board_token)

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

    logger.info("Total jobs added: %d", total_added)
    return total_added


if __name__ == "__main__":
    import app.models.job          # noqa: F401 — ensure JobPosting model is registered
    import app.models.application  # noqa: F401 — ensure Application model is registered
    from app.database import SessionLocal, Base, engine

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Auto-create all tables if the DB is fresh (safe no-op if already exist)
    Base.metadata.create_all(bind=engine)
    logger.info("DB tables ensured.")

    db = SessionLocal()

    # Edit these lists to add more companies
    ingestors = build_ingestors(
        greenhouse_tokens=["gitlab", "stripe", "figma", "notion"],
        lever_tokens=["linear", "vercel"]
    )
    ingest_jobs(db, ingestors)
