from __future__ import annotations

import json
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_ROOT = PROJECT_ROOT / ".sessions"
API_KEYS_STATE_FILE = SESSIONS_ROOT / "api_keys.json"

_STATE_LOCK = threading.RLock()

PLAN_CATALOG: dict[str, dict[str, Any]] = {
    "free": {
        "plan": "free",
        "label": "Free Local Plan",
        "credits": 1_000,
        "mode": "local",
        "price_usd": 0,
    },
    "starter_cloud": {
        "plan": "starter_cloud",
        "label": "Starter Cloud Plan ($10)",
        "credits": 10_000,
        "mode": "cloud",
        "price_usd": 10,
    },
    "growth_cloud": {
        "plan": "growth_cloud",
        "label": "Growth Cloud Plan ($49)",
        "credits": 100_000,
        "mode": "cloud",
        "price_usd": 49,
    },
}


class InsufficientCreditsError(ValueError):
    """Kullanıcının kalan kredisi talep edilen işlem miktarından az olduğunda fırlatılır."""


class APIKeyResult(str):
    """
    Hem doğrudan `str` (`"sk-ownapi-..."`) olarak kullanılabilen,
    hem de sözlük/nesne (`res["api_key"]`, `res["credits"]`, `res.plan`, `res.to_dict()`)
    erişimini destekleyen hibrit dönüş sınıfı.
    """

    def __new__(cls, api_key: str, metadata: dict[str, Any] | None = None) -> APIKeyResult:
        obj = super().__new__(cls, api_key)
        obj._meta = dict(metadata or {})
        obj._meta.setdefault("api_key", api_key)
        return obj

    @property
    def api_key(self) -> str:
        return str(self)

    @property
    def tenant_id(self) -> str:
        return str(self._meta.get("tenant_id", "default"))

    @property
    def plan(self) -> str:
        return str(self._meta.get("plan", "free"))

    @property
    def credits(self) -> int:
        return int(self._meta.get("credits", 0))

    @property
    def mode(self) -> str:
        return str(self._meta.get("mode", "local"))

    @property
    def price_usd(self) -> int:
        return int(self._meta.get("price_usd", 0))

    def __getitem__(self, item: str | int | slice) -> Any:
        if isinstance(item, (int, slice)):
            return super().__getitem__(item)
        return self._meta[item]

    def get(self, key: str, default: Any = None) -> Any:
        return self._meta.get(key, default)

    def keys(self) -> Any:
        return self._meta.keys()

    def items(self) -> Any:
        return self._meta.items()

    def to_dict(self) -> dict[str, Any]:
        return dict(self._meta)


class CreditBalance(int):
    """
    Hem doğrudan `int` (kalan kredi sayısı) olarak kullanılabilen,
    hem de `.get(...)` / `["credits"]` / `.to_dict()` ile detay bilgisi sunan hibrit tam sayı sınıfı.
    """

    def __new__(cls, remaining_credits: int, metadata: dict[str, Any] | None = None) -> CreditBalance:
        obj = super().__new__(cls, int(remaining_credits))
        obj._meta = dict(metadata or {})
        obj._meta["credits"] = int(remaining_credits)
        obj._meta["remaining_credits"] = int(remaining_credits)
        return obj

    @property
    def remaining_credits(self) -> int:
        return int(self)

    @property
    def credits(self) -> int:
        return int(self)

    @property
    def tenant_id(self) -> str:
        return str(self._meta.get("tenant_id", "default"))

    @property
    def plan(self) -> str:
        return str(self._meta.get("plan", "free"))

    @property
    def mode(self) -> str:
        return str(self._meta.get("mode", "local"))

    def __getitem__(self, item: str) -> Any:
        return self._meta[item]

    def get(self, key: str, default: Any = None) -> Any:
        return self._meta.get(key, default)

    def to_dict(self) -> dict[str, Any]:
        return dict(self._meta)


def _sanitize_tenant_id(tenant_id: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", (tenant_id or "default").strip())
    return clean or "default"


def _normalize_plan(plan: str | None) -> str:
    clean = (plan or "free").strip().lower()
    if clean not in PLAN_CATALOG:
        raise ValueError(
            f"Geçersiz paket tipi: '{plan}'. Desteklenen paketler: {list(PLAN_CATALOG.keys())}"
        )
    return clean


def _build_secret_key() -> str:
    """`sk-ownapi-` ile başlayan kriptografik olarak güvenli ve benzersiz bir API anahtarı üretir."""
    return f"sk-ownapi-{secrets.token_urlsafe(24)}"


class APIKeyManager:
    """
    `.sessions/api_keys.json` üzerinde kiracıların (`tenant_id`) API anahtarlarını,
    paket tiplerini (`free`, `starter_cloud`, `growth_cloud`), çalışma modlarını (`local` / `cloud`)
    ve kalan kredilerini kalıcı olarak yöneten sınıf.
    """

    def __init__(self, state_file: Path | str | None = None) -> None:
        self.state_file = Path(state_file) if state_file else API_KEYS_STATE_FILE
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

    def _load_state(self) -> dict[str, Any]:
        with _STATE_LOCK:
            if not self.state_file.exists():
                return {"updated_at": None, "tenants": {}, "key_index": {}}
            try:
                raw = json.loads(self.state_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    raw.setdefault("tenants", {})
                    raw.setdefault("key_index", {})
                    return raw
            except Exception:
                pass
            return {"updated_at": None, "tenants": {}, "key_index": {}}

    def _save_state(self, state: dict[str, Any]) -> None:
        with _STATE_LOCK:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            now_iso = datetime.now(timezone.utc).isoformat()
            state["updated_at"] = now_iso

            # key_index tablosunu tenants üzerinden senkronize et
            tenants = state.get("tenants", {})
            key_index: dict[str, str] = {}
            if isinstance(tenants, dict):
                for t_id, t_data in tenants.items():
                    if isinstance(t_data, dict) and t_data.get("api_key"):
                        key_index[str(t_data["api_key"])] = t_id
            state["key_index"] = key_index

            tmp_file = self.state_file.with_suffix(f".tmp.{os.getpid()}")
            tmp_file.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_file.replace(self.state_file)

    def get_or_create_tenant(self, tenant_id: str = "default") -> dict[str, Any]:
        """
        Kullanıcı (`tenant_id`) kaydını döndürür.
        Eğer kullanıcı henüz kayıtlı değilse varsayılan olarak `free` paket
        (1.000 kredi, `mode="local"`) ve yeni bir `sk-ownapi-...` anahtarı ile oluşturur.
        """
        clean_tenant = _sanitize_tenant_id(tenant_id)
        with _STATE_LOCK:
            state = self._load_state()
            tenants = state.setdefault("tenants", {})
            if clean_tenant in tenants and isinstance(tenants[clean_tenant], dict):
                return dict(tenants[clean_tenant])

            plan_meta = PLAN_CATALOG["free"]
            now_iso = datetime.now(timezone.utc).isoformat()
            api_key = _build_secret_key()
            record: dict[str, Any] = {
                "tenant_id": clean_tenant,
                "api_key": api_key,
                "plan": plan_meta["plan"],
                "plan_label": plan_meta["label"],
                "credits": int(plan_meta["credits"]),
                "mode": plan_meta["mode"],
                "price_usd": int(plan_meta["price_usd"]),
                "total_consumed": 0,
                "created_at": now_iso,
                "updated_at": now_iso,
            }
            tenants[clean_tenant] = record
            self._save_state(state)
            return dict(record)

    def generate_api_key(self, tenant_id: str = "default", plan: str = "free") -> APIKeyResult:
        """
        Belirtilen `tenant_id` ve `plan` (`free`, `starter_cloud`, `growth_cloud`) için
        `sk-ownapi-` ile başlayan güvenli bir API anahtarı üretir ve `.sessions/api_keys.json`
        dosyasına kaydeder.
        """
        clean_tenant = _sanitize_tenant_id(tenant_id)
        clean_plan = _normalize_plan(plan)
        plan_meta = PLAN_CATALOG[clean_plan]

        with _STATE_LOCK:
            state = self._load_state()
            tenants = state.setdefault("tenants", {})
            existing = tenants.get(clean_tenant) if isinstance(tenants.get(clean_tenant), dict) else {}
            now_iso = datetime.now(timezone.utc).isoformat()
            new_key = _build_secret_key()

            record: dict[str, Any] = {
                "tenant_id": clean_tenant,
                "api_key": new_key,
                "plan": plan_meta["plan"],
                "plan_label": plan_meta["label"],
                "credits": int(plan_meta["credits"]),
                "mode": plan_meta["mode"],
                "price_usd": int(plan_meta["price_usd"]),
                "total_consumed": int(existing.get("total_consumed", 0)),
                "created_at": existing.get("created_at") or now_iso,
                "updated_at": now_iso,
            }
            tenants[clean_tenant] = record
            self._save_state(state)
            return APIKeyResult(new_key, record)

    def resolve_tenant_id(self, tenant_id_or_key: str) -> str:
        """
        Verilen değerin `sk-ownapi-...` anahtarı mı yoksa `tenant_id` mi olduğunu çözer.
        """
        raw = (tenant_id_or_key or "default").strip()
        with _STATE_LOCK:
            state = self._load_state()
            key_index = state.get("key_index", {})
            if raw in key_index:
                return str(key_index[raw])
            for t_id, t_data in state.get("tenants", {}).items():
                if isinstance(t_data, dict) and t_data.get("api_key") == raw:
                    return str(t_id)
            if raw.startswith("sk-ownapi-"):
                raise KeyError(f"Geçersiz veya tanımsız API anahtarı: '{raw}'")
            return _sanitize_tenant_id(raw)

    def consume_credit(self, tenant_id_or_key: str, amount: int = 1) -> CreditBalance:
        """
        Her kullanımda kullanıcının (`tenant_id` veya `sk-ownapi-...` anahtarı) kredisinden
        `amount` kadar düşer, `.sessions/api_keys.json` dosyasını günceller ve kalan krediyi döndürür.
        Yeni bir `tenant_id` ile çağrılırsa önce varsayılan `free` paketi (1.000 kredi, `mode="local"`) tanımlar.
        """
        if amount < 0:
            raise ValueError("Düşülecek kredi miktarı (amount) negatif olamaz.")

        with _STATE_LOCK:
            clean_tenant = self.resolve_tenant_id(tenant_id_or_key)
            state = self._load_state()
            tenants = state.setdefault("tenants", {})
            if clean_tenant not in tenants or not isinstance(tenants[clean_tenant], dict):
                self.get_or_create_tenant(clean_tenant)
                state = self._load_state()
                tenants = state.setdefault("tenants", {})

            record = tenants[clean_tenant]
            current_credits = int(record.get("credits", 0))
            if current_credits < amount:
                raise InsufficientCreditsError(
                    f"Yetersiz kredi! Kiracı '{clean_tenant}' ({record.get('plan')}) için "
                    f"kalan kredi: {current_credits}, gereken: {amount}."
                )

            remaining = current_credits - int(amount)
            record["credits"] = remaining
            record["total_consumed"] = int(record.get("total_consumed", 0)) + int(amount)
            record["updated_at"] = datetime.now(timezone.utc).isoformat()
            tenants[clean_tenant] = record
            self._save_state(state)
            return CreditBalance(remaining, record)

    def upgrade_plan(self, tenant_id: str, plan: str = "starter_cloud") -> APIKeyResult:
        """
        Kullanıcıyı belirtilen pakete geçirir ve yeni `sk-ownapi-...` anahtarını döndürür:
        - `starter_cloud`: $10 bulut paketi -> 10.000 kredi, `mode="cloud"`
        - `growth_cloud`: $49 bulut paketi -> 100.000 kredi, `mode="cloud"`
        - `free`: $0 yerel paket -> 1.000 kredi, `mode="local"`
        """
        clean_tenant = _sanitize_tenant_id(tenant_id)
        clean_plan = _normalize_plan(plan)
        plan_meta = PLAN_CATALOG[clean_plan]

        with _STATE_LOCK:
            state = self._load_state()
            tenants = state.setdefault("tenants", {})
            existing = tenants.get(clean_tenant) if isinstance(tenants.get(clean_tenant), dict) else {}
            now_iso = datetime.now(timezone.utc).isoformat()
            new_key = _build_secret_key()

            record: dict[str, Any] = {
                "tenant_id": clean_tenant,
                "api_key": new_key,
                "plan": plan_meta["plan"],
                "plan_label": plan_meta["label"],
                "credits": int(plan_meta["credits"]),
                "mode": plan_meta["mode"],
                "price_usd": int(plan_meta["price_usd"]),
                "total_consumed": int(existing.get("total_consumed", 0)),
                "created_at": existing.get("created_at") or now_iso,
                "upgraded_at": now_iso,
                "updated_at": now_iso,
            }
            tenants[clean_tenant] = record
            self._save_state(state)
            return APIKeyResult(new_key, record)

    def get_tenant_info(self, tenant_id_or_key: str = "default") -> dict[str, Any]:
        """Kullanıcının güncel paket, mod, anahtar ve kalan kredi bilgilerini döndürür."""
        clean_tenant = self.resolve_tenant_id(tenant_id_or_key)
        return self.get_or_create_tenant(clean_tenant)


# Modül düzeyinde varsayılan yönetici ve doğrudan çağrılabilir fonksiyonlar
_default_manager = APIKeyManager()


def get_or_create_tenant(
    tenant_id: str = "default",
    state_file: Path | str | None = None,
) -> dict[str, Any]:
    mgr = APIKeyManager(state_file=state_file) if state_file else _default_manager
    return mgr.get_or_create_tenant(tenant_id=tenant_id)


def generate_api_key(
    tenant_id: str = "default",
    plan: str = "free",
    state_file: Path | str | None = None,
) -> APIKeyResult:
    mgr = APIKeyManager(state_file=state_file) if state_file else _default_manager
    return mgr.generate_api_key(tenant_id=tenant_id, plan=plan)


def consume_credit(
    tenant_id_or_key: str,
    amount: int = 1,
    state_file: Path | str | None = None,
) -> CreditBalance:
    mgr = APIKeyManager(state_file=state_file) if state_file else _default_manager
    return mgr.consume_credit(tenant_id_or_key=tenant_id_or_key, amount=amount)


def upgrade_plan(
    tenant_id: str,
    plan: str = "starter_cloud",
    state_file: Path | str | None = None,
) -> APIKeyResult:
    mgr = APIKeyManager(state_file=state_file) if state_file else _default_manager
    return mgr.upgrade_plan(tenant_id=tenant_id, plan=plan)


def get_tenant_info(
    tenant_id_or_key: str = "default",
    state_file: Path | str | None = None,
) -> dict[str, Any]:
    mgr = APIKeyManager(state_file=state_file) if state_file else _default_manager
    return mgr.get_tenant_info(tenant_id_or_key=tenant_id_or_key)
