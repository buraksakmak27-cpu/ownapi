"""OwnAPI Registry Module: yahoo_finance."""

from .client import YahooFinanceClient, get_stock_quote

__all__ = ["get_stock_quote", "YahooFinanceClient"]
