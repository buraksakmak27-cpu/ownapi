"""Shadow-API Registry Module: hepsiburada."""

from .client import send_sensor_data, HepsiburadaClient

__all__ = ["send_sensor_data", "HepsiburadaClient"]
