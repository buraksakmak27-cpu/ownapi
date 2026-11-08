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


class GithubTrendingClient(BaseOwnClient):
    """GitHub Trending & Popular Repository Search client via OwnAPI User-Delegated Browser Middleware."""

    def __init__(
        self,
        tenant_id: str = "default",
        token_store: TokenStore | None = None,
        mode: str = "local",
    ) -> None:
        super().__init__(
            service_name="github_trending",
            base_url="https://github.com/",
            tenant_id=tenant_id,
            token_store=token_store,
            mode=mode,
        )

    def search_repositories(self, query: str = "mcp server", limit: int = 5) -> dict[str, Any]:
        url = "https://api.github.com/search/repositories"
        headers = {
            "accept": "application/vnd.github+json",
            "referer": "https://github.com/search",
        }
        params = {
            "q": str(query).strip() or "mcp server",
            "sort": "stars",
            "order": "desc",
            "per_page": max(1, min(int(limit), 30)),
        }
        raw = self.request(method="GET", url=url, headers=headers, params=params)
        if isinstance(raw, dict) and "items" in raw:
            items = raw.get("items") or []
            return {
                "success": True,
                "service": "github_trending",
                "query": query,
                "total_count": raw.get("total_count", len(items)),
                "returned_count": len(items),
                "repositories": [
                    {
                        "full_name": repo.get("full_name"),
                        "description": repo.get("description"),
                        "stars": repo.get("stargazers_count"),
                        "forks": repo.get("forks_count"),
                        "language": repo.get("language"),
                        "url": repo.get("html_url"),
                        "topics": (repo.get("topics") or [])[:6],
                    }
                    for repo in items
                    if isinstance(repo, dict)
                ],
            }
        return raw


def search_repositories(
    query: str = "mcp server",
    limit: int = 5,
    tenant_id: str = "default",
    mode: str = "local",
) -> dict[str, Any]:
    """
    Searches popular and trending open-source repositories on GitHub sorted by stars
    via OwnAPI User-Delegated Browser Middleware.
    """
    client = GithubTrendingClient(tenant_id=tenant_id, mode=mode)
    return client.search_repositories(query=query, limit=limit)


if __name__ == "__main__":
    t0 = time.perf_counter()
    res = search_repositories()
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"[OwnAPI - GitHub Trending] ({elapsed:.1f} ms)")
    print(json.dumps(res, ensure_ascii=False, indent=2)[:800])
