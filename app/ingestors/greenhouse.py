import requests
import html
import re
from typing import List
from app.ingestors.base import BaseIngestor
from app.ingestors.filter import filter_and_diversify
from app.schemas.job import JobPostingCreate


def _get_company_name(board_token: str) -> str:
    """
    Fetch the real company name from the Greenhouse board metadata API.
    Falls back to a title-cased version of the board_token if the call fails.
    """
    try:
        resp = requests.get(
            f"https://boards-api.greenhouse.io/v1/boards/{board_token}",
            timeout=10
        )
        if resp.status_code == 200:
            return resp.json().get("name", board_token.title())
    except Exception:
        pass
    return board_token.title()


class GreenhouseIngestor(BaseIngestor):
    portal_name = "Greenhouse"

    def __init__(self, board_token: str):
        self.board_token = board_token
        self.company_name = _get_company_name(board_token)
        self.base_url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs"

    def fetch_jobs(self, **kwargs) -> List[dict]:
        try:
            response = requests.get(f"{self.base_url}?content=true", timeout=30)
            response.raise_for_status()
            raw_jobs = response.json().get("jobs", [])

            # Greenhouse: location lives inside nested dict; flatten for the filter
            for job in raw_jobs:
                if isinstance(job.get("location"), dict):
                    job["_location_str"] = job["location"].get("name", "")
                else:
                    job["_location_str"] = str(job.get("location", ""))

            filtered = filter_and_diversify(
                raw_jobs,
                company=self.company_name,
                title_key="title",
                description_key="content",   # Greenhouse uses 'content' for description
                location_key="_location_str",
            )
            return filtered
        except Exception as e:
            print(f"Error fetching Greenhouse jobs for '{self.board_token}': {e}")
            return []

    def normalize(self, raw_job: dict) -> JobPostingCreate:
        description = raw_job.get("content", "")
        clean_desc = re.sub('<[^<]+?>', '', html.unescape(description))
        location = raw_job.get("location", {})
        if isinstance(location, dict):
            location = location.get("name", "")

        return JobPostingCreate(
            portal=self.portal_name,
            portal_job_id=str(raw_job.get("id")),
            title=raw_job.get("title", "Unknown"),
            company=self.company_name,
            location=location,
            description=clean_desc.strip()[:2000],
            url=raw_job.get("absolute_url", ""),
            raw_data=raw_job
        )
