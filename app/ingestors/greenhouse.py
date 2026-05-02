import requests
from typing import List
import html
from app.ingestors.base import BaseIngestor
from app.schemas.job import JobPostingCreate

class GreenhouseIngestor(BaseIngestor):
    portal_name = "Greenhouse"

    def __init__(self, board_token: str):
        self.board_token = board_token
        self.base_url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs"

    def fetch_jobs(self, **kwargs) -> List[dict]:
        try:
            # ?content=true to get descriptions
            response = requests.get(f"{self.base_url}?content=true")
            response.raise_for_status()
            data = response.json()
            return data.get("jobs", [])
        except Exception as e:
            print(f"Error fetching from Greenhouse for {self.board_token}: {e}")
            return []

    def normalize(self, raw_job: dict) -> JobPostingCreate:
        # greenhouse often has HTML in descriptions
        description = raw_job.get("content", "")
        # Very simple HTML strip
        import re
        clean_desc = re.sub('<[^<]+?>', '', html.unescape(description))
        
        location = raw_job.get("location", {}).get("name", "")
        
        return JobPostingCreate(
            portal=self.portal_name,
            portal_job_id=str(raw_job.get("id")),
            title=raw_job.get("title", "Unknown"),
            company=self.board_token, # Using board token as company name placeholder
            location=location,
            description=clean_desc.strip()[:2000], # Trucated for simplicity if needed
            url=raw_job.get("absolute_url", ""),
            raw_data=raw_job
        )
