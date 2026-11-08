from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class TokenStore:
    """
    Çok kiracılı (multi-tenant) oturum ve çerez izolasyonunu sağlayan merkezi token deposu.
    Her kiracı (tenant_id) ve servis (service) için oturum çerezlerini izole olarak
    `.sessions/{tenant_id}/{service}_tokens.json` dosya yolunda saklar.
    Geriye dönük uyumluluk için kök dizindeki `session_tokens.json` dosyasını otomatik okur ve günceller.
    """

    def __init__(self, sessions_dir: Path | str = Path(".sessions"), legacy_tokens_path: Path | str = Path("session_tokens.json")) -> None:
        self.sessions_dir = Path(sessions_dir)
        self.legacy_tokens_path = Path(legacy_tokens_path)
        self._lock = threading.Lock()

    def get_session_path(self, tenant_id: str = "default", service: str = "thy") -> Path:
        """Belirtilen kiracı ve servis için özel session_tokens dosya yolunu döndürür."""
        clean_tenant = str(tenant_id).strip().replace("/", "_").replace("\\", "_") or "default"
        clean_service = str(service).strip().lower().replace("/", "_").replace("\\", "_") or "default"
        tenant_dir = self.sessions_dir / clean_tenant
        return tenant_dir / f"{clean_service}_tokens.json"

    def get_session(self, tenant_id: str = "default", service: str = "thy") -> dict[str, Any]:
        """
        Kiracı ve servise ait güncel oturum demetini (cookie, user-agent, headers vb.) döndürür.
        Eğer tenant_id='default' ve henüz izole dosya oluşmamışsa, geriye dönük uyumluluk
        olarak kök dizindeki `session_tokens.json` dosyasını okur.
        """
        with self._lock:
            session_path = self.get_session_path(tenant_id, service)
            if session_path.exists():
                try:
                    data = json.loads(session_path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        return data
                except Exception:
                    pass

            # Geriye dönük uyumluluk: default tenant için legacy session_tokens.json kontrolü
            if tenant_id == "default" and self.legacy_tokens_path.exists():
                try:
                    data = json.loads(self.legacy_tokens_path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        return data
                except Exception:
                    pass

            return {}

    def save_session(
        self,
        tenant_id: str = "default",
        service: str = "thy",
        cookies: dict[str, str] | list[dict[str, Any]] | str = "",
        user_agent: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Path:
        """
        Kiracı ve servise ait güncel oturumu atomik ve thread-safe olarak izole dosyaya kaydeder.
        cookies parametresi:
          - dict: {"_px3": "abc", "bm_sz": "123"}
          - list: Playwright context.cookies() formatı ([{"name": "_px3", "value": "abc"}, ...])
          - str: "_px3=abc; bm_sz=123"
        """
        cookie_str = ""
        cookie_dict: dict[str, str] = {}
        playwright_cookies: list[dict[str, Any]] = []

        if isinstance(cookies, dict):
            cookie_dict = {str(k).strip(): str(v).strip() for k, v in cookies.items() if k}
            cookie_str = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
        elif isinstance(cookies, list):
            # Playwright cookies listesi
            clean_parts = []
            for c in cookies:
                if isinstance(c, dict) and c.get("name"):
                    k, v = str(c["name"]).strip(), str(c.get("value", "")).strip()
                    cookie_dict[k] = v
                    clean_parts.append(f"{k}={v}")
                    playwright_cookies.append(c)
            cookie_str = "; ".join(clean_parts)
        elif isinstance(cookies, str):
            cookie_str = cookies.strip()
            for part in cookie_str.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    k_clean, v_clean = k.strip(), v.strip()
                    if k_clean:
                        cookie_dict[k_clean] = v_clean

        meta = dict(metadata or {})
        target_url = meta.get("target_url") or ""

        token_bundle: dict[str, Any] = {
            "tenant_id": tenant_id,
            "service": service,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "target_url": target_url,
            "cookie": cookie_str,
            "cookie_dict": cookie_dict,
            "playwright_cookies": playwright_cookies,
            "user-agent": user_agent or meta.get("user-agent", ""),
            "headers": {
                "cookie": cookie_str,
                "user-agent": user_agent or meta.get("user-agent", ""),
                "accept": "application/json",
                "content-type": "application/json",
            },
            "metadata": meta,
        }

        # Mevcut ekstra başlıkları koru
        if "headers" in meta and isinstance(meta["headers"], dict):
            token_bundle["headers"].update(meta["headers"])
            if cookie_str:
                token_bundle["headers"]["cookie"] = cookie_str
            if user_agent:
                token_bundle["headers"]["user-agent"] = user_agent

        with self._lock:
            session_path = self.get_session_path(tenant_id, service)
            session_path.parent.mkdir(parents=True, exist_ok=True)
            session_path.write_text(json.dumps(token_bundle, ensure_ascii=False, indent=2), encoding="utf-8")

            # Geriye dönük uyumluluk: default kiracı ise kök dizindeki session_tokens.json'ı da güncelle
            if tenant_id == "default":
                try:
                    self.legacy_tokens_path.write_text(json.dumps(token_bundle, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception:
                    pass

        return session_path

    def clear_session(self, tenant_id: str = "default", service: str = "thy") -> bool:
        """Belirtilen kiracı ve servisin oturum dosyasını güvenle temizler."""
        with self._lock:
            session_path = self.get_session_path(tenant_id, service)
            if session_path.exists():
                try:
                    session_path.unlink()
                    return True
                except Exception:
                    return False
            return False

    def list_tenants(self) -> list[str]:
        """Kayıtlı tüm kiracıları listeler."""
        if not self.sessions_dir.exists():
            return ["default"]
        tenants = [p.name for p in self.sessions_dir.iterdir() if p.is_dir()]
        if "default" not in tenants:
            tenants.insert(0, "default")
        return sorted(tenants)

    def list_sessions(self, tenant_id: str = "default") -> list[str]:
        """Belirtilen kiracıya ait kayıtlı servis isimlerini listeler."""
        clean_tenant = str(tenant_id).strip() or "default"
        tenant_dir = self.sessions_dir / clean_tenant
        if not tenant_dir.exists():
            return []
        services = []
        for p in tenant_dir.glob("*_tokens.json"):
            svc = p.name.replace("_tokens.json", "")
            services.append(svc)
        return sorted(services)

    def get_playwright_cookies(
        self,
        tenant_id: str = "default",
        service: str = "thy",
        default_url: str = "",
    ) -> list[dict[str, Any]]:
        """
        Kayıtlı oturumdaki çerezleri Playwright context.add_cookies() formatına dönüştürür.
        """
        session = self.get_session(tenant_id=tenant_id, service=service)
        if not session:
            return []

        pw_cookies = session.get("playwright_cookies")
        if isinstance(pw_cookies, list) and pw_cookies:
            valid = []
            for c in pw_cookies:
                if isinstance(c, dict) and c.get("name") and c.get("value") is not None:
                    if c.get("domain") or c.get("url"):
                        valid.append(c)
                    elif default_url:
                        c_copy = dict(c)
                        c_copy["url"] = default_url
                        valid.append(c_copy)
            if valid:
                return valid

        target_url = default_url or session.get("target_url") or ""
        if not target_url:
            return []

        cookie_dict = session.get("cookie_dict")
        if not isinstance(cookie_dict, dict) or not cookie_dict:
            raw_cookie = session.get("cookie", "")
            cookie_dict = {}
            for part in str(raw_cookie).split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    if k.strip():
                        cookie_dict[k.strip()] = v.strip()

        result = []
        for k, v in cookie_dict.items():
            if k and v is not None:
                result.append({
                    "name": str(k).strip(),
                    "value": str(v).strip(),
                    "url": target_url,
                })
        return result
