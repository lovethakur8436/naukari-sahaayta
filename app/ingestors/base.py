import logging
from abc import ABC, abstractmethod
from typing import List

from app.schemas.job import JobPostingCreate

logger = logging.getLogger(__name__)


class BaseIngestor(ABC):
    """
    Abstract base for all job-board ingestors.

    Concrete subclasses must:
      - Set a class-level `portal_name` string attribute.
      - Implement `fetch_jobs()` returning a list of raw dicts.
      - Implement `normalize()` converting one raw dict to JobPostingCreate.
    """

    # Subclasses declare this as a plain class attribute, e.g.:
    #   portal_name = "Greenhouse"
    # The @property/@abstractmethod pattern broke subclasses that set it
    # as a class attribute rather than a property, so it is removed here.
    portal_name: str = "Unknown"

    @abstractmethod
    def fetch_jobs(self, **kwargs) -> List[dict]:
        """Fetch raw job dicts from the portal API."""

    @abstractmethod
    def normalize(self, raw_job: dict) -> JobPostingCreate:
        """Convert a raw job dict to the common JobPostingCreate schema."""

    def run(self, **kwargs) -> List[JobPostingCreate]:
        """
        Fetch all jobs and normalize each one.
        Failures are logged and skipped; they do not abort the whole run.
        """
        raw_jobs = self.fetch_jobs(**kwargs)
        normalized: List[JobPostingCreate] = []
        for raw in raw_jobs:
            try:
                normalized.append(self.normalize(raw))
            except Exception:
                # Fix: was bare print(); now uses structured logging so errors
                # appear in the application log at WARNING level with traceback.
                logger.warning(
                    "Failed to normalize job from %s (id=%s)",
                    self.portal_name,
                    raw.get("id") or raw.get("_id", "?"),
                    exc_info=True,
                )
        return normalized
