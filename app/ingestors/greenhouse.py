import html
import logging
import re
from typing import List

import requests

from app.ingestors.base import BaseIngestor
from app.ingestors.filter import filter_and_diversify, _strip_html
from app.schemas.job import JobPostingCreate

logger = logging.getLogger(__name__)


def _get_company_name(board_token: str) -> str:
    """
    Fetch the real company name from the Greenhouse board metadata API.
    Falls back to a title-cased version of the board_token if the call fails.
    """
    try:
        resp = requests.get(
            f"https://boards-api.greenhouse.io/v1/boards/{board_token}",
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("name") or board_token.title()
    except Exception:
        pass
    return board_token.title()


def _greenhouse_apply_url(board_token: str, job_id) -> str:
    """
    Canonical Greenhouse apply URL that is always stable and goes directly
    to the apply form, avoiding custom-career-page redirects.
    Format: https://boards.greenhouse.io/{board_token}/jobs/{job_id}
    """
    return f"https://boards.greenhouse.io/{board_token}/jobs/{job_id}"


class GreenhouseIngestor(BaseIngestor):
    portal_name = "Greenhouse"

    def __init__(self, board_token: str):
        self.board_token = board_token
        self.company_name = _get_company_name(board_token)
        self.base_url = (
            f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs"
        )

    def fetch_jobs(self, **kwargs) -> List[dict]:
        try:
            response = requests.get(f"{self.base_url}?content=true", timeout=30)
            response.raise_for_status()
            raw_jobs = response.json().get("jobs") or []

            for job in raw_jobs:
                # Flatten nested location dict -> plain string once.
                loc = job.get("location", "")
                job["_location_str"] = (
                    loc.get("name", "") if isinstance(loc, dict) else str(loc)
                )
                # Pre-strip HTML from the description so filter_and_diversify
                # scores clean text instead of HTML tag noise.
                # Fix: previously the raw HTML 'content' field was passed directly
                # to the filter, causing HTML tags (<li>, <p>, etc.) to pollute
                # keyword matching and inflate/deflate scores incorrectly.
                raw_content = job.get("content") or ""
                job["_desc_clean"] = _strip_html(raw_content)

            filtered = filter_and_diversify(
                raw_jobs,
                company=self.company_name,
                title_key="title",
                description_key="_desc_clean",   # use pre-stripped field
                location_key="_location_str",
            )
            return filtered
        except Exception:
            logger.exception(
                "Error fetching Greenhouse jobs for '%s'", self.board_token
            )
            return []

    def normalize(self, raw_job: dict) -> JobPostingCreate:
        # Prefer the pre-stripped clean description; fall back to stripping live.
        description = raw_job.get("_desc_clean") or _strip_html(
            raw_job.get("content") or ""
        )

        location = raw_job.get("_location_str")
        if not location:
            loc_raw = raw_job.get("location", "")
            location = (
                loc_raw.get("name", "") if isinstance(loc_raw, dict) else str(loc_raw)
            )

        job_id = raw_job.get("id")
        apply_url = _greenhouse_apply_url(self.board_token, job_id)

        return JobPostingCreate(
            portal=self.portal_name,
            portal_job_id=str(job_id),
            title=raw_job.get("title") or "Unknown",
            company=self.company_name,
            location=location,
            description=description.strip()[:2000],
            url=apply_url,
            raw_data=raw_job,
        )
