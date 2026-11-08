"""OwnAPI Registry Module: wikipedia."""

from .client import WikipediaClient, search_wikipedia

__all__ = ["search_wikipedia", "WikipediaClient"]
