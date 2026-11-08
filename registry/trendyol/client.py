from __future__ import annotations

import json
import logging
import pathlib
import sys
import time
from typing import Any
from urllib.parse import quote_plus, urlencode

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from core.base_client import BaseOwnClient
from core.token_store import TokenStore

logger = logging.getLogger("own_api.trendyol")


class TrendyolClient(BaseOwnClient):
    """Trendyol e-ticaret servisi için OwnAPI User-Delegated Browser Middleware istemcisi."""

    def __init__(
        self,
        tenant_id: str = "default",
        token_store: TokenStore | None = None,
        mode: str = "local",
    ) -> None:
        super().__init__(
            service_name="trendyol",
            base_url="https://www.trendyol.com",
            tenant_id=tenant_id,
            token_store=token_store,
            mode=mode,
        )

    def search(
        self,
        query: str = "laptop",
        max_results: int = 10,
    ) -> dict[str, Any]:
        """Trendyol üzerinde ürün araması yapar ve yapılandırılmış ürün listesini döndürür."""
        clean_query = str(query).strip()
        encoded_query = quote_plus(clean_query)
        search_path = f"/sr?q={encoded_query}"
        full_url = f"{self.base_url}{search_path}"

        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "accept-language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
            "referer": "https://www.trendyol.com/",
        }

        # İsteği doğrudan BaseShadowClient (In-Browser Fetch) üzerinden gönder
        raw = self.request(
            method="GET",
            url=full_url,
            headers=headers,
        )

        # Yanıt HTML metnini al
        html_text = ""
        if isinstance(raw, dict):
            html_text = raw.get("text", "")
            if not html_text and "error" in raw and not raw.get("success", True):
                return raw
        elif isinstance(raw, str):
            html_text = raw

        parsed = self._extract_products_from_html(html_text, clean_query, max_results)
        return parsed

    @staticmethod
    def _extract_products_from_html(html_text: str, query: str, max_results: int) -> dict[str, Any]:
        """Trendyol SSR HTML içindeki __single-search-result__PROPS JSON bloğunu ayrıştırır."""
        prefix = 'window["__single-search-result__PROPS"]='
        idx = html_text.find(prefix)
        if idx == -1:
            prefix_alt = 'window.__SEARCH_APP_INITIAL_STATE__='
            idx = html_text.find(prefix_alt)
            prefix = prefix_alt

        raw_products: list[dict[str, Any]] = []
        if idx != -1:
            sub = html_text[idx + len(prefix):]
            end_script = sub.find("</script>")
            if end_script != -1:
                raw_json = sub[:end_script].rstrip(";").strip()
                try:
                    data = json.loads(raw_json)
                    if isinstance(data, dict):
                        if "data" in data and isinstance(data["data"], dict):
                            raw_products = data["data"].get("products", [])
                        elif "products" in data:
                            raw_products = data["products"]
                except Exception as e:
                    logger.warning(f"Trendyol JSON parse hatası: {e}")

        formatted_products: list[dict[str, Any]] = []
        for p in raw_products[:max_results]:
            if not isinstance(p, dict):
                continue

            # Fiyat ayıklama
            p_price = p.get("price")
            discounted_val = None
            original_val = None
            currency = "TL"

            if isinstance(p_price, dict):
                discounted_val = p_price.get("discountedPrice") or p_price.get("current")
                if isinstance(discounted_val, dict):
                    discounted_val = discounted_val.get("value")
                original_val = p_price.get("originalPrice") or p_price.get("old")
                if isinstance(original_val, dict):
                    original_val = original_val.get("value")
                currency = p_price.get("currency") or p_price.get("currencySymbol") or "TL"
            elif isinstance(p_price, (int, float)):
                discounted_val = float(p_price)

            # Marka ayıklama
            brand = p.get("brand")
            brand_name = brand.get("name", "") if isinstance(brand, dict) else str(brand or "")

            # Puan ayıklama
            rating_score = p.get("ratingScore")
            avg_rating = None
            total_reviews = None
            if isinstance(rating_score, dict):
                avg_rating = rating_score.get("averageRating")
                total_reviews = rating_score.get("totalCount")

            # URL
            rel_url = p.get("url", "")
            full_prod_url = f"https://www.trendyol.com{rel_url}" if rel_url.startswith("/") else rel_url

            formatted_products.append({
                "id": p.get("id"),
                "name": p.get("name", ""),
                "brand": brand_name,
                "price": discounted_val,
                "original_price": original_val,
                "currency": currency,
                "rating": round(avg_rating, 2) if isinstance(avg_rating, (int, float)) else None,
                "review_count": total_reviews,
                "url": full_prod_url,
            })

        return {
            "success": True,
            "service": "trendyol",
            "query": query,
            "total_found": len(raw_products),
            "returned_count": len(formatted_products),
            "products": formatted_products,
        }


def search_products(
    query: str = "laptop",
    max_results: int = 10,
    tenant_id: str = "default",
    mode: str = "local",
) -> dict[str, Any]:
    """
    Trendyol üzerinde ürün arama ve fiyat karşılaştırması yapar.
    query: Aranacak kelime (örn: 'laptop', 'iphone 15', 'kablosuz kulaklık')
    max_results: Döndürülecek maksimum ürün sayısı (varsayılan: 10)
    tenant_id: Oturum izolasyonu için kiracı ID'si (varsayılan: 'default')
    mode: Çalışma modu ('local' yerel Chrome veya 'cloud' Docker/Remote Browser)
    """
    client = TrendyolClient(tenant_id=tenant_id, mode=mode)
    return client.search(query=query, max_results=max_results)


def print_product_summary(data: dict[str, Any], elapsed_ms: float = 0.0) -> None:
    """Ayrıştırılmış ürün sonuçlarını terminalde tablo gibi temiz bir formatta yazdırır."""
    products = data.get("products", [])
    query = data.get("query", "")
    total = data.get("total_found", 0)

    print("\n" + "=" * 80)
    print(f"🛍️  Trendyol Arama Sonuçları: '{query}' | Bulunan: {total} | Gösterilen: {len(products)}")
    if elapsed_ms > 0:
        print(f"⏱️  Yanıt Süresi: {elapsed_ms:.2f} ms")
    print("=" * 80)

    if not products:
        print("ℹ️ Eşleşen ürün bulunamadı.")
        return

    for idx, p in enumerate(products, 1):
        name = p.get("name", "")
        brand = p.get("brand", "")
        price = p.get("price")
        orig_price = p.get("original_price")
        curr = p.get("currency", "TL")
        rating = p.get("rating")
        reviews = p.get("review_count")

        price_str = f"{price:,.2f} {curr}" if isinstance(price, (int, float)) else "Belirtilmemiş"
        if isinstance(orig_price, (int, float)) and orig_price > (price or 0):
            price_str += f" (Eski: {orig_price:,.2f} {curr})"

        rating_str = f"⭐ {rating}" if rating else ""
        if reviews:
            rating_str += f" ({reviews} değerlendirme)"

        print(f"\n{idx:2d}. [{brand}] {name[:65]}")
        print(f"    Fiyat: {price_str}  {rating_str}")
        print(f"    Link : {p.get('url')}")

    print("\n" + "=" * 80 + "\n")


if __name__ == "__main__":
    t0 = time.perf_counter()
    query_arg = sys.argv[1] if len(sys.argv) > 1 else "laptop"
    res = search_products(query=query_arg, max_results=5)
    t_elapsed = (time.perf_counter() - t0) * 1000
    print_product_summary(res, elapsed_ms=t_elapsed)
