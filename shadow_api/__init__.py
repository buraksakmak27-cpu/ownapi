"""Backwards-compatible alias module re-exporting OwnAPI (`own_api`)."""

from own_api import (
    USER_DELEGATION_DISCLAIMER,
    AutoRepairEngine,
    BaseOwnClient,
    BaseShadowClient,
    ConsentManager,
    HITLQueueManager,
    OwnAPIClient,
    ShadowClient,
    TokenStore,
    __version__,
    onboard_new_site,
)

__all__ = [
    "OwnAPIClient",
    "ShadowClient",
    "BaseOwnClient",
    "BaseShadowClient",
    "AutoRepairEngine",
    "HITLQueueManager",
    "TokenStore",
    "ConsentManager",
    "USER_DELEGATION_DISCLAIMER",
    "onboard_new_site",
    "__version__",
]
