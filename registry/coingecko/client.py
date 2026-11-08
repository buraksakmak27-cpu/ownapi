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


class CoingeckoClient(BaseOwnClient):
    """CoinGecko cryptocurrency market & token search client via OwnAPI User-Delegated Browser Middleware."""

    def __init__(
        self,
        tenant_id: str = "default",
        token_store: TokenStore | None = None,
        mode: str = "local",
    ) -> None:
        super().__init__(
            service_name="coingecko",
            base_url="https://api.coingecko.com/",
            tenant_id=tenant_id,
            token_store=token_store,
            mode=mode,
        )

    def search_crypto(self, query: str = "bitcoin") -> dict[str, Any]:
        url = "https://api.coingecko.com/api/v3/search"
        headers = {
            "accept": "application/json, text/plain, */*",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/94.0.4606.81 Safari/537.36",
        }
        params = {
            "query": str(query).strip() or "bitcoin",
        }
        raw = self.request(method="GET", url=url, headers=headers, params=params)
        if isinstance(raw, dict) and "coins" in raw:
            coins = raw.get("coins") or []
            return {
                "success": True,
                "service": "coingecko",
                "query": query,
                "total_coins_found": len(coins),
                "top_coins": [
                    {
                        "id": c.get("id"),
                        "name": c.get("name"),
                        "symbol": c.get("symbol"),
                        "market_cap_rank": c.get("market_cap_rank"),
                        "thumb": c.get("thumb"),
                    }
                    for c in coins[:10]
                    if isinstance(c, dict)
                ],
                "categories": (raw.get("categories") or [])[:5],
            }
        return raw


def search_crypto(
    query: str = "bitcoin",
    tenant_id: str = "default",
    mode: str = "local",
) -> dict[str, Any]:
    """
    Searches cryptocurrencies, tokens, market cap ranks, and categories on CoinGecko
    via OwnAPI User-Delegated Browser Middleware.
    """
    client = CoingeckoClient(tenant_id=tenant_id, mode=mode)
    return client.search_crypto(query=query)


if __name__ == "__main__":
    t0 = time.perf_counter()
    res = search_crypto()
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"[OwnAPI - CoinGecko] ({elapsed:.1f} ms)")
    print(json.dumps(res, ensure_ascii=False, indent=2)[:800])
