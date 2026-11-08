from __future__ import annotations

import json
import pathlib
import re
import sys
from html import unescape
from typing import Any
from urllib.parse import quote_plus

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from core.base_client import BaseOwnClient
from core.token_store import TokenStore


def _clean_html_text(raw: str) -> str:
    """HTML etiketlerini ve fazla boşlukları temizler."""
    text = unescape(re.sub(r"<[^>]+>", " ", raw or ""))
    return re.sub(r"\s+", " ", text).strip()


def _parse_linkedin_job_cards(raw_html_or_text: str) -> list[dict[str, Any]]:
    """
    `seeMoreJobPostings/search` veya LinkedIn iş arama yanıtındaki TÜM iş ilanı kartlarını
    (`<li>`, `.base-card`, `.job-search-card`) döngüyle ayıklar.
    """
    jobs: list[dict[str, Any]] = []
    if not raw_html_or_text:
        return jobs

    # Kart bloklarını hem <li>...</li> üzerinden hem de base-card / job-search-card bölümlerinden yakala
    card_blocks = re.findall(r"<li\b[^>]*>(.*?)</li>", raw_html_or_text, flags=re.DOTALL | re.IGNORECASE)
    if len(card_blocks) <= 1 and "base-search-card__title" in raw_html_or_text:
        # <li> etiketi kapanmamış veya farklı sarmalayıcı kullanılmışsa kart başlangıçlarından böl
        parts = re.split(r'(?=<div[^>]+class="[^"]*(?:base-card|job-search-card)[^"]*")', raw_html_or_text, flags=re.IGNORECASE)
        card_blocks = [p for p in parts if "base-search-card__title" in p]

    seen_keys: set[str] = set()
    for block in card_blocks:
        title_m = re.search(
            r'class="[^"]*base-search-card__title[^"]*"[^>]*>(.*?)</h[1-6]>',
            block,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if not title_m:
            continue

        company_m = re.search(
            r'class="[^"]*base-search-card__subtitle[^"]*"[^>]*>(.*?)</h[1-6]>',
            block,
            flags=re.DOTALL | re.IGNORECASE,
        )
        loc_m = re.search(
            r'class="[^"]*job-search-card__location[^"]*"[^>]*>(.*?)</span>',
            block,
            flags=re.DOTALL | re.IGNORECASE,
        )
        link_m = re.search(
            r'href="(https://[^"]*linkedin\.com/jobs/view/[^"?\s]+)',
            block,
            flags=re.IGNORECASE,
        )
        urn_m = re.search(
            r'data-entity-urn="urn:li:jobPosting:(\d+)"',
            block,
            flags=re.IGNORECASE,
        )
        date_m = re.search(r'<time[^>]*datetime="([^"]+)"', block, flags=re.IGNORECASE)

        clean_title = _clean_html_text(title_m.group(1))
        clean_company = _clean_html_text(company_m.group(1)) if company_m else ""
        clean_loc = _clean_html_text(loc_m.group(1)) if loc_m else ""
        job_id = urn_m.group(1).strip() if urn_m else ""
        job_url = link_m.group(1).strip() if link_m else (f"https://www.linkedin.com/jobs/view/{job_id}/" if job_id else "")
        listed_at = date_m.group(1).strip() if date_m else ""

        if not clean_title:
            continue

        dedup_key = job_id or job_url or f"{clean_title}|{clean_company}|{clean_loc}"
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        jobs.append(
            {
                "job_id": job_id,
                "title": clean_title,
                "company": clean_company,
                "location": clean_loc,
                "job_url": job_url,
                "listed_at": listed_at,
            }
        )

    return jobs


class LinkedinJobsClient(BaseOwnClient):
    """LinkedIn Jobs iş ilanı arama ve dinamik sayfalama istemcisi (User-Delegated Browser Middleware)."""

    def __init__(
        self,
        tenant_id: str = "default",
        token_store: TokenStore | None = None,
        mode: str = "local",
    ) -> None:
        super().__init__(
            service_name="linkedin_jobs",
            base_url="https://www.linkedin.com",
            tenant_id=tenant_id,
            token_store=token_store,
            mode=mode,
        )

    def search_jobs(
        self,
        keywords: str = "AI Engineer",
        location: str = "",
        limit: int = 10,
        start: int = 0,
    ) -> dict[str, Any]:
        """
        LinkedIn üzerinde belirtilen `keywords` (örn. 'AI Engineer'), `location` ve `limit`
        değerlerine göre iş ilanlarını sorgular.
        Tek sayfadan dönen ilan sayısı `limit` değerinden azsa (örn. limit=15, sayfa=10),
        `start` parametresini otomatik artırarak (`start=0`, `start=10`...) hedef `limit`
        sayısına ulaşana kadar sayfalama yapar ve tam olarak `jobs[:limit]` döndürür.
        """
        clean_keywords = (keywords or "AI Engineer").strip()
        target_limit = max(1, min(int(limit or 10), 50))
        current_start = max(0, int(start or 0))
        encoded_kw = quote_plus(clean_keywords)
        search_page_url = f"https://www.linkedin.com/jobs/search/?keywords={encoded_kw}"

        headers = {
            "accept": "application/json, text/plain, */*",
            "referer": search_page_url,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }

        collected_jobs: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        pages_fetched = 0
        max_pages = max(1, (target_limit // 10) + 2)
        last_status = 200
        last_raw_text = ""

        while len(collected_jobs) < target_limit and pages_fetched < max_pages:
            params: dict[str, Any] = {
                "keywords": clean_keywords,
                "start": current_start,
            }
            if location and location.strip():
                params["location"] = location.strip()

            res = self.request(
                method="GET",
                url="https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
                headers=headers,
                params=params,
                target_url="https://www.linkedin.com/robots.txt",
            )

            raw_text = ""
            if isinstance(res, dict):
                last_status = int(res.get("status_code", 200))
                raw_text = str(res.get("text") or res.get("response_sample") or "")
            else:
                raw_text = str(res or "")

            last_raw_text = raw_text
            page_jobs = _parse_linkedin_job_cards(raw_text)
            pages_fetched += 1

            if not page_jobs:
                break

            added_in_page = 0
            for job in page_jobs:
                key = job.get("job_id") or job.get("job_url") or f"{job.get('title')}|{job.get('company')}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    collected_jobs.append(job)
                    added_in_page += 1
                    if len(collected_jobs) >= target_limit:
                        break

            # Bu sayfada yeni ilan gelmediyse veya hedef limite ulaşıldıysa döngüyü bitir
            if added_in_page == 0 or len(collected_jobs) >= target_limit:
                break

            current_start += len(page_jobs)

        final_jobs = collected_jobs[:target_limit]

        return {
            "service": "linkedin_jobs",
            "keywords": clean_keywords,
            "location": location or "Global",
            "requested_limit": target_limit,
            "start": int(start),
            "pages_fetched": pages_fetched,
            "total_jobs_returned": len(final_jobs),
            "jobs": final_jobs,
            "search_url": search_page_url,
            "status_code": last_status,
            "response_preview": (
                f"{len(final_jobs)}/{target_limit} iş ilanı ({pages_fetched} sayfa taranarak) başarıyla getirildi."
                if final_jobs
                else last_raw_text[:500]
            ),
        }

    # Geriye dönük uyumluluk
    def get_user_metadata(self, keywords: str = "AI Engineer") -> dict[str, Any]:
        return self.search_jobs(keywords=keywords)


def search_jobs(
    keywords: str = "AI Engineer",
    location: str = "",
    limit: int = 10,
    start: int = 0,
    tenant_id: str = "default",
    mode: str = "local",
) -> dict[str, Any]:
    """
    LinkedIn Jobs üzerinde anahtar kelime (`keywords`), konum (`location`) ve
    istenilen ilan sayısına (`limit`) göre iş ilanlarını arar; gerekirse otomatik sayfalama yapar.
    """
    client = LinkedinJobsClient(tenant_id=tenant_id, mode=mode)
    return client.search_jobs(keywords=keywords, location=location, limit=limit, start=start)


if __name__ == "__main__":
    result = search_jobs(keywords="AI Engineer", limit=5)
    print(json.dumps(result, ensure_ascii=False, indent=2)[:1500])
