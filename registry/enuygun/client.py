from __future__ import annotations

import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from core.base_client import BaseOwnClient
from core.token_store import TokenStore


class EnuygunClient(BaseOwnClient):
    """Enuygun.com için OwnAPI User-Delegated Browser Middleware istemcisi."""

    def __init__(
        self,
        tenant_id: str = "default",
        token_store: TokenStore | None = None,
        mode: str = "local",
    ) -> None:
        super().__init__(
            service_name="enuygun",
            base_url="https://www.enuygun.com/ucak-bileti/",
            tenant_id=tenant_id,
            token_store=token_store,
            mode=mode,
        )

    def get_popular_flights(
        self,
        referer: str = "https://www.enuygun.com/ucak-bileti",
        origin: str | None = None,
        destination: str | None = None,
        departure_date: str | None = None,
        return_date: str | None = None,
    ) -> dict[str, Any]:
        url = "https://www.enuygun.com/napi/v1/flights/populars"
        headers = {
            "referer": "https://www.enuygun.com/ucak-bileti/",
            "accept": "application/json, text/plain, */*",
        }
        params = {"referer": referer}
        form_params = {
            k: v
            for k, v in {
                "origin": origin,
                "destination": destination,
                "departure_date": departure_date,
                "return_date": return_date,
            }.items()
            if v
        }
        return self.request(
            method="GET",
            url=url,
            headers=headers,
            params=params,
            json_payload=None,
            form_params=form_params or None,
        )


def get_popular_flights(
    referer: str = "https://www.enuygun.com/ucak-bileti",
    tenant_id: str = "default",
    mode: str = "local",
) -> dict[str, Any]:
    """Enuygun popüler uçuşlarını kullanıcının gerçek tarayıcı oturumu üzerinden getirir."""
    client = EnuygunClient(tenant_id=tenant_id, mode=mode)
    return client.get_popular_flights(referer=referer)


if __name__ == "__main__":
    import json
    res = get_popular_flights()
    print(json.dumps(res, ensure_ascii=False, indent=2)[:800])
