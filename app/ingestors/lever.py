import requests
from typing import List
from app.ingestors.base import BaseIngestor
from app.schemas.job import JobPostingCreate

class LeverIngestor(BaseIngestor):
    portal_name = "Lever"

    def __init__(self, company_id: str):
        self.company_id = company_id
        self.base_url = f"https://api.lever.co/v0/postings/{company_id}"

    def fetch_jobs(self, **kwargs) -> List[dict]:
        try:
            response = requests.get(f"{self.base_url}?mode=json")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"Error fetching from Lever for {self.company_id}: {e}")
            return []

    def normalize(self, raw_job: dict) -> JobPostingCreate:
        description = raw_job.get("description", "")
        if "lists" in raw_job:
            for l in raw_job["lists"]:
                description += f"\n\n{l.get('text', '')}\n"
                description += "\n".join([f"- {i.get('text', '')}" for i in l.get("items", [])])
                
        location = raw_job.get("categories", {}).get("location", "")
        
        return JobPostingCreate(
            portal=self.portal_name,
            portal_job_id=str(raw_job.get("id")),
            title=raw_job.get("text", "Unknown"),
            company=self.company_id,
            location=location,
            description=description.strip(),
            url=raw_job.get("hostedUrl", ""),
            raw_data=raw_job
        )
