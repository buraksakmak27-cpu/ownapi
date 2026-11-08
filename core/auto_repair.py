from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Response, async_playwright
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from compiler import RegistrySchema
from core.onboarding import (
    MAX_LLM_FIELD_CHARS,
    _compile_with_llm,
    _is_static_asset_url,
    _is_valid_json_response,
    _load_and_call_client,
    _sanitize_endpoint_for_llm,
    _select_meaningful_endpoints,
    _truncate_for_llm,
    validate_and_repair_client,
)
from core.token_store import TokenStore
from recorder import IGNORED_DOMAIN_KEYWORDS, RELEVANT_HEADER_PREFIXES

console = Console(stderr=True, legacy_windows=False)


class AutoRepairEngine:
    """
    Self-Healing 2.0: Otomatik Gece Tamiri ve Zamanlayıcı Motoru.
    - `validate_all_services(runs=1, auto_heal=True)`: `registry/` altındaki tüm aktif servisleri
      sırayla test eder.
    - Testten geçemeyen (`BAŞARISIZ`) servisler için otomatik olarak headless/stealth tarayıcıyla
      hedef URL'ye gidip güncel ağ trafiğini yeniden yakalar ve GPT-4o (`validate_and_repair_client`)
      ile `client.py` dosyasını kendi kendine onarıp 3x doğrulamadan geçirir.
    - Tüm tamir ve sağlık geçmişini `.sessions/auto_repair_report.json` dosyasına kaydeder.
    - `run_daemon(schedule_time="03:00", interval_minutes=None)` ile her gece 03:00'te veya
      belirtilen periyotta otomatik bakım yapar.
    """

    def __init__(
        self,
        registry_root: Path | str = PROJECT_ROOT / "registry",
        sessions_root: Path | str = PROJECT_ROOT / ".sessions",
        tenant_id: str = "default",
        mode: str = "local",
    ) -> None:
        self.registry_root = Path(registry_root)
        self.sessions_root = Path(sessions_root)
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        self.report_path = self.sessions_root / "auto_repair_report.json"
        self.tenant_id = tenant_id or "default"
        self.mode = mode or "local"
        self.token_store = TokenStore(sessions_dir=self.sessions_root)

    async def _auto_capture_fresh_traffic_async(
        self,
        target_url: str,
        output_path: Path,
        service_name: str,
        wait_seconds: int = 6,
    ) -> list[dict[str, Any]]:
        """
        Bozulan bir servisin hedef URL'sine kullanıcı müdahalesi olmadan (otomatik headless/stealth)
        giderek güncel XHR/Fetch ağ trafiğini ve taze çerezleri yakalar.
        """
        if service_name.lower() == "thy" or "turkishairlines.com" in (target_url or "").lower():
            target_url = "https://www.turkishairlines.com/tr-tr/"
        else:
            parsed_t = urlparse(target_url or "")
            if "/api/" in parsed_t.path.lower() and parsed_t.scheme and parsed_t.netloc:
                target_url = f"{parsed_t.scheme}://{parsed_t.netloc}/"

        profile_dir = PROJECT_ROOT / ".browser_profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        captured_endpoints: list[dict[str, Any]] = []
        endpoint_counter = 0

        console.print(
            f"[bold cyan]🕵️ Self-Healing 2.0 Otomatik Trafik Yakalama:[/bold cyan] "
            f"[white]{target_url}[/white] ({wait_seconds} sn dinleniyor...)"
        )

        async with async_playwright() as p:
            launch_kwargs: dict[str, Any] = {
                "user_data_dir": str(profile_dir.resolve()),
                "headless": True,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                ],
            }
            context = await p.chromium.launch_persistent_context(**launch_kwargs)

            stored_pw_cookies = self.token_store.get_playwright_cookies(
                tenant_id=self.tenant_id,
                service=service_name,
                default_url=target_url,
            )
            if stored_pw_cookies:
                try:
                    await context.add_cookies(stored_pw_cookies)
                except Exception:
                    pass

            page = context.pages[0] if context.pages else await context.new_page()

            async def on_response(res: Response) -> None:
                nonlocal endpoint_counter
                try:
                    req = res.request
                    if req.resource_type not in ("xhr", "fetch"):
                        return
                    req_url = req.url
                    if _is_static_asset_url(req_url):
                        return
                    try:
                        parsed_url = urlparse(req_url)
                        hostname = (parsed_url.hostname or "").lower()
                    except Exception:
                        return
                    if any(ign in hostname for ign in IGNORED_DOMAIN_KEYWORDS):
                        return

                    try:
                        raw_headers = dict(req.headers)
                    except (UnicodeDecodeError, Exception):
                        raw_headers = {}

                    filtered_headers: dict[str, str] = {}
                    for k, v in raw_headers.items():
                        lk = k.lower().strip()
                        if any(lk == pref or lk.startswith(pref) for pref in RELEVANT_HEADER_PREFIXES):
                            filtered_headers[lk] = v

                    payload: Any = None
                    try:
                        raw_post = req.post_data
                        if raw_post:
                            clipped_post = raw_post[:MAX_LLM_FIELD_CHARS]
                            try:
                                payload = _truncate_for_llm(json.loads(raw_post), max_chars=MAX_LLM_FIELD_CHARS)
                            except Exception:
                                payload = clipped_post
                    except (UnicodeDecodeError, Exception):
                        payload = None

                    sample = ""
                    is_truncated = False
                    try:
                        text = await res.text()
                        if text:
                            sample = text[:MAX_LLM_FIELD_CHARS]
                            is_truncated = len(text) > MAX_LLM_FIELD_CHARS
                    except (UnicodeDecodeError, Exception):
                        sample = ""

                    endpoint_counter += 1
                    captured_endpoints.append({
                        "id": endpoint_counter,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "method": req.method.upper(),
                        "url": f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}",
                        "full_url": req_url,
                        "domain": hostname,
                        "path": parsed_url.path,
                        "status_code": res.status,
                        "resource_type": req.resource_type,
                        "query_params": parse_qs(parsed_url.query),
                        "headers": filtered_headers,
                        "request_payload": payload,
                        "response_sample": sample,
                        "response_truncated": is_truncated,
                    })
                except (UnicodeDecodeError, Exception):
                    return

            page.on("response", on_response)

            try:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=20_000)
            except Exception as exc:
                console.print(f"[dim]Otomatik gezinme uyarısı ({service_name}): {exc}[/dim]")

            await asyncio.sleep(wait_seconds)

            if service_name.lower() == "thy":
                try:
                    from registry.thy.client import _build_validate_payload, build_thy_availability_payload
                    thy_body = build_thy_availability_payload()
                    thy_val = _build_validate_payload(thy_body)
                    await page.evaluate(
                        """async ({ valBody, mainBody }) => {
                            const hdrs = {
                                'Accept': 'application/json, text/plain, */*',
                                'Content-Type': 'application/json',
                                'Referer': 'https://www.turkishairlines.com/',
                                'Origin': 'https://www.turkishairlines.com',
                                'x-country': 'TR',
                                'x-language': 'tr'
                            };
                            try {
                                await fetch('https://www.turkishairlines.com/api/v1/availability/validate', {
                                    method: 'POST',
                                    headers: hdrs,
                                    credentials: 'include',
                                    referrer: 'https://www.turkishairlines.com/',
                                    body: JSON.stringify(valBody)
                                });
                            } catch (_) {}
                            try {
                                await fetch('https://www.turkishairlines.com/api/v1/availability', {
                                    method: 'POST',
                                    headers: hdrs,
                                    credentials: 'include',
                                    referrer: 'https://www.turkishairlines.com/',
                                    body: JSON.stringify(mainBody)
                                });
                            } catch (_) {}
                        }""",
                        {"valBody": thy_val, "mainBody": thy_body},
                    )
                    await asyncio.sleep(1.5)
                except Exception:
                    pass

            try:
                cookies = await context.cookies()
                user_agent = await page.evaluate("() => navigator.userAgent")
                self.token_store.save_session(
                    tenant_id=self.tenant_id,
                    service=service_name,
                    cookies=cookies,
                    user_agent=user_agent,
                    metadata={"target_url": target_url, "auto_healed_at": datetime.now(timezone.utc).isoformat()},
                )
            except Exception:
                pass

            try:
                await context.close()
            except Exception:
                pass

        if captured_endpoints:
            traffic_session = {
                "target_url": target_url,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "total_endpoints": len(captured_endpoints),
                "endpoints": captured_endpoints,
            }
            output_path.write_text(json.dumps(traffic_session, ensure_ascii=False, indent=2), encoding="utf-8")
            console.print(
                f"[bold green]✔ {service_name}: {len(captured_endpoints)} güncel XHR/Fetch uç noktası yakalandı.[/bold green]"
            )
        return captured_endpoints

    def _run_auto_capture(self, target_url: str, output_path: Path, service_name: str) -> list[dict[str, Any]]:
        try:
            return asyncio.run(self._auto_capture_fresh_traffic_async(target_url, output_path, service_name))
        except RuntimeError:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(
                    asyncio.run,
                    self._auto_capture_fresh_traffic_async(target_url, output_path, service_name),
                ).result()

    def validate_all_services(
        self,
        runs: int = 1,
        auto_heal: bool = True,
        services: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        registry/ altındaki tüm aktif servisleri (veya `services` listesini) sırayla test eder.
        Hata alan servisleri otomatik trafik yakalama + GPT-4o 3x doğrulama döngüsüyle tamir eder
        ve raporu `.sessions/auto_repair_report.json` dosyasına yazar.
        """
        if not self.registry_root.exists():
            return {"total_services": 0, "healthy": 0, "repaired": 0, "failed": 0, "results": []}

        service_dirs = sorted([
            d for d in self.registry_root.iterdir()
            if d.is_dir() and (d / "schema.json").exists() and (d / "client.py").exists()
        ])
        if services:
            wanted = {s.strip().lower() for s in services}
            service_dirs = [d for d in service_dirs if d.name.lower() in wanted]

        console.print(
            Panel.fit(
                f"[bold cyan]🛠️ Self-Healing 2.0: Toplu Servis Doğrulama & Otomatik Tamir Motoru[/bold cyan]\n"
                f"[white]Taranacak Servis Sayısı:[/white] [bold yellow]{len(service_dirs)}[/bold yellow] | "
                f"[white]Test Tekrarı:[/white] [cyan]{runs}x[/cyan] | "
                f"[white]Otomatik Tamir (Auto-Heal):[/white] [green]{'AKTİF (3x Doğrulama)' if auto_heal else 'KAPALI'}[/green]",
                title="OwnAPI AutoRepairEngine",
                border_style="cyan",
            )
        )

        # Arka plan taramasında input() kilitlenmesini önlemek için OWNAPI_NON_INTERACTIVE=1 aç
        prev_ni = os.environ.get("OWNAPI_NON_INTERACTIVE")
        os.environ["OWNAPI_NON_INTERACTIVE"] = "1"

        service_reports: list[dict[str, Any]] = []
        healthy_count = 0
        repaired_count = 0
        pending_hitl_count = 0
        failed_count = 0

        try:
            for sdir in service_dirs:
                svc_name = sdir.name
                schema_path = sdir / "schema.json"
                client_path = sdir / "client.py"
                traffic_path = sdir / "captured_traffic.json"

                try:
                    raw_schema = json.loads(schema_path.read_text(encoding="utf-8"))
                    schema_obj = RegistrySchema.model_validate(raw_schema)
                except Exception as exc:
                    failed_count += 1
                    service_reports.append({
                        "service_name": svc_name,
                        "initial_status": "BAŞARISIZ",
                        "final_status": "BAŞARISIZ",
                        "auto_healed": False,
                        "detail": f"Şema okuma hatası: {exc}",
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                    })
                    continue

                console.print(f"\n[bold magenta]🔍 [{svc_name}][/bold magenta] `{schema_obj.function_name}()` test ediliyor...")
                passed = 0
                last_detail = ""
                is_pending_hitl = False
                hitl_task_id = None
                start_t = time.perf_counter()

                for r_idx in range(1, runs + 1):
                    try:
                        res = _load_and_call_client(
                            client_path=client_path,
                            function_name=schema_obj.function_name,
                            tenant_id=self.tenant_id,
                            mode=self.mode,
                        )
                        if isinstance(res, dict) and res.get("status") == "PENDING_USER_APPROVAL":
                            is_pending_hitl = True
                            hitl_task_id = res.get("task_id")
                            last_detail = f"HITL Kuyruğunda Onay Bekliyor (task_id={hitl_task_id})"
                            break

                        ok, detail = _is_valid_json_response(res)
                        if ok:
                            passed += 1
                            last_detail = detail
                        else:
                            last_detail = detail
                            break
                    except Exception as exc:
                        last_detail = f"Çalışma zamanı hatası: {exc}"
                        break

                elapsed_ms = round((time.perf_counter() - start_t) * 1000.0, 2)

                if is_pending_hitl:
                    pending_hitl_count += 1
                    service_reports.append({
                        "service_name": svc_name,
                        "function_name": schema_obj.function_name,
                        "initial_status": "PENDING_USER_APPROVAL",
                        "final_status": "PENDING_USER_APPROVAL",
                        "hitl_task_id": hitl_task_id,
                        "auto_healed": False,
                        "elapsed_ms": elapsed_ms,
                        "detail": last_detail,
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                    })
                    continue

                if passed == runs:
                    healthy_count += 1
                    raw_schema["validation_status"] = "BAŞARILI"
                    raw_schema["last_validated_at"] = datetime.now(timezone.utc).isoformat()
                    schema_path.write_text(json.dumps(raw_schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    service_reports.append({
                        "service_name": svc_name,
                        "function_name": schema_obj.function_name,
                        "initial_status": "BAŞARILI",
                        "final_status": "BAŞARILI",
                        "auto_healed": False,
                        "elapsed_ms": elapsed_ms,
                        "detail": last_detail,
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                    })
                    console.print(f"  [bold green]✔ {svc_name} Sağlıklı ({elapsed_ms:.0f} ms)[/bold green] — {last_detail}")
                    continue

                # Servis testten geçemedi -> Auto-Heal (Trafik Yakalama + GPT-4o Onarım + 3x Doğrulama)
                console.print(f"  [bold red]✖ {svc_name} Test Başarısız:[/bold red] {last_detail}")
                raw_schema["validation_status"] = "BAŞARISIZ"

                if not auto_heal:
                    failed_count += 1
                    schema_path.write_text(json.dumps(raw_schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    service_reports.append({
                        "service_name": svc_name,
                        "function_name": schema_obj.function_name,
                        "initial_status": "BAŞARISIZ",
                        "final_status": "BAŞARISIZ",
                        "auto_healed": False,
                        "elapsed_ms": elapsed_ms,
                        "detail": last_detail,
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                    })
                    continue

                console.print(f"  [bold yellow]🔧 [{svc_name}] Self-Healing 2.0 Otomatik Onarım Başlatılıyor...[/bold yellow]")
                endpoints: list[dict[str, Any]] = []
                if traffic_path.exists():
                    try:
                        tdata = json.loads(traffic_path.read_text(encoding="utf-8"))
                        endpoints = tdata.get("endpoints", [])
                    except Exception:
                        endpoints = []

                # Hedef URL'ye gidip taze trafik yakala
                fresh_eps = self._run_auto_capture(schema_obj.target_url, traffic_path, svc_name)
                if fresh_eps:
                    endpoints = fresh_eps

                meaningful = _select_meaningful_endpoints(endpoints)
                raw_matched = next(
                    (e for e in meaningful if e.get("url") == schema_obj.endpoint_url),
                    meaningful[0] if meaningful else (endpoints[0] if endpoints else {"url": schema_obj.endpoint_url, "method": schema_obj.method}),
                )
                matched_ep = _sanitize_endpoint_for_llm(raw_matched, max_chars=MAX_LLM_FIELD_CHARS)

                # Mevcut client.py dosyasında SyntaxError varsa önce _compile_with_llm ile temiz yeniden derle
                try:
                    import ast

                    ast.parse(client_path.read_text(encoding="utf-8"))
                except Exception:
                    try:
                        recompiled_code, recompiled_schema, matched_ep = _compile_with_llm(
                            svc_name,
                            schema_obj.target_url,
                            schema_obj.goal,
                            meaningful or endpoints,
                        )
                        schema_obj.function_name = recompiled_schema.function_name
                        client_path.write_text(recompiled_code.strip() + "\n", encoding="utf-8")
                    except Exception:
                        pass

                repair_report = validate_and_repair_client(
                    service_name=svc_name,
                    target_url=schema_obj.target_url,
                    description=schema_obj.goal,
                    client_path=client_path,
                    schema_obj=schema_obj,
                    matched_endpoint=matched_ep,
                    all_endpoints=meaningful or endpoints,
                    validation_runs=3,
                    max_repair_attempts=3,
                    tenant_id=self.tenant_id,
                    mode=self.mode,
                )

                final_st = repair_report.get("validation_status", "BAŞARISIZ")
                if final_st == "BAŞARILI":
                    repaired_count += 1
                    raw_schema["validation_status"] = "BAŞARILI"
                    raw_schema["validation_passed_runs"] = 3
                else:
                    failed_count += 1
                    raw_schema["validation_status"] = "BAŞARISIZ"

                raw_schema["last_validated_at"] = datetime.now(timezone.utc).isoformat()
                schema_path.write_text(json.dumps(raw_schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

                service_reports.append({
                    "service_name": svc_name,
                    "function_name": schema_obj.function_name,
                    "initial_status": "BAŞARISIZ",
                    "final_status": final_st,
                    "auto_healed": final_st == "BAŞARILI",
                    "repair_attempts": repair_report.get("repair_attempts", 0),
                    "elapsed_ms": elapsed_ms,
                    "detail": "3/3 Auto-Repair Başarılı" if final_st == "BAŞARILI" else repair_report.get("last_error", last_detail),
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                })
        finally:
            if prev_ni is None:
                os.environ.pop("OWNAPI_NON_INTERACTIVE", None)
            else:
                os.environ["OWNAPI_NON_INTERACTIVE"] = prev_ni

        summary_report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tenant_id": self.tenant_id,
            "mode": self.mode,
            "total_services": len(service_dirs),
            "healthy": healthy_count,
            "repaired": repaired_count,
            "pending_hitl": pending_hitl_count,
            "failed": failed_count,
            "results": service_reports,
        }

        self._append_report_to_disk(summary_report)
        self._print_summary_table(summary_report)
        return summary_report

    def _append_report_to_disk(self, report: dict[str, Any]) -> None:
        """Tamir ve sağlık geçmişini `.sessions/auto_repair_report.json` dosyasına zaman damgasıyla kaydeder."""
        history: list[dict[str, Any]] = []
        if self.report_path.exists():
            try:
                existing = json.loads(self.report_path.read_text(encoding="utf-8"))
                if isinstance(existing, dict) and isinstance(existing.get("history"), list):
                    history = existing["history"]
            except Exception:
                history = []

        history.append(report)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "total_runs": len(history),
            "latest_summary": {
                "timestamp": report["timestamp"],
                "total_services": report["total_services"],
                "healthy": report["healthy"],
                "repaired": report["repaired"],
                "pending_hitl": report["pending_hitl"],
                "failed": report["failed"],
            },
            "history": history[-50:],
        }
        self.report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _print_summary_table(self, report: dict[str, Any]) -> None:
        table = Table(
            title=(
                f"🩺 OwnAPI Self-Healing 2.0 Sağlık & Tamir Raporu "
                f"(Sağlıklı: {report['healthy']} | Onarılan: {report['repaired']} | "
                f"HITL Bekleyen: {report['pending_hitl']} | Hatalı: {report['failed']})"
            ),
            show_header=True,
            header_style="bold magenta",
            show_lines=True,
        )
        table.add_column("Servis", style="bold cyan", no_wrap=True)
        table.add_column("İlk Durum", justify="center")
        table.add_column("Son Durum", justify="center")
        table.add_column("Otomatik Tamir", justify="center")
        table.add_column("Süre", justify="right", style="yellow")
        table.add_column("Açıklama / Detay", style="white", overflow="fold")

        for item in report.get("results", []):
            init_st = item.get("initial_status", "-")
            fin_st = item.get("final_status", "-")
            init_badge = "[green]🟢 BAŞARILI[/green]" if init_st == "BAŞARILI" else (
                "[yellow]⏳ PENDING_HITL[/yellow]" if init_st == "PENDING_USER_APPROVAL" else "[red]🔴 BAŞARISIZ[/red]"
            )
            fin_badge = "[bold green]🟢 BAŞARILI[/bold green]" if fin_st == "BAŞARILI" else (
                "[bold yellow]⏳ PENDING_HITL[/bold yellow]" if fin_st == "PENDING_USER_APPROVAL" else "[bold red]🔴 BAŞARISIZ[/bold red]"
            )
            healed_badge = "[bold cyan]🛠️ Onarıldı (3/3)[/bold cyan]" if item.get("auto_healed") else "[dim]-[/dim]"
            ms_str = f"{item.get('elapsed_ms', 0):.0f} ms"
            table.add_row(
                str(item.get("service_name")),
                init_badge,
                fin_badge,
                healed_badge,
                ms_str,
                str(item.get("detail", ""))[:140],
            )

        console.print()
        console.print(table)
        console.print(f"[dim]Rapor kaydedildi: {self.report_path}[/dim]\n")

    @staticmethod
    def _seconds_until_schedule(schedule_time: str = "03:00") -> float:
        """HH:MM formatındaki hedef saate kalan saniyeyi hesaplar."""
        now = datetime.now()
        try:
            hour, minute = [int(x) for x in schedule_time.strip().split(":", 1)]
        except Exception:
            hour, minute = 3, 0
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return max(1.0, (target - now).total_seconds())

    def run_daemon(
        self,
        schedule_time: str = "03:00",
        interval_minutes: float | None = None,
        runs: int = 1,
        auto_heal: bool = True,
        run_immediately: bool = False,
        max_cycles: int | None = None,
    ) -> None:
        """
        Her gece `schedule_time` (varsayılan '03:00') saatinde veya `interval_minutes`
        dakika periyodunda çalışan hafif arka plan bakım ve otomatik tamir zamanlayıcısı.
        """
        schedule_desc = (
            f"Her {interval_minutes} dakikada bir"
            if interval_minutes
            else f"Her gece saat {schedule_time}'te"
        )
        console.print(
            Panel.fit(
                f"[bold green]🌙 OwnAPI Self-Healing 2.0 Gece Bakım Daemon'u Başlatıldı[/bold green]\n"
                f"[white]Zamanlama:[/white] [bold cyan]{schedule_desc}[/bold cyan]\n"
                f"[white]Otomatik Tamir (GPT-4o + 3x Auto-Test):[/white] [green]{'Aktif' if auto_heal else 'Pasif'}[/green]\n"
                f"[white]Rapor Dosyası:[/white] [dim]{self.report_path}[/dim]\n"
                "[dim]Durdurmak için CTRL+C tuşlarına basın.[/dim]",
                title="OwnAPI Auto-Repair Daemon",
                border_style="green",
            )
        )

        cycle = 0
        if run_immediately:
            cycle += 1
            console.print(f"\n[bold cyan]⚡ İlk bakım döngüsü (#{cycle}) hemen çalıştırılıyor...[/bold cyan]")
            self.validate_all_services(runs=runs, auto_heal=auto_heal)
            if max_cycles and cycle >= max_cycles:
                return

        try:
            while True:
                if interval_minutes is not None and interval_minutes > 0:
                    wait_sec = float(interval_minutes) * 60.0
                else:
                    wait_sec = self._seconds_until_schedule(schedule_time)

                next_run_dt = datetime.now() + timedelta(seconds=wait_sec)
                console.print(
                    f"[dim]⏳ Sonraki otomatik bakım zamanı: "
                    f"{next_run_dt.strftime('%Y-%m-%d %H:%M:%S')} ({wait_sec / 60.0:.1f} dk sonra)...[/dim]"
                )
                time.sleep(wait_sec)

                cycle += 1
                console.print(
                    f"\n[bold cyan]🛠️ Zamanlanmış Gece Bakımı Döngüsü (#{cycle}) Başladı "
                    f"({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})[/bold cyan]"
                )
                self.validate_all_services(runs=runs, auto_heal=auto_heal)

                if max_cycles and cycle >= max_cycles:
                    break
        except KeyboardInterrupt:
            console.print("\n[yellow]⏹️ OwnAPI Auto-Repair Daemon kullanıcı tarafından durduruldu.[/yellow]")
