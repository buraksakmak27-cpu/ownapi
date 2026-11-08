"""OwnAPI Registry Module: enuygun."""

from .client import EnuygunClient, get_popular_flights

__all__ = ["get_popular_flights", "EnuygunClient"]
