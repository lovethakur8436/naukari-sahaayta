"""
app/apply_agent/resume_guard.py

Single responsibility: ensure a tailored resume PDF exists on disk
before the browser apply-agent opens.

Public API:
    resolve_resume(db, application, base_resume_data) -> str
        Returns the absolute path to the ready PDF.
        Raises ResumeNotReadyError on unrecoverable failure.

Logic:
    1. Check application.tailored_resume_pdf_path — if set AND the file
       exists on disk, reuse it (no LLM call, no re-render).
    2. Derive the expected PDF path from the naming convention used by
       tailor/engine.py and check that path too (handles the case where
       the file exists but the DB column was never updated).
    3. If neither exists, call generate_tailored_resume() to create it.
    4. Verify the resulting file is a non-empty, readable PDF before
       returning the path.
"""

import logging
import os
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.application import Application
from app.tailor.engine import generate_tailored_resume

logger = logging.getLogger(__name__)

# Directory where the tailor engine writes resumes (mirrors engine.py)
_RESUME_DIR = Path("data")


class ResumeNotReadyError(Exception):
    """
    Raised when a tailored resume cannot be resolved or generated.
    Carries the application ID and a human-readable reason so the
    caller can log it and mark the application as RESUME_FAILED.
    """

    def __init__(self, application_id: int, reason: str):
        self.application_id = application_id
        self.reason = reason
        super().__init__(
            f"[resume_guard] App {application_id}: {reason}"
        )


def _expected_pdf_path(application: Application) -> Path:
    """
    Derive the PDF path using the same naming convention as tailor/engine.py:
        data/resume_<app_id>_<job_id>_<COMPANY>.pdf
    This lets us find the file even when the DB column was never written.
    """
    job = application.job
    company_abbr = "".join(
        c for c in (job.company or "") if c.isalnum()
    )[:8].upper()
    return _RESUME_DIR / f"resume_{application.id}_{job.id}_{company_abbr}.pdf"


def _is_valid_pdf(path: Path) -> bool:
    """Return True only if the path is a non-empty, readable file."""
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def resolve_resume(
    db: Session,
    application: Application,
    base_resume_data: dict,
) -> str:
    """
    Ensure a tailored resume PDF exists for *application* and return
    its absolute path as a string.

    Steps
    -----
    1. Check DB column  -> path on disk  -> reuse.
    2. Check expected path convention   -> sync DB column -> reuse.
    3. Generate via tailor engine       -> verify -> sync DB column.

    Parameters
    ----------
    db                : active SQLAlchemy session
    application       : Application ORM row (must have .job loaded)
    base_resume_data  : candidate base JSON passed to the tailor engine

    Returns
    -------
    str  — absolute path to the PDF file.

    Raises
    ------
    ResumeNotReadyError  — if generation fails or the file is still missing.
    """
    app_id = application.id

    # ── Check 1: DB column ────────────────────────────────────────────────────
    db_path = application.tailored_resume_pdf_path
    if db_path:
        pdf = Path(db_path)
        if _is_valid_pdf(pdf):
            logger.info(
                "[resume_guard] App %d: reusing existing resume at '%s'",
                app_id, pdf,
            )
            return str(pdf.resolve())
        else:
            logger.warning(
                "[resume_guard] App %d: DB path '%s' does not exist or is empty — "
                "will regenerate.",
                app_id, db_path,
            )

    # ── Check 2: expected path by naming convention ───────────────────────────
    expected = _expected_pdf_path(application)
    if _is_valid_pdf(expected):
        logger.info(
            "[resume_guard] App %d: found resume at expected path '%s' "
            "(DB column was not set — syncing now).",
            app_id, expected,
        )
        application.tailored_resume_pdf_path = str(expected)
        db.commit()
        return str(expected.resolve())

    # ── Step 3: generate ──────────────────────────────────────────────────────
    logger.info(
        "[resume_guard] App %d: no tailored resume found — generating now "
        "(job: '%s' @ %s).",
        app_id,
        application.job.title,
        application.job.company,
    )

    try:
        generate_tailored_resume(db, application, base_resume_data)
    except Exception as exc:
        raise ResumeNotReadyError(
            app_id,
            f"tailor engine failed: {exc}",
        ) from exc

    # ── Verify the engine actually wrote the file ────────────────────────────
    # generate_tailored_resume() sets application.tailored_resume_pdf_path and
    # commits. Re-read the path from the (now updated) application object.
    final_path = application.tailored_resume_pdf_path
    if not final_path:
        raise ResumeNotReadyError(
            app_id,
            "tailor engine returned without setting tailored_resume_pdf_path.",
        )

    pdf = Path(final_path)
    if not _is_valid_pdf(pdf):
        raise ResumeNotReadyError(
            app_id,
            f"tailor engine completed but PDF not found or empty at '{pdf}'.",
        )

    logger.info(
        "[resume_guard] App %d: resume generated successfully at '%s'.",
        app_id, pdf,
    )
    return str(pdf.resolve())
