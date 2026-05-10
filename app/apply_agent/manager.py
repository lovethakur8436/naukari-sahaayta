"""
app/apply_agent/manager.py

Orchestrates the full apply pipeline for one application:

  Step 0 — Resume guard
    Check if a tailored resume PDF already exists on disk.
    If not, generate it via the tailor engine.
    Abort with status=RESUME_FAILED (no browser opened) on failure.

  Step 1 — Browser apply
    Launch the portal-specific apply agent and run the form-fill flow.
    Persist result (status, logs, screenshot path) to the DB.
"""

import json
import logging
import os
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.application import Application
from app.apply_agent.greenhouse import GreenhouseApplyAgent
from app.apply_agent.resume_guard import ResumeNotReadyError, resolve_resume

logger = logging.getLogger(__name__)

# Ensure the data output directory exists at import time.
Path("data").mkdir(parents=True, exist_ok=True)


def process_application(
    db: Session,
    application: Application,
    candidate_profile: dict,
) -> None:
    """
    Run the full apply pipeline for *application*.

    Parameters
    ----------
    db                 : active SQLAlchemy session
    application        : Application ORM row (must have .job eagerly loaded)
    candidate_profile  : candidate data dict — must contain a
                         'base_resume_data' key with the raw resume JSON
                         used by the tailor engine.
    """
    app_id = application.id

    # ── Step 0: Resume guard ──────────────────────────────────────────────────
    # Extract the base resume data that the tailor engine needs.
    # It lives under candidate_profile['base_resume_data'] by convention;
    # fall back to the top-level dict itself for backwards compatibility.
    base_resume_data: dict = candidate_profile.get(
        "base_resume_data", candidate_profile
    )

    logger.info("[apply] App %d — Step 0: checking tailored resume.", app_id)
    try:
        resume_path = resolve_resume(db, application, base_resume_data)
        logger.info(
            "[apply] App %d — resume ready at '%s'.", app_id, resume_path
        )
    except ResumeNotReadyError as exc:
        logger.error(
            "[apply] App %d — RESUME_FAILED: %s", app_id, exc.reason
        )
        application.status = "RESUME_FAILED"
        _append_log(db, application, f"RESUME_FAILED: {exc.reason}")
        db.commit()
        return   # abort — no browser opened

    # ── Step 1: Browser apply ─────────────────────────────────────────────────
    portal = (application.job.portal or "").lower()
    logger.info(
        "[apply] App %d — Step 1: launching apply agent for portal '%s'.",
        app_id, portal,
    )

    if portal == "greenhouse":
        agent = GreenhouseApplyAgent(headless=False)
    else:
        logger.warning(
            "[apply] App %d — no agent configured for portal '%s'.",
            app_id, portal,
        )
        application.status = "SKIPPED"
        _append_log(
            db, application,
            f"No apply agent configured for portal '{portal}'.",
        )
        db.commit()
        return

    result = agent.apply(application, candidate_profile, resume_path=resume_path)

    # ── Persist result ────────────────────────────────────────────────────────
    log_path = f"data/apply_log_{app_id}.json"
    try:
        with open(log_path, "w") as f:
            json.dump(result, f, indent=2)
        application.submission_log_json_path = log_path
    except OSError as exc:
        logger.warning(
            "[apply] App %d — could not write apply log: %s", app_id, exc
        )

    application.screenshot_path = result.get("screenshot")
    application.status = (
        "AUTO_APPLIED" if result.get("status") == "SUCCESS" else "FAILED"
    )
    db.commit()
    logger.info(
        "[apply] App %d — finished with status '%s'.",
        app_id, application.status,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _append_log(db: Session, application: Application, message: str) -> None:
    """
    Append *message* to the application's submission log JSON file.
    Creates the file if it does not exist yet.
    """
    log_path = f"data/apply_log_{application.id}.json"
    try:
        if os.path.exists(log_path):
            with open(log_path) as f:
                data = json.load(f)
        else:
            data = {"status": application.status, "logs": [], "screenshot": None}
        data["logs"].append(message)
        with open(log_path, "w") as f:
            json.dump(data, f, indent=2)
        application.submission_log_json_path = log_path
    except OSError:
        pass  # non-fatal — logging failure must not crash the pipeline
