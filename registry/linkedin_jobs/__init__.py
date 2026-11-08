"""OwnAPI Registry Module: linkedin_jobs."""

from .client import LinkedinJobsClient, search_jobs

__all__ = ["search_jobs", "LinkedinJobsClient"]
