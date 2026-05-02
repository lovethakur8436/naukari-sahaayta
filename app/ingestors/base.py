from abc import ABC, abstractmethod
from typing import List
from app.schemas.job import JobPostingCreate

class BaseIngestor(ABC):
    @property
    @abstractmethod
    def portal_name(self) -> str:
        pass

    @abstractmethod
    def fetch_jobs(self, **kwargs) -> List[dict]:
        """Fetch raw jobs from the portal"""
        pass
    
    @abstractmethod
    def normalize(self, raw_job: dict) -> JobPostingCreate:
        """Normalize a raw job to the common schema"""
        pass

    def run(self, **kwargs) -> List[JobPostingCreate]:
        raw_jobs = self.fetch_jobs(**kwargs)
        normalized_jobs = []
        for raw in raw_jobs:
            try:
                normalized = self.normalize(raw)
                normalized_jobs.append(normalized)
            except Exception as e:
                print(f"Failed to normalize job from {self.portal_name}: {e}")
        return normalized_jobs
