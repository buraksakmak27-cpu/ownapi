from __future__ import annotations

import json
import os
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

PROJECT_ROOT = Path(__file__).resolve().parents[1]
console = Console(stderr=True, legacy_windows=False)


class HITLQueueManager:
    """
    Human-in-the-Loop (HITL) Asenkron Onay Kuyruğu Yöneticisi.
    Arka planda veya gece çalışan ajanlar Captcha / PerimeterX / Akamai / 2FA doğrulamasına
    takıldığında terminalde `input()` ile kilitlenmek yerine görevi
    `.sessions/{tenant_id}/hitl_queue.json` dosyasına kaydeder, opsiyonel olarak
    `OWNAPI_WEBHOOK_URL` adresine bildirim gönderir ve kullanıcı `own-api resume <task_id>`
    (veya `own-api approve <task_id>`) çalıştırdığında tarayıcıyı açıp görevi kaldığı yerden tamamlar.
    """

    def __init__(
        self,
        sessions_root: Path | str = PROJECT_ROOT / ".sessions",
        webhook_url: str | None = None,
    ) -> None:
        load_dotenv(override=False)
        self.sessions_root = Path(sessions_root)
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        self.webhook_url = (webhook_url or os.getenv("OWNAPI_WEBHOOK_URL", "")).strip() or None

    @staticmethod
    def _sanitize(value: str) -> str:
        clean = "".join(c if (c.isalnum() or c in ("-", "_")) else "_" for c in value.strip())
        return clean or "default"

    def _queue_file_path(self, tenant_id: str = "default") -> Path:
        t_dir = self.sessions_root / self._sanitize(tenant_id)
        t_dir.mkdir(parents=True, exist_ok=True)
        return t_dir / "hitl_queue.json"

    def _load_queue_data(self, tenant_id: str = "default") -> dict[str, Any]:
        q_file = self._queue_file_path(tenant_id)
        if not q_file.exists():
            return {"tenant_id": self._sanitize(tenant_id), "tasks": []}
        try:
            data = json.loads(q_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("tasks"), list):
                return data
        except Exception:
            pass
        return {"tenant_id": self._sanitize(tenant_id), "tasks": []}

    def _save_queue_data(self, tenant_id: str, tasks: list[dict[str, Any]]) -> None:
        q_file = self._queue_file_path(tenant_id)
        payload = {
            "tenant_id": self._sanitize(tenant_id),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "total_tasks": len(tasks),
            "pending_count": sum(1 for t in tasks if t.get("status") == "PENDING_USER_ACTION"),
            "tasks": tasks,
        }
        q_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _resolve_session_target_url(self, service: str, url: str, target_url: str | None = None) -> str:
        """
        Tarayıcının direkt boş/404 API uç noktasına (/api/...) gitmesini önlemek için
        her zaman servisin ana oturum sayfasını (örn. THY için https://www.turkishairlines.com/tr-tr/) döndürür.
        """
        clean_svc = self._sanitize(service).lower()
        if clean_svc == "thy" or "turkishairlines.com" in (url or "").lower():
            return "https://www.turkishairlines.com/tr-tr/"

        from urllib.parse import urlparse

        if target_url and target_url.rstrip("/") != (url or "").rstrip("/"):
            parsed_t = urlparse(target_url)
            if "/api/" not in parsed_t.path.lower():
                return target_url

        schema_file = PROJECT_ROOT / "registry" / clean_svc / "schema.json"
        if schema_file.exists():
            try:
                s_data = json.loads(schema_file.read_text(encoding="utf-8"))
                cand = str(s_data.get("target_url", "")).strip()
                if cand and "/api/" not in urlparse(cand).path.lower():
                    return cand
            except Exception:
                pass

        parsed_u = urlparse(url or "")
        if parsed_u.scheme and parsed_u.netloc:
            return f"{parsed_u.scheme}://{parsed_u.netloc}/"
        return target_url or url

    def enqueue_task(
        self,
        tenant_id: str,
        service: str,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_payload: Any = None,
        target_url: str | None = None,
        reason: str = "Captcha / WAF / 2FA doğrulaması gerekiyor",
    ) -> dict[str, Any]:
        """
        Doğrulama gerektiren isteği kuyruğa (`PENDING_USER_ACTION`) ekler ve
        varsa `OWNAPI_WEBHOOK_URL` adresine HTTP POST bildirimi atar.
        """
        clean_tenant = self._sanitize(tenant_id)
        clean_svc = self._sanitize(service)
        task_id = f"hitl_{uuid.uuid4().hex[:8]}"
        now_iso = datetime.now(timezone.utc).isoformat()

        resolved_target_url = self._resolve_session_target_url(clean_svc, url, target_url)
        req_headers = dict(headers or {})
        req_payload = json_payload

        if clean_svc.lower() == "thy" or "turkishairlines.com" in (url or "").lower():
            req_headers["Referer"] = "https://www.turkishairlines.com/"
            req_headers["Origin"] = "https://www.turkishairlines.com"
            if not isinstance(req_payload, dict) or not req_payload.get("originDestinationInformationList"):
                try:
                    from registry.thy.client import build_thy_availability_payload
                    req_payload = build_thy_availability_payload()
                except Exception:
                    pass

        task_record: dict[str, Any] = {
            "task_id": task_id,
            "tenant_id": clean_tenant,
            "service": clean_svc,
            "method": method.upper(),
            "url": url,
            "target_url": resolved_target_url,
            "params": params or {},
            "headers": req_headers,
            "json_payload": req_payload,
            "reason": reason,
            "status": "PENDING_USER_ACTION",
            "created_at": now_iso,
            "updated_at": now_iso,
            "completed_at": None,
            "webhook_notified": False,
        }

        q_data = self._load_queue_data(clean_tenant)
        tasks: list[dict[str, Any]] = q_data.get("tasks", [])
        tasks.append(task_record)

        if self.webhook_url:
            task_record["webhook_notified"] = self._send_webhook_notification(task_record)

        self._save_queue_data(clean_tenant, tasks)

        console.print(
            Panel.fit(
                f"[bold yellow]⏸️ Non-Interactive HITL Kuyruğuna Eklendi![/bold yellow]\n"
                f"[white]Görev ID (task_id):[/white] [bold cyan]{task_id}[/bold cyan]\n"
                f"[white]Servis / Tenant:[/white] [magenta]{service}[/magenta] ([cyan]{clean_tenant}[/cyan])\n"
                f"[white]Neden:[/white] [yellow]{reason}[/yellow]\n"
                f"[white]Durum:[/white] [bold red]PENDING_USER_ACTION[/bold red]\n"
                f"[dim]Onaylayıp devam ettirmek için:[/dim] [bold green]own-api resume {task_id}[/bold green]",
                title="OwnAPI Human-in-the-Loop (HITL) Queue",
                border_style="yellow",
            )
        )
        return task_record

    def _send_webhook_notification(self, task_record: dict[str, Any]) -> bool:
        """OWNAPI_WEBHOOK_URL tanımlıysa görevi JSON POST olarak bildirir."""
        if not self.webhook_url:
            return False
        try:
            webhook_payload = {
                "event": "OWNAPI_HITL_PENDING_USER_ACTION",
                "task_id": task_record["task_id"],
                "tenant_id": task_record["tenant_id"],
                "service": task_record["service"],
                "method": task_record["method"],
                "url": task_record["url"],
                "reason": task_record["reason"],
                "status": task_record["status"],
                "created_at": task_record["created_at"],
                "resume_command": f"own-api resume {task_record['task_id']}",
            }
            data_bytes = json.dumps(webhook_payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                self.webhook_url,
                data=data_bytes,
                headers={"Content-Type": "application/json", "User-Agent": "OwnAPI-HITL-Webhook/1.0"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                return 200 <= resp.status < 300
        except Exception as exc:
            console.print(f"[dim yellow]⚠️ HITL Webhook bildirimi gönderilemedi ({exc})[/dim yellow]")
            return False

    def list_tasks(
        self,
        tenant_id: str | None = "default",
        status_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Belirtilen kiracının (veya tenant_id=None ise tüm kiracıların) HITL kuyruğundaki görevleri listeler.
        """
        all_tasks: list[dict[str, Any]] = []
        if tenant_id:
            q_data = self._load_queue_data(tenant_id)
            all_tasks.extend(q_data.get("tasks", []))
        else:
            for t_dir in sorted(self.sessions_root.iterdir()):
                if t_dir.is_dir() and (t_dir / "hitl_queue.json").exists():
                    q_data = self._load_queue_data(t_dir.name)
                    all_tasks.extend(q_data.get("tasks", []))

        if status_filter:
            sf = status_filter.strip().upper()
            all_tasks = [t for t in all_tasks if str(t.get("status", "")).upper() == sf]
        return all_tasks

    def get_task(self, task_id: str, tenant_id: str | None = None) -> tuple[dict[str, Any] | None, str]:
        """task_id ile görevi ve ait olduğu tenant_id'yi bulur."""
        clean_id = task_id.strip()
        if tenant_id:
            for t in self.list_tasks(tenant_id=tenant_id):
                if t.get("task_id") == clean_id:
                    return t, tenant_id
        for t in self.list_tasks(tenant_id=None):
            if t.get("task_id") == clean_id:
                return t, str(t.get("tenant_id", "default"))
        return None, tenant_id or "default"

    def resume_task(self, task_id: str, tenant_id: str | None = None) -> dict[str, Any]:
        """
        Bekleyen (`PENDING_USER_ACTION`) bir görevi bulur, kullanıcının yerel tarayıcısını
        etkileşimli (`non_interactive=False`, `mode="local"`) olarak açar, doğrulama çözüldükten
        sonra isteği kaldığı yerden yürütür ve görevi `COMPLETED` olarak günceller.
        """
        task, found_tenant = self.get_task(task_id, tenant_id=tenant_id)
        if not task:
            raise ValueError(f"Görev bulunamadı: '{task_id}'")

        console.print(
            Panel.fit(
                f"[bold cyan]▶️ HITL Görevi Devam Ettiriliyor (Resume)[/bold cyan]\n"
                f"[white]Görev ID:[/white] [bold yellow]{task['task_id']}[/bold yellow]\n"
                f"[white]Servis / Tenant:[/white] [magenta]{task['service']}[/magenta] ([cyan]{found_tenant}[/cyan])\n"
                f"[white]Uç Nokta:[/white] [green]{task['method']} {task['url']}[/green]\n"
                f"[white]Bekleme Nedeni:[/white] [yellow]{task.get('reason')}[/yellow]",
                title="OwnAPI HITL Task Resumer",
                border_style="cyan",
            )
        )

        from healer import SessionHealer

        resolved_target_url = self._resolve_session_target_url(
            task["service"],
            task["url"],
            task.get("target_url"),
        )
        req_headers = dict(task.get("headers") or {})
        req_payload = task.get("json_payload")

        if str(task.get("service", "")).lower() == "thy" or "turkishairlines.com" in str(task.get("url", "")).lower():
            req_headers["Referer"] = "https://www.turkishairlines.com/"
            req_headers["Origin"] = "https://www.turkishairlines.com"
            if not isinstance(req_payload, dict) or not req_payload.get("originDestinationInformationList"):
                try:
                    from registry.thy.client import build_thy_availability_payload
                    req_payload = build_thy_availability_payload()
                except Exception:
                    pass

        healer = SessionHealer(
            target_url=resolved_target_url,
            tenant_id=found_tenant,
            service=task["service"],
            mode="local",
            non_interactive=False,
        )

        response = healer.execute_in_browser(
            method=task["method"],
            url=task["url"],
            headers=req_headers,
            params=task.get("params") or {},
            json_payload=req_payload,
            target_url=resolved_target_url,
            tenant_id=found_tenant,
            mode="local",
            non_interactive=False,
        )

        # Kuyruk durumunu COMPLETED olarak güncelle
        q_data = self._load_queue_data(found_tenant)
        tasks = q_data.get("tasks", [])
        now_iso = datetime.now(timezone.utc).isoformat()
        for item in tasks:
            if item.get("task_id") == task["task_id"]:
                item["status"] = "COMPLETED"
                item["updated_at"] = now_iso
                item["completed_at"] = now_iso
                if isinstance(response, dict):
                    item["response_keys"] = list(response.keys())[:10]
                break

        self._save_queue_data(found_tenant, tasks)

        console.print(
            f"[bold green]✔ Görev '{task['task_id']}' başarıyla onaylandı ve tamamlandı (COMPLETED)![/bold green]"
        )
        return {
            "status": "COMPLETED",
            "task_id": task["task_id"],
            "service": task["service"],
            "tenant_id": found_tenant,
            "completed_at": now_iso,
            "response": response,
        }

    def print_queue_table(self, tenant_id: str | None = "default", status_filter: str | None = None) -> None:
        """Kuyruktaki bekleyen ve tamamlanan görevleri Rich tablosu olarak ekrana basar."""
        tasks = self.list_tasks(tenant_id=tenant_id, status_filter=status_filter)
        if not tasks:
            console.print("[yellow]HITL onay kuyruğunda kayıtlı görev bulunmuyor.[/yellow]")
            return

        table = Table(
            title=f"🛡️ OwnAPI Human-in-the-Loop (HITL) Onay Kuyruğu ({len(tasks)} Görev)",
            show_header=True,
            header_style="bold magenta",
            show_lines=True,
        )
        table.add_column("Task ID", style="bold cyan", no_wrap=True)
        table.add_column("Tenant / Servis", style="bold yellow")
        table.add_column("Metot & URL", style="white", overflow="fold", ratio=2)
        table.add_column("Neden", style="yellow", overflow="fold", ratio=1)
        table.add_column("Durum", justify="center")
        table.add_column("Oluşturulma / Tamamlanma", style="dim")

        for t in tasks:
            st = str(t.get("status", "PENDING_USER_ACTION"))
            if st == "PENDING_USER_ACTION":
                badge = "[bold red]⏳ PENDING_USER_ACTION[/bold red]"
            elif st == "COMPLETED":
                badge = "[bold green]✔ COMPLETED[/bold green]"
            else:
                badge = f"[yellow]{st}[/yellow]"

            c_at = str(t.get("created_at", ""))[:19].replace("T", " ")
            d_at = str(t.get("completed_at") or "")[:19].replace("T", " ")
            time_str = f"Oluş: {c_at}" + (f"\nBitiş: {d_at}" if d_at else "")

            table.add_row(
                str(t.get("task_id")),
                f"{t.get('tenant_id', 'default')} / [bold]{t.get('service')}[/bold]",
                f"[cyan]{t.get('method')}[/cyan] {t.get('url')}",
                str(t.get("reason", "-")),
                badge,
                time_str,
            )

        console.print()
        console.print(table)
        console.print()
