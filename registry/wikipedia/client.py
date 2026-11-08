from __future__ import annotations

import pathlib
import sys
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from core.base_client import BaseOwnClient
from core.token_store import TokenStore


class WikipediaClient(BaseOwnClient):
    """Wikipedia REST API için OwnAPI User-Delegated Browser Middleware istemcisi."""

    def __init__(
        self,
        tenant_id: str = "default",
        token_store: TokenStore | None = None,
        mode: str = "local",
    ) -> None:
        super().__init__(
            service_name="wikipedia",
            base_url="https://en.wikipedia.org/",
            tenant_id=tenant_id,
            token_store=token_store,
            mode=mode,
        )

    def search_wikipedia(self, query: str = "peter thiel", limit: int = 10) -> dict[str, Any]:
        url = "https://en.wikipedia.org/w/rest.php/v1/search/title"
        headers = {
            "accept": "application/json",
            "referer": "https://en.wikipedia.org/",
        }
        params = {
            "q": query,
            "limit": limit,
        }
        return self.request(method="GET", url=url, headers=headers, params=params)


def search_wikipedia(
    query: str = "peter thiel",
    limit: int = 10,
    tenant_id: str = "default",
    mode: str = "local",
) -> dict[str, Any]:
    """
    Wikipedia üzerinde kavram ve kişi araması yapar (OwnAPI User-Delegated Browser Middleware).
    """
    client = WikipediaClient(tenant_id=tenant_id, mode=mode)
    return client.search_wikipedia(query=query, limit=limit)


if __name__ == "__main__":
    start_time = time.perf_counter()
    results = search_wikipedia()
    elapsed_time = (time.perf_counter() - start_time) * 1000
    pages = results.get("pages", []) if isinstance(results, dict) else []
    print(f"[OwnAPI] Arama sonuçları: {pages[:2]}... (Toplam {len(pages)} sonuç)")
    print(f"[OwnAPI] Sorgu süresi: {elapsed_time:.2f} ms")
