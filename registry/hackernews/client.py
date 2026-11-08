from __future__ import annotations

import pathlib
import sys
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from core.base_client import BaseOwnClient
from core.token_store import TokenStore


class HackernewsClient(BaseOwnClient):
    """Hacker News (Algolia) arama API'si için OwnAPI User-Delegated Browser Middleware istemcisi."""

    def __init__(
        self,
        tenant_id: str = "default",
        token_store: TokenStore | None = None,
        mode: str = "local",
    ) -> None:
        super().__init__(
            service_name="hackernews",
            base_url="https://hn.algolia.com/",
            tenant_id=tenant_id,
            token_store=token_store,
            mode=mode,
        )

    def search_hacker_news_articles(
        self,
        query: str = "startup",
        page: int = 0,
        hits_per_page: int = 30,
    ) -> dict[str, Any]:
        url = "https://uj5wyc0l7x-dsn.algolia.net/1/indexes/Item_dev/query"
        headers = {
            "accept": "application/json",
            "content-type": "application/x-www-form-urlencoded",
        }
        params = {
            "x-algolia-agent": "Algolia for JavaScript (4.13.1); Browser (lite)",
            "x-algolia-api-key": "28f0e1ec37a5e792e6845e67da5f20dd",
            "x-algolia-application-id": "UJ5WYC0L7X",
        }
        payload = {
            "query": query,
            "analyticsTags": ["web"],
            "page": page,
            "hitsPerPage": hits_per_page,
            "minWordSizefor1Typo": 4,
            "minWordSizefor2Typos": 8,
            "advancedSyntax": True,
            "ignorePlurals": False,
            "clickAnalytics": True,
            "minProximity": 7,
            "numericFilters": [],
            "tagFilters": [["story"], []],
            "typoTolerance": True,
            "queryType": "prefixNone",
            "restrictSearchableAttributes": ["title", "comment_text", "url", "story_text", "author"],
            "getRankingInfo": True,
        }
        return self.request(method="POST", url=url, headers=headers, params=params, json_payload=payload)


def search_hacker_news_articles(
    query: str = "startup",
    page: int = 0,
    hits_per_page: int = 30,
    tenant_id: str = "default",
    mode: str = "local",
) -> dict[str, Any]:
    """
    Hacker News üzerinde makale araması yapar (OwnAPI User-Delegated Browser Middleware).
    """
    client = HackernewsClient(tenant_id=tenant_id, mode=mode)
    return client.search_hacker_news_articles(query=query, page=page, hits_per_page=hits_per_page)


if __name__ == "__main__":
    start_time = time.perf_counter()
    results = search_hacker_news_articles()
    duration = (time.perf_counter() - start_time) * 1000
    print(f"[OwnAPI] Arama {duration:.2f} ms sürdü.")
    print(f"[OwnAPI] Toplam bulunan kayıt sayısı: {results.get('nbHits', 'Bilinmiyor')}")
    for hit in results.get("hits", [])[:3]:
        print(f"Başlık: {hit.get('title')}, URL: {hit.get('url')}")
