"""OwnAPI Registry Module: thy."""

from .client import THYClient, ThyClient, search_flights

__all__ = ["search_flights", "ThyClient", "THYClient"]
