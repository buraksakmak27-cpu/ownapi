from __future__ import annotations

import hashlib
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel

PROJECT_ROOT = Path(__file__).resolve().parents[1]
console = Console(stderr=True, legacy_windows=False)

TOS_VERSION = "1.0-Enterprise"

USER_DELEGATION_DISCLAIMER = (
    "OwnAPI (User-Delegated Browser Middleware) Kullanım Koşulları ve Yetkilendirme Beyanı:\n"
    "1. Kullanıcı Delegasyonu (Plaid-Style Delegation): Bu yazılım gizli bir bot veya sunucu taraflı\n"
    "   veri kazıyıcı (scraper) DEĞİLDİR. Tüm işlemler yalnızca kullanıcının kendi açık rızasıyla,\n"
    "   kendi tarayıcı oturumu, kendi kimlik bilgileri ve kendi IP adresi üzerinden vekaleten yürütülür.\n"
    "2. Veri İzolasyonu: Yakalanan oturum çerezleri yalnızca yerel/izole `.sessions/{tenant_id}/`\n"
    "   kasasında saklanır ve üçüncü taraflarla paylaşılmaz.\n"
    "3. Sorumluluk: Kullanıcı, hedef web servisinin kullanım koşullarına (ToS) kendi hesabı ve\n"
    "   oturumu kapsamında uymaktan sorumludur."
)


class ConsentManager:
    """
    ToS / Kullanıcı Onay Mekanizması (User Delegation Disclaimer & Audit Trail).
    Yeni bir site eklenirken (onboarding) veya bir servis ilk kez çağrıldığında
    kullanıcının kendi tarayıcısı/hesabıyla bağlandığını onaylayan yasal kayıt katmanıdır.
    Onay kayıtlarını `.sessions/{tenant_id}/consent_audit.json` altında tutar.
    """

    def __init__(self, sessions_root: Path | str = PROJECT_ROOT / ".sessions") -> None:
        self.sessions_root = Path(sessions_root)
        self.sessions_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _sanitize(value: str) -> str:
        clean = "".join(c if (c.isalnum() or c in ("-", "_")) else "_" for c in value.strip())
        return clean or "default"

    def _audit_file_path(self, tenant_id: str) -> Path:
        t_dir = self.sessions_root / self._sanitize(tenant_id)
        t_dir.mkdir(parents=True, exist_ok=True)
        return t_dir / "consent_audit.json"

    def get_consents(self, tenant_id: str = "default") -> list[dict[str, Any]]:
        """Belirtilen kiracı (tenant) için kaydedilmiş tüm yasal onay kayıtlarını döndürür."""
        audit_file = self._audit_file_path(tenant_id)
        if not audit_file.exists():
            return []
        try:
            data = json.loads(audit_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("consents"), list):
                return data["consents"]
        except Exception:
            pass
        return []

    def has_consented(self, tenant_id: str, service: str) -> bool:
        """Kullanıcının belirtilen servis için daha önce onay verip vermediğini kontrol eder."""
        clean_svc = self._sanitize(service)
        for entry in self.get_consents(tenant_id):
            if entry.get("service") in (clean_svc, "*") and entry.get("consent_granted") is True:
                return True
        return False

    def record_consent(
        self,
        tenant_id: str,
        service: str,
        action: str = "first_invocation",
        mode: str = "local",
        accepted_by: str | None = None,
    ) -> dict[str, Any]:
        """Kullanıcı onayını zaman damgası ve kriptografik özet ile denetim günlüğüne (audit log) yazar."""
        audit_file = self._audit_file_path(tenant_id)
        existing = self.get_consents(tenant_id)

        disclaimer_hash = hashlib.sha256(USER_DELEGATION_DISCLAIMER.encode("utf-8")).hexdigest()[:16]
        host_user = accepted_by or os.getenv("USERNAME") or os.getenv("USER") or "authenticated_user"
        try:
            hostname = socket.gethostname()
        except Exception:
            hostname = "localhost"

        record: dict[str, Any] = {
            "tenant_id": self._sanitize(tenant_id),
            "service": self._sanitize(service),
            "action": action,
            "execution_mode": mode,
            "tos_version": TOS_VERSION,
            "disclaimer_sha256": disclaimer_hash,
            "consent_granted": True,
            "delegated_by": f"{host_user}@{hostname}",
            "accepted_at": datetime.now(timezone.utc).isoformat(),
        }

        existing.append(record)
        payload = {
            "tenant_id": self._sanitize(tenant_id),
            "tos_version": TOS_VERSION,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "total_records": len(existing),
            "consents": existing,
        }
        audit_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return record

    def ensure_consent(
        self,
        tenant_id: str,
        service: str,
        action: str = "first_invocation",
        mode: str = "local",
        interactive: bool = False,
        force_banner: bool = False,
    ) -> dict[str, Any]:
        """
        Servis çağrılmadan veya yeni site eklenmeden önce ToS / Kullanıcı Delegasyon onayını doğrular.
        İlk kullanımda veya onboarding sırasında yasal onay bildirimini ekrana basar ve loglar.
        """
        already_consented = self.has_consented(tenant_id, service)
        if already_consented and not force_banner:
            consents = self.get_consents(tenant_id)
            return next(
                (c for c in reversed(consents) if c.get("service") in (self._sanitize(service), "*")),
                {},
            )

        console.print(
            Panel.fit(
                f"[bold yellow]⚖️ Terms of Service Agreement / User Delegation Disclaimer[/bold yellow]\n"
                f"[white]{USER_DELEGATION_DISCLAIMER}[/white]\n\n"
                f"[dim]Tenant:[/dim] [cyan]{tenant_id}[/cyan] | "
                f"[dim]Servis:[/dim] [magenta]{service}[/magenta] | "
                f"[dim]Çalışma Modu:[/dim] [green]{mode.upper()}[/green] | "
                f"[dim]İşlem:[/dim] [yellow]{action}[/yellow]",
                title="OwnAPI Legal Shield & User Delegation Consent",
                border_style="yellow",
            )
        )

        if interactive and sys.stdin and sys.stdin.isatty():
            ans = input("👉 Kendi tarayıcı oturumunuzla vekaleten işlem yapılmasını onaylıyor musunuz? [E/h]: ").strip().lower()
            if ans in ("h", "n", "hayir", "hayır", "no"):
                raise PermissionError("Kullanıcı ToS / User Delegation onayını reddetti. İşlem iptal edildi.")

        record = self.record_consent(
            tenant_id=tenant_id,
            service=service,
            action=action,
            mode=mode,
        )
        console.print(
            f"[bold green]✔ Kullanıcı Delegasyon Onayı Kaydedildi:[/bold green] "
            f"[dim].sessions/{self._sanitize(tenant_id)}/consent_audit.json "
            f"(SHA256: {record['disclaimer_sha256']})[/dim]"
        )
        return record


import sys  # noqa: E402
