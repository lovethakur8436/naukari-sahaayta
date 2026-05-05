import requests
import html
import re
from typing import List
from app.ingestors.base import BaseIngestor
from app.ingestors.filter import filter_and_diversify
from app.schemas.job import JobPostingCreate


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

            # Lever: location is nested under categories.location
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
        except Exception as e:
            print(f"Error fetching Lever jobs for '{self.board_token}': {e}")
            return []

    def normalize(self, raw_job: dict) -> JobPostingCreate:
        description = raw_job.get("descriptionPlain") or raw_job.get("description", "")
        if "<" in description:
            description = re.sub('<[^<]+?>', '', html.unescape(description))

        location = raw_job.get("_location_str") or (
            raw_job.get("categories", {}).get("location", "")
            or raw_job.get("workplaceType", "")
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
