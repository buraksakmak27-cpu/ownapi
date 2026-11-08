"""OwnAPI Registry Module: pegasus."""

from .client import PegasusClient, get_flight_availability

__all__ = ["get_flight_availability", "PegasusClient"]
