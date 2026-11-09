from __future__ import annotations

import pathlib
import sys
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from core.base_client import BaseOwnClient
from core.token_store import TokenStore


class UcakBiletiClient(BaseOwnClient):
    """Obilet / Uçak Bileti servisi için OwnAPI User-Delegated Browser Middleware + Generic Form Automation istemcisi."""

    def __init__(
        self,
        base_url: str = "https://www.obilet.com",
        tenant_id: str = "default",
        token_store: TokenStore | None = None,
        mode: str = "local",
    ) -> None:
        super().__init__(
            service_name="ucak_bileti",
            base_url=base_url,
            tenant_id=tenant_id,
            token_store=token_store,
            mode=mode,
        )

    def search_flight_offers(
        self,
        origin_id: str = "250_0",
        destination_id: str = "1912_170",
        departure_date: str = "20270707",
        return_date: str = "20270714",
        tenant_id: str | None = None,
        mode: str | None = None,
    ) -> dict[str, Any]:
        """
        İki şehir ve tarih arasında uçak bileti seferlerini ve fiyatlarını
        Generic Form Automation + In-Browser Fetch katmanı üzerinden sorgular.
        """
        url = f"{self.base_url}/json/ucuslar/async/{origin_id}-{destination_id}/{departure_date}-{return_date}/2a-1c-1s/economy"
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "origin": "https://www.obilet.com",
            "referer": f"https://www.obilet.com/ucuslar/{origin_id}-{destination_id}/{departure_date}-{return_date}/2a-1c-1s/economy/all",
        }
        return self.request(
            method="GET",
            url=url,
            headers=headers,
            target_url="https://www.obilet.com/ucak-bileti",
            mode=mode or self.mode,
            form_params={
                "origin": origin_id,
                "destination": destination_id,
                "departure_date": departure_date,
                "return_date": return_date,
            },
        )


def search_flight_offers(
    origin_id: str = "250_0",
    destination_id: str = "1912_170",
    departure_date: str = "20270707",
    return_date: str = "20270714",
    tenant_id: str = "default",
    mode: str = "local",
) -> dict[str, Any]:
    client = UcakBiletiClient(tenant_id=tenant_id, mode=mode)
    return client.search_flight_offers(
        origin_id=origin_id,
        destination_id=destination_id,
        departure_date=departure_date,
        return_date=return_date,
        tenant_id=tenant_id,
        mode=mode,
    )


if __name__ == "__main__":
    client = UcakBiletiClient()
    start_time = time.perf_counter()
    result = client.search_flight_offers()
    end_time = time.perf_counter()
    duration = (end_time - start_time) * 1000
    print(f"Sorgu süresi: {duration:.2f} ms")
    print("Uçuş teklifleri:", result.get("data", {}).get("result", {}) if isinstance(result, dict) else result)
