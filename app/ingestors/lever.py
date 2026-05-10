import html as _html
import logging
import re
from typing import List

import requests

from app.ingestors.base import BaseIngestor
from app.ingestors.filter import filter_and_diversify, _strip_html
from app.schemas.job import JobPostingCreate

logger = logging.getLogger(__name__)


class LeverIngestor(BaseIngestor):
    portal_name = "Lever"

    def __init__(self, board_token: str):
        self.board_token = board_token
        self.base_url = f"https://api.lever.co/v0/postings/{board_token}"

    def fetch_jobs(self, **kwargs) -> List[dict]:
        try:
            response = requests.get(
                f"{self.base_url}?mode=json&limit=250", timeout=30
            )
            response.raise_for_status()
            raw_jobs = response.json() or []

            for job in raw_jobs:
                # Flatten nested location -> plain string.
                job["_location_str"] = (
                    (job.get("categories") or {}).get("location", "")
                    or job.get("workplaceType", "")
                    or ""
                )
                # Fix: descriptionPlain can be None/absent on some Lever postings.
                # Previously the filter received None which caused keyword scoring
                # to silently skip those jobs or crash on str operations.
                # Resolve descriptionPlain here, fall back to HTML-stripped 'description'.
                plain = job.get("descriptionPlain")
                if not plain:
                    raw_html_desc = job.get("description") or ""
                    plain = _strip_html(raw_html_desc)
                job["_desc_plain"] = plain

            company_name = self.board_token.title()
            filtered = filter_and_diversify(
                raw_jobs,
                company=company_name,
                title_key="text",
                description_key="_desc_plain",   # guaranteed non-None string
                location_key="_location_str",
            )
            return filtered
        except Exception:
            logger.exception(
                "Error fetching Lever jobs for '%s'", self.board_token
            )
            return []

    def normalize(self, raw_job: dict) -> JobPostingCreate:
        # Use pre-resolved plain description if available.
        description = raw_job.get("_desc_plain") or ""
        if not description:
            # Fallback: strip HTML from the raw description field.
            raw_html = raw_job.get("description") or ""
            description = _strip_html(raw_html)

        location = (
            (raw_job.get("categories") or {}).get("location", "")
            or raw_job.get("workplaceType", "")
            or ""
        )

        company = raw_job.get("company") or self.board_token.title()

        return JobPostingCreate(
            portal=self.portal_name,
            portal_job_id=raw_job.get("id", ""),
            title=raw_job.get("text") or "Unknown",
            company=company,
            location=location,
            description=description.strip()[:2000],
            url=raw_job.get("hostedUrl") or raw_job.get("applyUrl", ""),
            raw_data=raw_job,
        )
