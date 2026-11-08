"""OwnAPI Registry Module: github_trending."""

from .client import GithubTrendingClient, search_repositories

__all__ = ["search_repositories", "GithubTrendingClient"]
