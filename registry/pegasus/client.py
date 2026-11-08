from __future__ import annotations

import json
import pathlib
import sys
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from core.base_client import BaseOwnClient
from core.token_store import TokenStore
from healer import update_payload_deep


class PegasusClient(BaseOwnClient):
    """Pegasus Hava Yolları (flypgs.com) için OwnAPI User-Delegated Browser Middleware istemcisi."""

    def __init__(
        self,
        tenant_id: str = "default",
        token_store: TokenStore | None = None,
        mode: str = "local",
    ) -> None:
        super().__init__(
            service_name="pegasus",
            base_url="https://web.flypgs.com/booking",
            tenant_id=tenant_id,
            token_store=token_store,
            mode=mode,
        )

    def get_flight_availability(
        self,
        departure_port: str = "SAW",
        arrival_port: str = "ESB",
        departure_date: str = "2027-03-03",
        return_date: str = "2027-03-08",
        adult_count: int = 1,
        child_count: int = 0,
        infant_count: int = 0,
        currency: str = "TRY",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        headers: dict[str, str] = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://web.flypgs.com",
            "referer": (
                f"https://web.flypgs.com/booking?language=tr&adultCount={adult_count}"
                f"&arrivalPort={arrival_port}&departurePort={departure_port}&currency={currency}"
                f"&dateOption=1&departureDate={departure_date}&returnDate={return_date}"
            ),
        }
        payload: Any = {
            "flightSearchList": [
                {
                    "departurePort": departure_port,
                    "arrivalPort": arrival_port,
                    "departureDate": departure_date,
                    "returnDate": return_date,
                }
            ],
            "adultCount": adult_count,
            "childCount": child_count,
            "infantCount": infant_count,
            "currency": currency,
            "dateOption": 1,
            "ffRedemption": False,
            "totalPoints": None,
            "personnelFlightSearch": False,
            "expatPnr": False,
            "operationCode": "TK",
            "affiliate": {"id": None},
            "bookingType": "BOOKING",
        }
        payload = update_payload_deep(
            payload,
            {
                "departurePort": departure_port,
                "arrivalPort": arrival_port,
                "departureDate": departure_date,
                "returnDate": return_date,
                "adultCount": adult_count,
                "childCount": child_count,
                "infantCount": infant_count,
                "currency": currency,
            },
        )

        return self.request(
            method="POST",
            url="https://web.flypgs.com/pegasus/availability",
            headers=headers,
            params=params,
            json_payload=payload,
        )


def get_flight_availability(
    departure_port: str = "SAW",
    arrival_port: str = "ESB",
    departure_date: str = "2027-03-03",
    return_date: str = "2027-03-08",
    adult_count: int = 1,
    child_count: int = 0,
    infant_count: int = 0,
    currency: str = "TRY",
    tenant_id: str = "default",
    mode: str = "local",
) -> dict[str, Any]:
    """
    Pegasus Hava Yolları uçuş seferlerini ve fiyatlarını kullanıcının gerçek tarayıcı oturumu üzerinden sorgular.
    Hedef Endpoint: POST https://web.flypgs.com/pegasus/availability
    """
    client = PegasusClient(tenant_id=tenant_id, mode=mode)
    return client.get_flight_availability(
        departure_port=departure_port,
        arrival_port=arrival_port,
        departure_date=departure_date,
        return_date=return_date,
        adult_count=adult_count,
        child_count=child_count,
        infant_count=infant_count,
        currency=currency,
    )


if __name__ == "__main__":
    start_ns = time.perf_counter()
    data = get_flight_availability()
    elapsed_ms = (time.perf_counter() - start_ns) * 1000
    print(f"[OwnAPI] Yanıt Süresi: {elapsed_ms:.2f} ms")
    print(json.dumps(data, ensure_ascii=False, indent=2)[:800])
