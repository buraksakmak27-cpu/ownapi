"""OwnAPI — Enterprise User-Delegated Browser Middleware & Auto-Validated API SDK."""

from core.auto_repair import AutoRepairEngine
from core.base_client import BaseOwnClient, BaseShadowClient
from core.consent import ConsentManager, USER_DELEGATION_DISCLAIMER
from core.hitl_queue import HITLQueueManager
from core.onboarding import onboard_new_site
from core.token_store import TokenStore
from own_api.sdk import OwnAPIClient, ShadowClient

__version__ = "1.0.0"

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
]
