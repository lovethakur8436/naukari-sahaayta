import html
import logging
import re
from typing import List

import requests

from app.ingestors.base import BaseIngestor
from app.ingestors.filter import filter_and_diversify
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
            timeout=10
        )
        if resp.status_code == 200:
            return resp.json().get("name", board_token.title())
    except Exception:
        pass
    return board_token.title()


def _greenhouse_apply_url(board_token: str, job_id) -> str:
    """
    Build the canonical Greenhouse apply URL using the board token + job ID.

    WHY: The `absolute_url` returned by the Greenhouse API often points to a
    company's CUSTOM careers page (e.g. stripe.com/jobs/search?gh_jid=XXX).
    When a job closes, those custom pages redirect to a listing/search page,
    which the apply agent mistakenly treats as a dead-job redirect.

    The boards.greenhouse.io URL is always stable, goes directly to the apply
    form, and does NOT redirect when the job is still open.

    Format: https://boards.greenhouse.io/{board_token}/jobs/{job_id}
    Example: https://boards.greenhouse.io/stripe/jobs/7292520
    """
    return f"https://boards.greenhouse.io/{board_token}/jobs/{job_id}"


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

            # Flatten nested location dict into a plain string once, here.
            # normalize() reads this same key so both paths stay in sync.
            for job in raw_jobs:
                loc = job.get("location", "")
                job["_location_str"] = loc.get("name", "") if isinstance(loc, dict) else str(loc)

            filtered = filter_and_diversify(
                raw_jobs,
                company=self.company_name,
                title_key="title",
                description_key="content",
                location_key="_location_str",
            )
            return filtered
        except Exception:
            logger.exception("Error fetching Greenhouse jobs for '%s'", self.board_token)
            return []

    def normalize(self, raw_job: dict) -> JobPostingCreate:
        description = raw_job.get("content", "")
        clean_desc = re.sub('<[^<]+?>', '', html.unescape(description))

        # Prefer the pre-flattened string injected by fetch_jobs().
        # Fall back to re-deriving from the raw location field so that
        # normalize() remains safe even when called in isolation.
        location = raw_job.get("_location_str")
        if not location:
            loc_raw = raw_job.get("location", "")
            location = loc_raw.get("name", "") if isinstance(loc_raw, dict) else str(loc_raw)

        job_id = raw_job.get("id")

        # Always use the canonical boards.greenhouse.io apply URL.
        # Avoids false "dead job" detections caused by company custom career
        # pages (e.g. stripe.com/jobs/search?gh_jid=...) redirecting on close.
        apply_url = _greenhouse_apply_url(self.board_token, job_id)

        return JobPostingCreate(
            portal=self.portal_name,
            portal_job_id=str(job_id),
            title=raw_job.get("title", "Unknown"),
            company=self.company_name,
            location=location,
            description=clean_desc.strip()[:2000],
            url=apply_url,
            raw_data=raw_job
        )
