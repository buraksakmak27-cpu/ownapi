from __future__ import annotations

import logging
import os
from abc import ABC
from typing import Any
from urllib.parse import urljoin

from core.consent import ConsentManager
from core.token_store import TokenStore

logger = logging.getLogger("own_api.base_client")


class BaseOwnClient(ABC):
    """
    OwnAPI User-Delegated Browser Middleware Taban Sınıfı (Plaid-style Browser Automation).
    Tüm OwnAPI servis istemcilerinin (THY, Trendyol, Pegasus, Enuygun, Wikipedia, HackerNews vb.)
    miras aldığı kurumsal sınıf:
    - Çok kiracılı (multi-tenant) oturum ve çerez izolasyonu (`TokenStore` -> `.sessions/{tenant_id}/{service}_tokens.json`).
    - ToS / Kullanıcı Delegasyon Onay Kaydı (`ConsentManager` -> `.sessions/{tenant_id}/consent_audit.json`).
    - 2 Modlu Çalışma Altyapısı:
      * `mode="local"` (varsayılan): Kullanıcının kendi yerel Chrome oturumu ve IP'si üzerinden çalışır.
      * `mode="cloud"`: Bulut tabanlı Docker / Remote Browser (CDP / Browserless) üzerinden çalışır.
    """

    def __init__(
        self,
        service_name: str,
        base_url: str,
        tenant_id: str = "default",
        token_store: TokenStore | None = None,
        consent_manager: ConsentManager | None = None,
        mode: str = "local",
        cloud_cdp_url: str | None = None,
        non_interactive: bool = False,
        **kwargs: Any,
    ) -> None:
        self.service_name = service_name
        self.base_url = base_url
        self.tenant_id = tenant_id or "default"
        self.mode = (mode or os.getenv("OWNAPI_BROWSER_MODE", "local")).strip().lower()
        if self.mode not in ("local", "cloud"):
            self.mode = "local"
        self.cloud_cdp_url = cloud_cdp_url or os.getenv("OWNAPI_CLOUD_CDP_URL", "").strip() or None
        self.non_interactive = bool(
            non_interactive
            or os.getenv("OWNAPI_NON_INTERACTIVE", "").strip().lower() in ("1", "true", "yes")
        )
        self.token_store = token_store or TokenStore()
        self.consent_manager = consent_manager or ConsentManager()

        from healer import SessionHealer

        self.healer = SessionHealer(
            target_url=base_url,
            tenant_id=self.tenant_id,
            service=self.service_name,
            token_store=self.token_store,
            mode=self.mode,
            cloud_cdp_url=self.cloud_cdp_url,
            non_interactive=self.non_interactive,
        )

    def get_stored_tokens(self) -> dict[str, Any]:
        """Kiracıya ve servise özel kayıtlı oturum çerezlerini TokenStore'dan çeker."""
        return self.token_store.get_session(self.tenant_id, self.service_name)

    def save_tokens(self, cookies: Any, user_agent: str = "", metadata: dict[str, Any] | None = None) -> None:
        """Taze çerez ve başlıkları TokenStore'a (.sessions/{tenant_id}/{service}_tokens.json) işler."""
        self.token_store.save_session(
            tenant_id=self.tenant_id,
            service=self.service_name,
            cookies=cookies,
            user_agent=user_agent,
            metadata=metadata,
        )

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_payload: Any = None,
        data: Any = None,
        target_url: str | None = None,
        mode: str | None = None,
        non_interactive: bool | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        İsteği ilk çağrıda ToS / Kullanıcı Delegasyon Onay kaydını doğrulayarak
        kullanıcının gerçek tarayıcı oturumu (In-Browser Fetch) üzerinden yürütür.
        TokenStore'daki izole çerezler tarayıcıya otomatik enjekte edilir ve işlem sonunda güncellenir.
        """
        active_mode = (mode or self.mode or "local").strip().lower()
        if active_mode not in ("local", "cloud"):
            active_mode = "local"
        env_ni = os.getenv("OWNAPI_NON_INTERACTIVE", "").strip().lower() in ("1", "true", "yes")
        active_non_interactive = (
            bool(non_interactive or env_ni)
            if non_interactive is not None
            else bool(self.non_interactive or env_ni)
        )

        # İlk çağrıda kullanıcının kendi oturumuyla bağlandığını onaylayan ToS / Delegation kaydını tut
        self.consent_manager.ensure_consent(
            tenant_id=self.tenant_id,
            service=self.service_name,
            action="first_invocation",
            mode=active_mode,
        )

        req_headers = dict(headers or {})
        full_url = url if url.startswith("http") else urljoin(self.base_url, url)
        method_upper = method.upper().strip()

        # TokenStore'dan mevcut çerezleri ve User-Agent bilgisini başlıklara hazırla
        tokens = self.get_stored_tokens()
        if tokens:
            stored_cookie = tokens.get("cookie")
            stored_ua = tokens.get("user-agent")
            if stored_cookie and "cookie" not in {k.lower() for k in req_headers}:
                req_headers["cookie"] = stored_cookie
            if stored_ua and "user-agent" not in {k.lower() for k in req_headers}:
                req_headers["user-agent"] = stored_ua

        payload = json_payload if json_payload is not None else data

        from healer import ResponseDict

        res = self.healer.execute_in_browser(
            method=method_upper,
            url=full_url,
            headers=req_headers,
            params=params,
            json_payload=payload,
            target_url=target_url or self.base_url,
            tenant_id=self.tenant_id,
            mode=active_mode,
            non_interactive=active_non_interactive,
        )
        if isinstance(res, dict) and not isinstance(res, ResponseDict):
            return ResponseDict(res)
        return res


# Geriye dönük uyumluluk takma adı
BaseShadowClient = BaseOwnClient
