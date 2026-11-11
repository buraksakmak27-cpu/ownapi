"""OwnAPI — Enterprise User-Delegated Browser Middleware & Auto-Validated API SDK."""
from __future__ import annotations

import pathlib
from typing import Any

from core.api_keys import (
    APIKeyManager,
    InsufficientCreditsError,
    consume_credit,
    generate_api_key,
    get_or_create_tenant,
    get_tenant_info,
    upgrade_plan,
)
from core.auto_repair import AutoRepairEngine
from core.base_client import BaseOwnClient, BaseShadowClient
from core.consent import USER_DELEGATION_DISCLAIMER, ConsentManager
from core.hitl_queue import HITLQueueManager
from core.onboarding import onboard_new_site
from core.token_store import TokenStore
from ownapi.sdk import OwnAPI, OwnAPIClient, ServiceNamespaceProxy, ShadowClient

__version__ = "1.0.0"

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
_REGISTRY_ROOT = _PROJECT_ROOT / "registry"

# Doğrudan `from ownapi import thy, trendyol, pegasus` kullanımı için yerel (free/default) vekiller
thy = ServiceNamespaceProxy("thy", default_tenant_id="default", default_mode="local")
trendyol = ServiceNamespaceProxy("trendyol", default_tenant_id="default", default_mode="local")
pegasus = ServiceNamespaceProxy("pegasus", default_tenant_id="default", default_mode="local")
enuygun = ServiceNamespaceProxy("enuygun", default_tenant_id="default", default_mode="local")
linkedin_jobs = ServiceNamespaceProxy("linkedin_jobs", default_tenant_id="default", default_mode="local")
wikipedia = ServiceNamespaceProxy("wikipedia", default_tenant_id="default", default_mode="local")
hackernews = ServiceNamespaceProxy("hackernews", default_tenant_id="default", default_mode="local")
github_trending = ServiceNamespaceProxy("github_trending", default_tenant_id="default", default_mode="local")
coingecko = ServiceNamespaceProxy("coingecko", default_tenant_id="default", default_mode="local")
yahoo_finance = ServiceNamespaceProxy("yahoo_finance", default_tenant_id="default", default_mode="local")
edevlet = ServiceNamespaceProxy("edevlet", default_tenant_id="default", default_mode="local")
hepsiburada = ServiceNamespaceProxy("hepsiburada", default_tenant_id="default", default_mode="local")
ucak_bileti = ServiceNamespaceProxy("ucak_bileti", default_tenant_id="default", default_mode="local")

_DYNAMIC_PROXIES: dict[str, ServiceNamespaceProxy] = {}


def __getattr__(name: str) -> Any:
    """Yeni eklenen `registry/<site>` servislerinin de `from ownapi import <site>` ile içe aktarılmasını sağlar."""
    if name.startswith("_"):
        raise AttributeError(name)
    clean = name.strip().lower()
    if clean in _DYNAMIC_PROXIES:
        return _DYNAMIC_PROXIES[clean]
    if (_REGISTRY_ROOT / clean / "client.py").exists():
        proxy = ServiceNamespaceProxy(clean, default_tenant_id="default", default_mode="local")
        _DYNAMIC_PROXIES[clean] = proxy
        return proxy
    raise AttributeError(f"module 'ownapi' has no attribute '{name}'")


__all__ = [
    "OwnAPI",
    "OwnAPIClient",
    "ShadowClient",
    "ServiceNamespaceProxy",
    "BaseOwnClient",
    "BaseShadowClient",
    "APIKeyManager",
    "InsufficientCreditsError",
    "get_or_create_tenant",
    "generate_api_key",
    "consume_credit",
    "upgrade_plan",
    "get_tenant_info",
    "AutoRepairEngine",
    "HITLQueueManager",
    "TokenStore",
    "ConsentManager",
    "USER_DELEGATION_DISCLAIMER",
    "onboard_new_site",
    "thy",
    "trendyol",
    "pegasus",
    "enuygun",
    "linkedin_jobs",
    "wikipedia",
    "hackernews",
    "github_trending",
    "coingecko",
    "yahoo_finance",
    "edevlet",
    "hepsiburada",
    "ucak_bileti",
]
