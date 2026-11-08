from __future__ import annotations

import asyncio
import json
import os
import time
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
HITL_STATE_FILE = PROJECT_ROOT / ".sessions" / "hitl_state.json"
PENDING_HITL_STATUSES = {
    "PAUSED",
    "PENDING_USER_ACTION",
    "PENDING_USER_APPROVAL",
    "APPROVED_TO_RESUME",
    "RESUMING",
}

console = Console(stderr=True, legacy_windows=False)


class HITLQueueManager:
    """
    Human-in-the-Loop (HITL) Kalıcı Disk Tabanlı Onay ve Pause/Resume Yöneticisi.
    WAF / Captcha / PerimeterX / Akamai / 2FA engeline takılan ajanların görevlerini
    öldürmek yerine 'PAUSED' durumuna alır ve proje kök dizinindeki
    `.sessions/hitl_state.json` dosyasına kalıcı olarak kaydeder.
    Sayfa yenilense (F5) veya sunucu yeniden başlasa dahi bekleyen onaylar
    `.sessions/hitl_state.json` üzerinden okunur; kullanıcı arayüzünden onay verildiğinde
    ajan kaldığı yerden ('resume') çalışmaya devam eder.
    """

    def __init__(
        self,
        sessions_root: Path | str = PROJECT_ROOT / ".sessions",
        webhook_url: str | None = None,
    ) -> None:
        load_dotenv(override=False)
        self.sessions_root = Path(sessions_root)
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        self.state_file = self.sessions_root / "hitl_state.json"
        self.webhook_url = (webhook_url or os.getenv("OWNAPI_WEBHOOK_URL", "")).strip() or None
        self._ensure_state_file()

    @staticmethod
    def _sanitize(value: str) -> str:
        clean = "".join(c if (c.isalnum() or c in ("-", "_")) else "_" for c in str(value or "").strip())
        return clean or "default"

    @staticmethod
    def is_pending_status(status: str | None) -> bool:
        return str(status or "").strip().upper() in PENDING_HITL_STATUSES

    def _queue_file_path(self, tenant_id: str = "default") -> Path:
        t_dir = self.sessions_root / self._sanitize(tenant_id)
        t_dir.mkdir(parents=True, exist_ok=True)
        return t_dir / "hitl_queue.json"

    def _ensure_state_file(self) -> None:
        """
        `.sessions/hitl_state.json` dosyası henüz yoksa mevcut kiracı kuyruklarını
        (`.sessions/*/hitl_queue.json`) tarayarak kalıcı state dosyasını oluşturur.
        """
        if self.state_file.exists():
            return
        migrated_tasks: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        try:
            for t_dir in sorted(self.sessions_root.iterdir()):
                if not t_dir.is_dir():
                    continue
                q_file = t_dir / "hitl_queue.json"
                if not q_file.exists():
                    continue
                try:
                    raw = json.loads(q_file.read_text(encoding="utf-8"))
                    for item in raw.get("tasks", []):
                        tid = str(item.get("task_id", "")).strip()
                        if tid and tid not in seen_ids:
                            seen_ids.add(tid)
                            migrated_tasks.append(item)
                except Exception:
                    continue
        except Exception:
            pass
        self._save_global_state(migrated_tasks)

    def _load_global_state(self) -> dict[str, Any]:
        """
        Proje kök dizinindeki `.sessions/hitl_state.json` dosyasından tüm HITL görevlerini okur.
        Kiracı klasörlerindeki `.sessions/{tenant_id}/hitl_queue.json` dosyalarında olup
        henüz ana state dosyasına yazılmamış görevler varsa onları da birleştirir.
        """
        tasks_by_id: dict[str, dict[str, Any]] = {}
        ordered_ids: list[str] = []

        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("tasks"), list):
                    for t in data["tasks"]:
                        if isinstance(t, dict) and t.get("task_id"):
                            tid = str(t["task_id"])
                            if tid not in tasks_by_id:
                                ordered_ids.append(tid)
                            tasks_by_id[tid] = t
            except Exception:
                pass

        # Kiracı bazlı dosyalardaki görevleri de senkronize et
        try:
            for t_dir in sorted(self.sessions_root.iterdir()):
                if not t_dir.is_dir():
                    continue
                q_file = t_dir / "hitl_queue.json"
                if not q_file.exists():
                    continue
                try:
                    q_data = json.loads(q_file.read_text(encoding="utf-8"))
                    for t in q_data.get("tasks", []):
                        if isinstance(t, dict) and t.get("task_id"):
                            tid = str(t["task_id"])
                            if tid not in tasks_by_id:
                                ordered_ids.append(tid)
                                tasks_by_id[tid] = t
                except Exception:
                    continue
        except Exception:
            pass

        tasks = [tasks_by_id[tid] for tid in ordered_ids if tid in tasks_by_id]
        pending_count = sum(1 for t in tasks if self.is_pending_status(t.get("status")))
        completed_count = sum(1 for t in tasks if str(t.get("status", "")).upper() == "COMPLETED")
        return {
            "state_file": str(self.state_file),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "total_tasks": len(tasks),
            "pending_count": pending_count,
            "paused_count": pending_count,
            "completed_count": completed_count,
            "tasks": tasks,
        }

    def _save_global_state(self, tasks: list[dict[str, Any]]) -> None:
        """
        Tüm görevleri hem `.sessions/hitl_state.json` dosyasına hem de ilgili kiracıların
        `.sessions/{tenant_id}/hitl_queue.json` dosyalarına kalıcı olarak yazar.
        """
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        now_iso = datetime.now(timezone.utc).isoformat()
        pending_count = sum(1 for t in tasks if self.is_pending_status(t.get("status")))
        completed_count = sum(1 for t in tasks if str(t.get("status", "")).upper() == "COMPLETED")

        global_payload = {
            "updated_at": now_iso,
            "total_tasks": len(tasks),
            "pending_count": pending_count,
            "paused_count": pending_count,
            "completed_count": completed_count,
            "tasks": tasks,
        }
        tmp_file = self.state_file.with_suffix(".json.tmp")
        try:
            tmp_file.write_text(json.dumps(global_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp_file.replace(self.state_file)
        except Exception:
            self.state_file.write_text(json.dumps(global_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        # Kiracı bazlı dosyaları da senkronize güncelle
        by_tenant: dict[str, list[dict[str, Any]]] = {}
        for t in tasks:
            ten = self._sanitize(str(t.get("tenant_id", "default")))
            by_tenant.setdefault(ten, []).append(t)

        for ten, t_list in by_tenant.items():
            q_file = self._queue_file_path(ten)
            t_payload = {
                "tenant_id": ten,
                "updated_at": now_iso,
                "total_tasks": len(t_list),
                "pending_count": sum(1 for x in t_list if self.is_pending_status(x.get("status"))),
                "tasks": t_list,
            }
            try:
                q_file.write_text(json.dumps(t_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except Exception:
                pass

    def _load_queue_data(self, tenant_id: str = "default") -> dict[str, Any]:
        clean_tenant = self._sanitize(tenant_id)
        global_data = self._load_global_state()
        tenant_tasks = [
            t for t in global_data.get("tasks", [])
            if self._sanitize(str(t.get("tenant_id", "default"))) == clean_tenant
        ]
        return {"tenant_id": clean_tenant, "tasks": tenant_tasks}

    def _save_queue_data(self, tenant_id: str, tasks: list[dict[str, Any]]) -> None:
        clean_tenant = self._sanitize(tenant_id)
        global_data = self._load_global_state()
        other_tasks = [
            t for t in global_data.get("tasks", [])
            if self._sanitize(str(t.get("tenant_id", "default"))) != clean_tenant
        ]
        merged = other_tasks + tasks
        self._save_global_state(merged)

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
        in_place_paused: bool = False,
    ) -> dict[str, Any]:
        """
        WAF/Captcha doğrulaması gerektiren ajan görevini öldürmek yerine 'PAUSED' (`PENDING_USER_ACTION`)
        durumuna alarak `.sessions/hitl_state.json` dosyasına kalıcı olarak kaydeder.
        """
        clean_tenant = self._sanitize(tenant_id)
        clean_svc = self._sanitize(service)
        task_id = f"hitl_{uuid.uuid4().hex[:8]}"
        now_iso = datetime.now(timezone.utc).isoformat()

        resolved_target_url = self._resolve_session_target_url(clean_svc, url, target_url)
        req_headers = dict(headers or {})
        req_payload = json_payload

        if clean_svc.lower() == "thy" or "turkishairlines.com" in (url or "").lower():
            req_headers["Origin"] = "https://www.turkishairlines.com"
            if not isinstance(req_payload, dict) or not req_payload.get("originDestinationInformationList"):
                try:
                    from registry.thy.client import build_thy_availability_payload
                    req_payload = build_thy_availability_payload()
                except Exception:
                    pass
            try:
                from registry.thy.client import build_thy_search_url_from_payload
                cid_val = req_headers.get("x-client-id") or req_headers.get("x-clientid")
                resolved_target_url = build_thy_search_url_from_payload(req_payload, cid=cid_val)
                req_headers["Referer"] = resolved_target_url
                req_headers["referer"] = resolved_target_url
            except Exception:
                req_headers["Referer"] = "https://www.turkishairlines.com/"

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
            "status": "PAUSED",
            "legacy_status": "PENDING_USER_ACTION",
            "agent_state": "paused",
            "in_place_paused": bool(in_place_paused),
            "created_at": now_iso,
            "paused_at": now_iso,
            "updated_at": now_iso,
            "approved_at": None,
            "completed_at": None,
            "webhook_notified": False,
            "response": None,
        }

        state = self._load_global_state()
        tasks: list[dict[str, Any]] = state.get("tasks", [])
        tasks.append(task_record)

        if self.webhook_url:
            task_record["webhook_notified"] = self._send_webhook_notification(task_record)

        self._save_global_state(tasks)

        console.print(
            Panel.fit(
                f"[bold yellow]⏸️ Ajan Görevi PAUSED Durumuna Alındı (.sessions/hitl_state.json)![/bold yellow]\n"
                f"[white]Görev ID (task_id):[/white] [bold cyan]{task_id}[/bold cyan]\n"
                f"[white]Servis / Tenant:[/white] [magenta]{service}[/magenta] ([cyan]{clean_tenant}[/cyan])\n"
                f"[white]Neden:[/white] [yellow]{reason}[/yellow]\n"
                f"[white]Durum:[/white] [bold red]PAUSED (PENDING_USER_ACTION)[/bold red]\n"
                f"[dim]Web Dashboard üzerinden veya CLI ile onaylandığında kaldığı yerden devam eder:[/dim] [bold green]own-api resume {task_id}[/bold green]",
                title="OwnAPI Human-in-the-Loop (HITL) Persistent State Store",
                border_style="yellow",
            )
        )
        return task_record

    def update_task_state(
        self,
        task_id: str,
        status: str,
        response: Any = None,
        extra_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """`.sessions/hitl_state.json` içindeki görevin durumunu ve yanıtını günceller."""
        clean_id = task_id.strip()
        state = self._load_global_state()
        tasks: list[dict[str, Any]] = state.get("tasks", [])
        now_iso = datetime.now(timezone.utc).isoformat()
        updated_task: dict[str, Any] | None = None

        for item in tasks:
            if item.get("task_id") == clean_id:
                item["status"] = status
                item["updated_at"] = now_iso
                if status.upper() == "COMPLETED":
                    item["agent_state"] = "resumed"
                    item["completed_at"] = now_iso
                    item["in_place_paused"] = False
                elif status.upper() in ("APPROVED_TO_RESUME", "RESUMING"):
                    item["agent_state"] = "resuming"
                    item["approved_at"] = item.get("approved_at") or now_iso
                elif self.is_pending_status(status):
                    item["agent_state"] = "paused"

                if response is not None:
                    item["response"] = response
                    if isinstance(response, dict):
                        item["response_keys"] = list(response.keys())[:10]

                if extra_fields:
                    item.update(extra_fields)

                updated_task = dict(item)
                break

        if updated_task is not None:
            self._save_global_state(tasks)
        return updated_task

    async def await_task_resume(
        self,
        task_id: str,
        poll_interval: float = 0.5,
        timeout_sec: float = 300.0,
    ) -> dict[str, Any] | None:
        """
        PAUSED durumuna alınan bir ajanın öldürülmeden `.sessions/hitl_state.json` dosyasını
        dinleyerek kullanıcı onayını (`APPROVED_TO_RESUME` veya `COMPLETED`) beklemesini sağlar.
        """
        deadline = time.monotonic() + max(1.0, timeout_sec)
        while time.monotonic() < deadline:
            task, _ = self.get_task(task_id, tenant_id=None)
            if not task:
                return None
            st = str(task.get("status", "")).upper()
            if st in ("APPROVED_TO_RESUME", "RESUMING", "COMPLETED", "RESUMED"):
                return task
            await asyncio.sleep(poll_interval)
        return None

    def _send_webhook_notification(self, task_record: dict[str, Any]) -> bool:
        """OWNAPI_WEBHOOK_URL tanımlıysa görevi JSON POST olarak bildirir."""
        if not self.webhook_url:
            return False
        try:
            webhook_payload = {
                "event": "OWNAPI_HITL_PAUSED",
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
        tenant_id: str | None = None,
        status_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        `.sessions/hitl_state.json` üzerinden bekleyen (`PAUSED` / `PENDING_USER_ACTION`)
        ve tamamlanan (`COMPLETED`) görevleri listeler.
        """
        state = self._load_global_state()
        all_tasks: list[dict[str, Any]] = list(state.get("tasks", []))

        if tenant_id:
            clean_tenant = self._sanitize(tenant_id)
            all_tasks = [
                t for t in all_tasks
                if self._sanitize(str(t.get("tenant_id", "default"))) == clean_tenant
            ]

        if status_filter:
            sf = status_filter.strip().upper()
            if sf in ("PENDING", "PAUSED", "PENDING_USER_ACTION", "PENDING_USER_APPROVAL"):
                all_tasks = [t for t in all_tasks if self.is_pending_status(t.get("status"))]
            else:
                all_tasks = [t for t in all_tasks if str(t.get("status", "")).upper() == sf]
        return all_tasks

    def get_pending_tasks(self, tenant_id: str | None = None) -> list[dict[str, Any]]:
        """`.sessions/hitl_state.json` içindeki onay bekleyen (`PAUSED` / `PENDING_USER_ACTION`) görevleri döndürür."""
        return self.list_tasks(tenant_id=tenant_id, status_filter="PAUSED")

    def get_task(self, task_id: str, tenant_id: str | None = None) -> tuple[dict[str, Any] | None, str]:
        """task_id ile görevi ve ait olduğu tenant_id'yi `.sessions/hitl_state.json` üzerinden bulur."""
        clean_id = task_id.strip()
        if tenant_id:
            for t in self.list_tasks(tenant_id=tenant_id):
                if t.get("task_id") == clean_id:
                    return t, self._sanitize(str(t.get("tenant_id", tenant_id)))
        for t in self.list_tasks(tenant_id=None):
            if t.get("task_id") == clean_id:
                return t, self._sanitize(str(t.get("tenant_id", "default")))
        return None, self._sanitize(tenant_id or "default")

    def resume_task(self, task_id: str, tenant_id: str | None = None) -> dict[str, Any]:
        """
        Bekleyen (`PAUSED` / `PENDING_USER_ACTION`) bir görevi onaylar ve kaldığı yerden (`resume`) devam ettirir:
        1. Eğer ajan şu an bellekte/arka planda `in_place_paused` olarak `.sessions/hitl_state.json` dosyasını
           dinliyorsa, durumu `APPROVED_TO_RESUME` yaparak ajanın kendi oturumu içinde kaldığı yerden
           devam etmesini tetikler.
        2. Eğer ajan süreci kapanmış veya sayfa/sunucu yeniden başlatılmışsa, isteği `.sessions/hitl_state.json`
           kaydından yeniden çalıştırarak tamamlar ve durumu `COMPLETED` olarak kaydeder.
        """
        task, found_tenant = self.get_task(task_id, tenant_id=tenant_id)
        if not task:
            raise ValueError(f"Görev bulunamadı: '{task_id}'")

        if str(task.get("status", "")).upper() == "COMPLETED" and task.get("response") is not None:
            return {
                "status": "COMPLETED",
                "agent_state": "resumed",
                "task_id": task["task_id"],
                "service": task["service"],
                "tenant_id": found_tenant,
                "completed_at": task.get("completed_at"),
                "response": task.get("response"),
            }

        console.print(
            Panel.fit(
                f"[bold cyan]▶️ HITL Görevi Onaylandı — Kaldığı Yerden Devam Ettiriliyor (Resume)[/bold cyan]\n"
                f"[white]Görev ID:[/white] [bold yellow]{task['task_id']}[/bold yellow]\n"
                f"[white]Servis / Tenant:[/white] [magenta]{task['service']}[/magenta] ([cyan]{found_tenant}[/cyan])\n"
                f"[white]Uç Nokta:[/white] [green]{task['method']} {task['url']}[/green]\n"
                f"[white]Bekleme Nedeni:[/white] [yellow]{task.get('reason')}[/yellow]",
                title="OwnAPI HITL Task Resumer",
                border_style="cyan",
            )
        )

        # 1. Durumu APPROVED_TO_RESUME olarak işaretle (canlı bekleyen ajan varsa anında uyanır)
        self.update_task_state(task["task_id"], status="APPROVED_TO_RESUME")

        if task.get("in_place_paused"):
            wait_deadline = time.monotonic() + 25.0
            while time.monotonic() < wait_deadline:
                fresh_task, _ = self.get_task(task["task_id"], tenant_id=found_tenant)
                if fresh_task and str(fresh_task.get("status", "")).upper() == "COMPLETED":
                    console.print(
                        f"[bold green]✔ Duraklatılmış ajan '{task['task_id']}' onay sinyalini aldı ve kaldığı yerden tamamlandı![/bold green]"
                    )
                    return {
                        "status": "COMPLETED",
                        "agent_state": "resumed",
                        "task_id": fresh_task["task_id"],
                        "service": fresh_task["service"],
                        "tenant_id": found_tenant,
                        "completed_at": fresh_task.get("completed_at"),
                        "response": fresh_task.get("response"),
                    }
                time.sleep(0.4)

        # 2. Ajan canlı beklemiyorsa veya devralınması gerekiyorsa doğrudan yürüt
        from healer import SessionHealer

        resolved_target_url = self._resolve_session_target_url(
            task["service"],
            task["url"],
            task.get("target_url"),
        )
        req_headers = dict(task.get("headers") or {})
        req_payload = task.get("json_payload")

        if str(task.get("service", "")).lower() == "thy" or "turkishairlines.com" in str(task.get("url", "")).lower():
            req_headers["Origin"] = "https://www.turkishairlines.com"
            if not isinstance(req_payload, dict) or not req_payload.get("originDestinationInformationList"):
                try:
                    from registry.thy.client import build_thy_availability_payload
                    req_payload = build_thy_availability_payload()
                except Exception:
                    pass
            try:
                from registry.thy.client import build_thy_search_url_from_payload
                cid_val = req_headers.get("x-client-id") or req_headers.get("x-clientid")
                resolved_target_url = build_thy_search_url_from_payload(req_payload, cid=cid_val)
                req_headers["Referer"] = resolved_target_url
                req_headers["referer"] = resolved_target_url
            except Exception:
                req_headers["Referer"] = "https://www.turkishairlines.com/"

        self.update_task_state(task["task_id"], status="RESUMING")

        prev_resuming = os.environ.get("OWNAPI_HITL_RESUMING")
        os.environ["OWNAPI_HITL_RESUMING"] = "1"
        try:
            healer = SessionHealer(
                target_url=resolved_target_url,
                tenant_id=found_tenant,
                service=task["service"],
                mode=str(task.get("mode") or "local"),
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
                mode=str(task.get("mode") or "local"),
                non_interactive=False,
            )
        finally:
            if prev_resuming is None:
                os.environ.pop("OWNAPI_HITL_RESUMING", None)
            else:
                os.environ["OWNAPI_HITL_RESUMING"] = prev_resuming

        updated = self.update_task_state(
            task["task_id"],
            status="COMPLETED",
            response=response,
        )
        now_iso = (updated or {}).get("completed_at") or datetime.now(timezone.utc).isoformat()

        console.print(
            f"[bold green]✔ Görev '{task['task_id']}' başarıyla onaylandı ve kaldığı yerden tamamlandı (COMPLETED)![/bold green]"
        )
        return {
            "status": "COMPLETED",
            "agent_state": "resumed",
            "task_id": task["task_id"],
            "service": task["service"],
            "tenant_id": found_tenant,
            "completed_at": now_iso,
            "response": response,
        }

    def print_queue_table(self, tenant_id: str | None = None, status_filter: str | None = None) -> None:
        """Kuyruktaki bekleyen ve tamamlanan görevleri Rich tablosu olarak ekrana basar."""
        tasks = self.list_tasks(tenant_id=tenant_id, status_filter=status_filter)
        if not tasks:
            console.print("[yellow]HITL onay kuyruğunda (.sessions/hitl_state.json) kayıtlı görev bulunmuyor.[/yellow]")
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
            st = str(t.get("status", "PAUSED")).upper()
            if self.is_pending_status(st):
                badge = f"[bold red]⏸️ {st}[/bold red]"
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
