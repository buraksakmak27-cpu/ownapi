"""OwnAPI Registry Module: coingecko."""

from .client import CoingeckoClient, search_crypto

__all__ = ["search_crypto", "CoingeckoClient"]
