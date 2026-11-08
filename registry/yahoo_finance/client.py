from __future__ import annotations

import json
import pathlib
import sys
import time
from typing import Any
from urllib.parse import quote

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from core.base_client import BaseOwnClient
from core.token_store import TokenStore


class YahooFinanceClient(BaseOwnClient):
    """Yahoo Finance real-time stock quote & chart client via OwnAPI User-Delegated Browser Middleware."""

    def __init__(
        self,
        tenant_id: str = "default",
        token_store: TokenStore | None = None,
        mode: str = "local",
    ) -> None:
        super().__init__(
            service_name="yahoo_finance",
            base_url="https://finance.yahoo.com/",
            tenant_id=tenant_id,
            token_store=token_store,
            mode=mode,
        )

    def get_stock_quote(self, symbol: str = "NVDA") -> dict[str, Any]:
        clean_symbol = quote(str(symbol).strip().upper() or "NVDA")
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{clean_symbol}"
        headers = {
            "accept": "application/json, text/plain, */*",
            "referer": f"https://finance.yahoo.com/quote/{clean_symbol}/",
        }
        params = {
            "interval": "1d",
            "range": "5d",
        }
        raw = self.request(method="GET", url=url, headers=headers, params=params)
        if not isinstance(raw, dict):
            return {"success": False, "symbol": symbol, "raw": raw}

        chart = raw.get("chart", {})
        results = chart.get("result") or []
        if results and isinstance(results[0], dict):
            meta = results[0].get("meta", {})
            price = meta.get("regularMarketPrice")
            prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
            change = round(price - prev_close, 4) if isinstance(price, (int, float)) and isinstance(prev_close, (int, float)) else None
            change_pct = (
                round(((price - prev_close) / prev_close) * 100.0, 2)
                if change is not None and prev_close
                else None
            )
            return {
                "success": True,
                "service": "yahoo_finance",
                "symbol": meta.get("symbol", clean_symbol),
                "short_name": meta.get("shortName") or meta.get("longName") or clean_symbol,
                "currency": meta.get("currency", "USD"),
                "exchange": meta.get("fullExchangeName") or meta.get("exchangeName"),
                "regular_market_price": price,
                "previous_close": prev_close,
                "change": change,
                "change_percent": change_pct,
                "fifty_two_week_high": meta.get("fiftyTwoWeekHigh"),
                "fifty_two_week_low": meta.get("fiftyTwoWeekLow"),
                "regular_market_volume": meta.get("regularMarketVolume"),
                "meta": meta,
            }
        return raw


def get_stock_quote(
    symbol: str = "NVDA",
    tenant_id: str = "default",
    mode: str = "local",
) -> dict[str, Any]:
    """
    Fetches real-time stock/index price, daily change, and 52-week range from Yahoo Finance
    via OwnAPI User-Delegated Browser Middleware.
    """
    client = YahooFinanceClient(tenant_id=tenant_id, mode=mode)
    return client.get_stock_quote(symbol=symbol)


if __name__ == "__main__":
    t0 = time.perf_counter()
    res = get_stock_quote()
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"[OwnAPI - Yahoo Finance] ({elapsed:.1f} ms)")
    print(json.dumps(res, ensure_ascii=False, indent=2)[:800])
