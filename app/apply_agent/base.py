"""
app/apply_agent/base.py

Abstract base for all portal apply-agents.

Changes vs previous version:
- apply() gains a `resume_path` keyword argument.
  The path is stored as self.resume_path so subclasses can access
  it inside run_apply_flow() when uploading the resume file.
- If resume_path is not supplied explicitly, it falls back to
  application.tailored_resume_pdf_path (legacy support).
- result dict now carries 'resume_path' so callers can inspect it.
"""

import logging
from abc import ABC, abstractmethod
from typing import Optional

from playwright.sync_api import sync_playwright, Page

from app.models.application import Application

logger = logging.getLogger(__name__)

# Statuses set intentionally by run_apply_flow — base must NOT override them.
_TERMINAL_STATUSES = {"SKIPPED", "VALIDATION_FAILED", "FAILED", "RESUME_FAILED"}


class BaseApplyAgent(ABC):
    def __init__(self, headless: bool = True):
        self.headless = headless
        self.resume_path: Optional[str] = None  # set by apply() before flow runs

    def apply(
        self,
        application: Application,
        candidate_profile: dict,
        resume_path: Optional[str] = None,
    ) -> dict:
        """
        Launch a browser, run the apply flow, and return a result dict.

        Parameters
        ----------
        application       : Application ORM row
        candidate_profile : candidate data dict
        resume_path       : path to the tailored resume PDF.
                            If omitted, falls back to
                            application.tailored_resume_pdf_path.
        """
        # Resolve resume path — prefer explicit arg, then DB column.
        self.resume_path = resume_path or application.tailored_resume_pdf_path

        if not self.resume_path:
            logger.warning(
                "[agent] App %d: no resume_path provided and "
                "application.tailored_resume_pdf_path is unset. "
                "Resume upload step will be skipped.",
                application.id,
            )

        result: dict = {
            "status": "FAILED",
            "logs": [],
            "screenshot": None,
            "resume_path": self.resume_path,
        }

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=self.headless, slow_mo=500
                )
                context = browser.new_context()
                page = context.new_page()

                self.run_apply_flow(
                    page, application, candidate_profile, result
                )

                # Only promote to SUCCESS if run_apply_flow did not set a
                # terminal status (SKIPPED, VALIDATION_FAILED, FAILED, ...).
                if result["status"] not in _TERMINAL_STATUSES:
                    screenshot_path = f"data/success_{application.id}.png"
                    page.screenshot(path=screenshot_path)
                    result["screenshot"] = screenshot_path
                    result["status"] = "SUCCESS"

                browser.close()

        except Exception:
            logger.exception(
                "[agent] App %d: unhandled exception during apply.",
                application.id,
            )
            result["logs"].append("Unhandled exception — see server log for traceback.")

        return result

    @abstractmethod
    def run_apply_flow(
        self,
        page: Page,
        application: Application,
        candidate_profile: dict,
        result: dict,
    ) -> None:
        """
        Implement the portal-specific form-fill logic here.

        Access the resume file via  self.resume_path  (str | None).
        Append log messages to result['logs'].
        Set result['status'] to a terminal value only on hard failure.
        """
