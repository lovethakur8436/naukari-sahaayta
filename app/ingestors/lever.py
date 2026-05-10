import html
import logging
import re
from typing import List

import requests

from app.ingestors.base import BaseIngestor
from app.ingestors.filter import filter_and_diversify
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
            raw_jobs = response.json()

            # Flatten Lever's nested location into a plain string once, here.
            # normalize() mirrors this same resolution logic independently so
            # it is safe to call without fetch_jobs() having run first.
            for job in raw_jobs:
                job["_location_str"] = (
                    job.get("categories", {}).get("location", "")
                    or job.get("workplaceType", "")
                    or ""
                )

            company_name = self.board_token.title()
            filtered = filter_and_diversify(
                raw_jobs,
                company=company_name,
                title_key="text",             # Lever uses 'text' for job title
                description_key="descriptionPlain",
                location_key="_location_str",
            )
            return filtered
        except Exception:
            logger.exception("Error fetching Lever jobs for '%s'", self.board_token)
            return []

    def normalize(self, raw_job: dict) -> JobPostingCreate:
        description = raw_job.get("descriptionPlain") or raw_job.get("description", "")
        if "<" in description:
            description = re.sub('<[^<]+?>', '', html.unescape(description))

        # Derive location self-sufficiently — do NOT rely on _location_str
        # being present (normalize() may be called without fetch_jobs()).
        location = (
            raw_job.get("categories", {}).get("location", "")
            or raw_job.get("workplaceType", "")
            or ""
        )

        company = raw_job.get("company") or self.board_token.title()

        return JobPostingCreate(
            portal=self.portal_name,
            portal_job_id=raw_job.get("id", ""),
            title=raw_job.get("text", "Unknown"),
            company=company,
            location=location,
            description=description.strip()[:2000],
            url=raw_job.get("hostedUrl") or raw_job.get("applyUrl", ""),
            raw_data=raw_job
        )
