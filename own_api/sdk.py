from __future__ import annotations

import inspect
import os
import pathlib
import sys
from typing import Any

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.auto_repair import AutoRepairEngine
from core.base_client import BaseOwnClient, BaseShadowClient
from core.consent import ConsentManager
from core.hitl_queue import HITLQueueManager
from core.onboarding import onboard_new_site
from core.token_store import TokenStore
from master_mcp import load_registry_tools


class OwnAPIClient:
    """
    OwnAPI Birleşik Kurumsal Python SDK Arayüzü (User-Delegated Browser Middleware).
    Geliştiricilerin tüm kayıtlı servisleri (THY, Trendyol, Pegasus, Enuygun, Wikipedia, HackerNews vb.)
    tek satırla listelemesini, çağırmasını, yeni siteler eklemesini (onboarding + 3x auto-test),
    Self-Healing 2.0 toplu bakımını, HITL asenkron onay kuyruğunu ve LLM ajanını yönetmesini sağlar.

    2 Modlu Çalışma Altyapısı:
    - mode="local" (varsayılan): Kullanıcının kendi yerel Chrome tarayıcısı ve IP'si üzerinden çalışır.
    - mode="cloud": Bulut tabanlı Docker / Remote Browser (CDP / Browserless) üzerinden çalışır.
    """

    def __init__(
        self,
        tenant_id: str = "default",
        mode: str = "local",
        cloud_cdp_url: str | None = None,
        non_interactive: bool = False,
        registry_root: pathlib.Path | str = PROJECT_ROOT / "registry",
        **kwargs: Any,
    ) -> None:
        self.tenant_id = tenant_id or "default"
        self.mode = (mode or os.getenv("OWNAPI_BROWSER_MODE", "local")).strip().lower()
        if self.mode not in ("local", "cloud"):
            self.mode = "local"
        self.cloud_cdp_url = cloud_cdp_url or os.getenv("OWNAPI_CLOUD_CDP_URL", "").strip() or None
        self.non_interactive = bool(
            non_interactive
            or os.getenv("OWNAPI_NON_INTERACTIVE", "").strip().lower() in ("1", "true", "yes")
        )
        self.registry_root = pathlib.Path(registry_root)
        self.token_store = TokenStore()
        self.consent_manager = ConsentManager()
        self.hitl_queue = HITLQueueManager()
        self.auto_repair = AutoRepairEngine(
            registry_root=self.registry_root,
            tenant_id=self.tenant_id,
            mode=self.mode,
        )
        self._agent = None

    def list_services(self) -> list[dict[str, Any]]:
        """Kayıtlı tüm servislerin şemalarını, fonksiyon adlarını ve parametrelerini listeler."""
        tools = load_registry_tools(self.registry_root)
        result: list[dict[str, Any]] = []
        for schema, fn in tools:
            result.append({
                "service_name": schema.service_name,
                "function_name": schema.function_name,
                "goal": schema.goal,
                "method": schema.method,
                "endpoint_url": schema.endpoint_url,
                "explanation": schema.explanation,
                "parameters": [
                    {
                        "name": p.name,
                        "type": p.param_type,
                        "default": p.default_value,
                        "description": p.description,
                        "location": p.location,
                    }
                    for p in schema.dynamic_params
                ],
            })
        return result

    def call(
        self,
        service_name: str,
        mode: str | None = None,
        non_interactive: bool | None = None,
        **kwargs: Any,
    ) -> Any:
        """
        Belirtilen servisi ToS / Kullanıcı Delegasyon Onay kaydı, dinamik parametreler,
        kiracı oturumu, çalışma modu ('local' veya 'cloud') ve HITL non_interactive bayrağıyla çağırır.
        """
        tools = load_registry_tools(self.registry_root)
        target_fn = None
        target_schema = None

        clean_req = service_name.strip().lower()
        for schema, fn in tools:
            if schema.service_name.lower() == clean_req or schema.function_name.lower() == clean_req:
                target_fn = fn
                target_schema = schema
                break

        if target_fn is None or target_schema is None:
            available = [s.service_name for s, _ in tools]
            raise ValueError(f"Servis '{service_name}' bulunamadı! Mevcut servisler: {available}")

        active_mode = (mode or self.mode or "local").strip().lower()
        if active_mode not in ("local", "cloud"):
            active_mode = "local"
        active_ni = self.non_interactive if non_interactive is None else bool(non_interactive)

        # İlk çağrıda ToS / User Delegation onay kaydını doğrula ve logla
        self.consent_manager.ensure_consent(
            tenant_id=self.tenant_id,
            service=target_schema.service_name,
            action="sdk_call",
            mode=active_mode,
        )

        sig = inspect.signature(target_fn)
        call_args = dict(kwargs)

        if "tenant_id" in sig.parameters and "tenant_id" not in call_args:
            call_args["tenant_id"] = self.tenant_id
        if "mode" in sig.parameters and "mode" not in call_args:
            call_args["mode"] = active_mode

        prev_ni = os.environ.get("OWNAPI_NON_INTERACTIVE")
        if active_ni:
            os.environ["OWNAPI_NON_INTERACTIVE"] = "1"
        try:
            return target_fn(**call_args)
        finally:
            if active_ni:
                if prev_ni is None:
                    os.environ.pop("OWNAPI_NON_INTERACTIVE", None)
                else:
                    os.environ["OWNAPI_NON_INTERACTIVE"] = prev_ni

    def validate_all(self, runs: int = 1, auto_heal: bool = True, services: list[str] | None = None) -> dict[str, Any]:
        """Tüm kayıtlı servisleri tarar ve bozuk olanları Self-Healing 2.0 ile otomatik onarır."""
        return self.auto_repair.validate_all_services(runs=runs, auto_heal=auto_heal, services=services)

    def list_hitl_queue(self, status_filter: str | None = None) -> list[dict[str, Any]]:
        """HITL asenkron onay kuyruğundaki bekleyen/tamamlanan görevleri listeler."""
        return self.hitl_queue.list_tasks(tenant_id=self.tenant_id, status_filter=status_filter)

    def resume_hitl_task(self, task_id: str) -> dict[str, Any]:
        """HITL kuyruğunda bekleyen bir görevi tarayıcıda doğrulayarak kaldığı yerden tamamlar."""
        return self.hitl_queue.resume_task(task_id=task_id, tenant_id=self.tenant_id)

    def onboard(
        self,
        service_name: str,
        start_url: str,
        description: str = "",
        headless: bool = False,
        mode: str | None = None,
        auto_validate: bool = True,
        validation_runs: int = 3,
        interactive_tos: bool = False,
    ) -> dict[str, Any]:
        """
        Yeni bir siteyi otomatik olarak dinleyip registry modülüne dönüştürür
        ve 3x Auto-Test (Compiler + Validator Loop) katmanından geçirir.
        """
        active_mode = (mode or self.mode or "local").strip().lower()
        return onboard_new_site(
            service_name=service_name,
            start_url=start_url,
            description=description,
            headless=headless,
            registry_root=self.registry_root,
            tenant_id=self.tenant_id,
            mode=active_mode,
            auto_validate=auto_validate,
            validation_runs=validation_runs,
            interactive_tos=interactive_tos,
        )

    def ask(self, prompt: str) -> str:
        """Doğal dil sorusunu kayıtlı tüm servisleri ve Function Calling'i kullanarak yanıtlar."""
        from agent_demo import ShadowAgent

        if self._agent is None:
            self._agent = ShadowAgent(registry_root=self.registry_root)
        return self._agent.ask(prompt)


# Geriye dönük uyumluluk takma adı
ShadowClient = OwnAPIClient
