"""Backwards-compatible alias module re-exporting OwnAPIClient from `own_api.sdk`."""

from own_api.sdk import (
    BaseOwnClient,
    BaseShadowClient,
    ConsentManager,
    OwnAPIClient,
    ShadowClient,
    TokenStore,
    onboard_new_site,
)

__all__ = [
    "OwnAPIClient",
    "ShadowClient",
    "BaseOwnClient",
    "BaseShadowClient",
    "TokenStore",
    "ConsentManager",
    "onboard_new_site",
]
