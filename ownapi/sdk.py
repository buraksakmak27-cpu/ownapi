from __future__ import annotations

import importlib.util
import inspect
import os
import pathlib
import sys
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.api_keys import (
    APIKeyManager,
    CreditBalance,
    consume_credit,
    generate_api_key,
    get_or_create_tenant,
    get_tenant_info,
    upgrade_plan,
)
from core.auto_repair import AutoRepairEngine
from core.base_client import BaseOwnClient, BaseShadowClient
from core.consent import ConsentManager
from core.hitl_queue import HITLQueueManager
from core.onboarding import onboard_new_site
from core.token_store import TokenStore
from master_mcp import load_registry_tools


def _load_connector_module(service_name: str, registry_root: Path | None = None) -> Any:
    """`registry/<service_name>/client.py` modülünü dinamik olarak yükler ve döndürür."""
    reg_dir = Path(registry_root) if registry_root else (PROJECT_ROOT / "registry")
    clean_svc = service_name.strip().lower()
    client_file = reg_dir / clean_svc / "client.py"
    if not client_file.exists():
        raise AttributeError(
            f"OwnAPI konektörü bulunamadı: 'registry/{clean_svc}/client.py'"
        )

    mod_name = f"ownapi_connector_{clean_svc}"
    if mod_name in sys.modules:
        return sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location(mod_name, str(client_file.resolve()))
    if spec is None or spec.loader is None:
        raise ImportError(f"Konektör modülü yüklenemedi: {client_file}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class ServiceNamespaceProxy:
    """
    `from ownapi import thy, trendyol, pegasus` veya `api.thy.search_flights(...)`
    kullanımlarını yöneten akıllı servis vekil (proxy) sınıfı.

    Her çağrıda:
    1. `core/api_keys.py` üzerinden aktif kullanıcı (`default` veya `api_key` sahibi kiracı)
       kredisinden `1` kredi düşer (`consume_credit`).
    2. Kullanıcının paketine ait çalışma modunu (`mode="local"` veya `mode="cloud"`) ve
       `tenant_id` bilgisini hedef konektör fonksiyonuna otomatik enjekte eder.
    """

    def __init__(
        self,
        service_name: str,
        owner: OwnAPI | None = None,
        default_tenant_id: str = "default",
        default_mode: str = "local",
        registry_root: Path | None = None,
        state_file: Path | str | None = None,
    ) -> None:
        self._service_name = service_name.strip().lower()
        self._owner = owner
        self._default_tenant_id = default_tenant_id
        self._default_mode = default_mode
        self._registry_root = Path(registry_root) if registry_root else (PROJECT_ROOT / "registry")
        self._state_file = state_file
        self._module: Any = None

    def _get_module(self) -> Any:
        if self._module is None:
            self._module = _load_connector_module(self._service_name, self._registry_root)
        return self._module

    def _resolve_execution_context(self) -> tuple[str, str, str | None, CreditBalance]:
        """
        Çağrı öncesi `core/api_keys.py` üzerinden kredi düşümünü yapar ve
        `(tenant_id, mode, api_key, credit_balance)` dörtlüsünü döndürür.
        """
        if self._owner is not None:
            auth_identifier = self._owner.api_key or self._owner.tenant_id
            state_path = self._owner._state_file
            info = get_tenant_info(auth_identifier, state_file=state_path)
            balance = consume_credit(auth_identifier, amount=1, state_file=state_path)
            active_tenant = str(info.get("tenant_id") or self._owner.tenant_id or "default")
            active_mode = str(self._owner._explicit_mode or info.get("mode") or self._owner.mode or "local")
            return active_tenant, active_mode, self._owner.api_key, balance

        # Bedava yerel mod (`from ownapi import thy, trendyol, pegasus`)
        info = get_or_create_tenant(self._default_tenant_id, state_file=self._state_file)
        balance = consume_credit(self._default_tenant_id, amount=1, state_file=self._state_file)
        active_tenant = str(info.get("tenant_id") or self._default_tenant_id)
        active_mode = str(info.get("mode") or self._default_mode or "local")
        return active_tenant, active_mode, info.get("api_key"), balance

    def _resolve_callable(self, attr_name: str) -> Callable[..., Any]:
        mod = self._get_module()

        # 1. Doğrudan modül fonksiyonu varsa (örn. thy.search_flights, pegasus.get_flight_availability)
        target = getattr(mod, attr_name, None)
        if callable(target) and not inspect.isclass(target):
            return target

        # 2. Modül içindeki BaseOwnClient alt sınıfı üzerinde metot varsa (örn. trendyol.search)
        client_cls = None
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if issubclass(obj, BaseOwnClient) and obj is not BaseOwnClient:
                client_cls = obj
                break

        if client_cls is not None and hasattr(client_cls, attr_name):
            def _bound_method_runner(
                *args: Any,
                tenant_id: str = "default",
                mode: str = "local",
                **kwargs: Any,
            ) -> Any:
                init_sig = inspect.signature(client_cls.__init__)
                init_kwargs: dict[str, Any] = {}
                if "tenant_id" in init_sig.parameters:
                    init_kwargs["tenant_id"] = tenant_id
                if "mode" in init_sig.parameters:
                    init_kwargs["mode"] = mode
                instance = client_cls(**init_kwargs)
                instance.tenant_id = tenant_id
                instance.mode = mode
                method_fn = getattr(instance, attr_name)
                m_sig = inspect.signature(method_fn)
                if "tenant_id" in m_sig.parameters:
                    kwargs["tenant_id"] = tenant_id
                if "mode" in m_sig.parameters:
                    kwargs["mode"] = mode
                return method_fn(*args, **kwargs)

            return _bound_method_runner

        # 3. Yaygın takma adlar (search / search_flights / call) için ana fonksiyonu bul
        if attr_name in ("search", "search_flights", "get_flight_availability", "call"):
            public_funcs = [
                obj
                for name, obj in inspect.getmembers(mod, inspect.isfunction)
                if obj.__module__ == mod.__name__ and not name.startswith("_")
            ]
            if public_funcs:
                return public_funcs[0]

        raise AttributeError(
            f"'{self._service_name}' servisinde '{attr_name}' fonksiyonu bulunamadı."
        )

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        mod = self._get_module()
        raw_attr = getattr(mod, name, None)
        if raw_attr is not None and not callable(raw_attr):
            return raw_attr

        target_fn = self._resolve_callable(name)

        def _wrapped(*args: Any, **kwargs: Any) -> Any:
            active_tenant, active_mode, _, _ = self._resolve_execution_context()
            sig = inspect.signature(target_fn)
            call_kwargs = dict(kwargs)
            if "tenant_id" in sig.parameters and "tenant_id" not in call_kwargs:
                call_kwargs["tenant_id"] = active_tenant
            if "mode" in sig.parameters and "mode" not in call_kwargs:
                call_kwargs["mode"] = active_mode
            return target_fn(*args, **call_kwargs)

        _wrapped.__name__ = name
        _wrapped.__doc__ = getattr(target_fn, "__doc__", None)
        return _wrapped

    def __repr__(self) -> str:
        mode_str = self._owner.mode if self._owner else self._default_mode
        return f"<OwnAPI ServiceProxy '{self._service_name}' (mode='{mode_str}')>"


class OwnAPI:
    """
    OwnAPI Birleşik Python SDK İstemcisi (`from ownapi import OwnAPI`).
    - Bedava Yerel Mod veya `$10` (`starter_cloud`) / `$49` (`growth_cloud`) bulut anahtarıyla çalışır:
      `api = OwnAPI(api_key="sk-ownapi-...")`
      `api.thy.search_flights(origin_airport_code="IST", destination_airport_code="ESB")`
      `api.trendyol.search(query="laptop")`
    """

    def __init__(
        self,
        api_key: str | None = None,
        tenant_id: str | None = None,
        mode: str | None = None,
        cloud_cdp_url: str | None = None,
        non_interactive: bool = False,
        registry_root: pathlib.Path | str = PROJECT_ROOT / "registry",
        state_file: pathlib.Path | str | None = None,
        **kwargs: Any,
    ) -> None:
        self._state_file = state_file
        self.api_key = (api_key or os.getenv("OWNAPI_API_KEY", "")).strip() or None
        self._explicit_mode = mode.strip().lower() if isinstance(mode, str) and mode.strip() else None

        if self.api_key:
            tenant_record = get_tenant_info(self.api_key, state_file=self._state_file)
            self.tenant_id = str(tenant_record.get("tenant_id", tenant_id or "default"))
            self.plan = str(tenant_record.get("plan", "free"))
            self.mode = str(self._explicit_mode or tenant_record.get("mode", "local")).lower()
        else:
            resolved_tenant = (tenant_id or "default").strip() or "default"
            tenant_record = get_or_create_tenant(resolved_tenant, state_file=self._state_file)
            self.tenant_id = str(tenant_record.get("tenant_id", resolved_tenant))
            self.api_key = str(tenant_record.get("api_key", "")) or None
            self.plan = str(tenant_record.get("plan", "free"))
            self.mode = str(self._explicit_mode or tenant_record.get("mode", "local")).lower()

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
        self._service_proxies: dict[str, ServiceNamespaceProxy] = {}

    @property
    def credits(self) -> int:
        """Kullanıcının güncel kalan kredi bakiyesini döndürür."""
        info = get_tenant_info(self.api_key or self.tenant_id, state_file=self._state_file)
        return int(info.get("credits", 0))

    def status(self) -> dict[str, Any]:
        """Kullanıcının paket, kalan kredi, çalışma modu ve anahtar durumunu döndürür."""
        return get_tenant_info(self.api_key or self.tenant_id, state_file=self._state_file)

    def __getattr__(self, name: str) -> ServiceNamespaceProxy:
        if name.startswith("_"):
            raise AttributeError(name)
        clean_name = name.strip().lower()
        if clean_name not in self._service_proxies:
            client_file = self.registry_root / clean_name / "client.py"
            if not client_file.exists():
                raise AttributeError(f"OwnAPI üzerinde '{name}' adında bir servis veya öznitelik bulunamadı.")
            self._service_proxies[clean_name] = ServiceNamespaceProxy(
                service_name=clean_name,
                owner=self,
                default_tenant_id=self.tenant_id,
                default_mode=self.mode,
                registry_root=self.registry_root,
                state_file=self._state_file,
            )
        return self._service_proxies[clean_name]

    def list_services(self) -> list[dict[str, Any]]:
        """Kayıtlı tüm servislerin şemalarını, fonksiyon adlarını ve parametrelerini listeler."""
        tools = load_registry_tools(self.registry_root)
        result: list[dict[str, Any]] = []
        for schema, _ in tools:
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
        _consume_credit: bool = True,
        **kwargs: Any,
    ) -> Any:
        """
        Belirtilen servisi API anahtarı / kredi kontrolü, ToS delegasyon onayı ve
        aktif çalışma modu ('local' veya 'cloud') ile çağırır.
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

        # Kredi düşümü ve mod çözümlemesi
        auth_id = self.api_key or self.tenant_id
        info = get_tenant_info(auth_id, state_file=self._state_file)
        if _consume_credit:
            consume_credit(auth_id, amount=1, state_file=self._state_file)

        active_mode = (mode or self._explicit_mode or info.get("mode") or self.mode or "local").strip().lower()
        if active_mode not in ("local", "cloud"):
            active_mode = "local"
        active_ni = self.non_interactive if non_interactive is None else bool(non_interactive)

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
        return self.auto_repair.validate_all_services(runs=runs, auto_heal=auto_heal, services=services)

    def list_hitl_queue(self, status_filter: str | None = None) -> list[dict[str, Any]]:
        return self.hitl_queue.list_tasks(tenant_id=self.tenant_id, status_filter=status_filter)

    def resume_hitl_task(self, task_id: str) -> dict[str, Any]:
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

    def ask(self, prompt: str, _consume_credit: bool = True) -> str:
        from agent_demo import ShadowAgent

        auth_id = self.api_key or self.tenant_id
        info = get_tenant_info(auth_id, state_file=self._state_file)
        if _consume_credit:
            consume_credit(auth_id, amount=1, state_file=self._state_file)
        self.mode = str(self._explicit_mode or info.get("mode") or self.mode or "local").lower()

        if self._agent is None:
            self._agent = ShadowAgent(registry_root=self.registry_root)
        return self._agent.ask(prompt)


# Geriye dönük uyumluluk takma adları
OwnAPIClient = OwnAPI
ShadowClient = OwnAPI
