from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from generated_client import search_flights as _shadow_client_fn

mcp = FastMCP("Shadow-API MCP Server")


@mcp.tool()
def search_flights(departure_date: str = '02-03-2027', return_date: str = '09-03-2027', origin_airport_code: str = 'IST', destination_airport_code: str = 'ESB') -> dict[str, Any]:
    """
    Kullanıcının hedefi İstanbul'dan Ankara'ya uçuş seferlerini ve fiyatlarını sorgulamak olduğundan, uçuş müsaitlik ve fiyat bilgilerini içeren '/api/v1/availability' uç noktası en uygun seçimdir. Bu uç nokta, uçuş bilgilerini almak için gerekli olan tüm parametreleri içermektedir.
    """
    return _shadow_client_fn(departure_date=departure_date, return_date=return_date, origin_airport_code=origin_airport_code, destination_airport_code=destination_airport_code)


if __name__ == "__main__":
    mcp.run()

