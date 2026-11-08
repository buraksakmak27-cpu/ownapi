from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from core.base_client import BaseOwnClient
from core.token_store import TokenStore


class EdevletClient(BaseOwnClient):
    """e-Devlet Kapısı (turkiye.gov.tr) User-Delegated Browser Middleware istemcisi."""

    def __init__(self, tenant_id: str = "default", token_store: TokenStore | None = None, mode: str = "local"):
        super().__init__(
            service_name="edevlet",
            base_url="https://www.turkiye.gov.tr",
            tenant_id=tenant_id,
            token_store=token_store,
            mode=mode,
        )

    def get_homepage_content(self) -> dict[str, Any]:
        headers = {
            "accept": "application/json, text/plain, */*",
        }
        res = self.request(method="GET", url="/", headers=headers)
        if isinstance(res, dict):
            text_val = res.get("text") or res.get("response_sample") or json.dumps(res, ensure_ascii=False)
            return {
                "service": "edevlet",
                "url": "https://www.turkiye.gov.tr/",
                "status_code": res.get("status_code", 200),
                "ok": res.get("ok", True),
                "content_preview": str(text_val)[:1500],
            }
        text_val = getattr(res, "text", str(res))
        return {
            "service": "edevlet",
            "url": "https://www.turkiye.gov.tr/",
            "status_code": 200,
            "ok": True,
            "content_preview": str(text_val)[:1500],
        }


def get_homepage_content(tenant_id: str = "default", mode: str = "local") -> dict[str, Any]:
    """e-Devlet ana sayfasının içeriğini kullanıcının gerçek tarayıcı oturumu üzerinden alır."""
    client = EdevletClient(tenant_id=tenant_id, mode=mode)
    return client.get_homepage_content()
