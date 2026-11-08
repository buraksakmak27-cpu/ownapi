from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import typer
from playwright.async_api import Page, Request, Response, async_playwright
from pydantic import BaseModel, Field
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console(stderr=True, legacy_windows=False)
app = typer.Typer(help="Shadow-API Compiler - AÄŸ TrafiÄŸi Kaydedici (Recorder)")

# Filtrelenecek telemetri, analitik ve reklam domain parÃ§alarÄ±
IGNORED_DOMAIN_KEYWORDS: tuple[str, ...] = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "sentry.io",
    "clarity.ms",
    "hotjar.com",
    "mixpanel.com",
    "segment.io",
    "segment.com",
    "amplitude.com",
    "datadoghq.com",
    "newrelic.com",
    "facebook.net",
    "connect.facebook.net",
    "analytics",
    "telemetry",
    "metrics",
    "beacon",
    "pixel",
    "bugsnag.com",
    "fullstory.com",
    "intercom.io",
    "crisp.chat",
)

# YakalanmasÄ± kritik olan kimlik doÄŸrulama, oturum ve tarayÄ±cÄ± parmak izi (fingerprint) baÅŸlÄ±klarÄ±
RELEVANT_HEADER_PREFIXES: tuple[str, ...] = (
    "authorization",
    "cookie",
    "x-csrf",
    "x-xsrf",
    "csrf",
    "xsrf",
    "x-api-key",
    "api-key",
    "x-algolia",
    "x-auth",
    "x-session",
    "x-token",
    "x-requested-with",
    "content-type",
    "accept",
    "user-agent",
    "referer",
    "origin",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
)


class CapturedEndpoint(BaseModel):
    """Yakalanan tekil bir XHR/Fetch API Ã§aÄŸrÄ±sÄ±nÄ±n veri modeli."""

    id: int = Field(..., description="Yakalanma sÄ±rasÄ±")
    timestamp: str = Field(..., description="UTC ISO-8601 zaman damgasÄ±")
    method: str = Field(..., description="HTTP Metodu (GET, POST, PUT, DELETE vb.)")
    url: str = Field(..., description="Tam istek URL'i (sorgu parametreleri hariÃ§ baz URL)")
    full_url: str = Field(..., description="Sorgu parametreleriyle birlikte tam URL")
    domain: str = Field(..., description="Hedef sunucu domain adÄ±")
    path: str = Field(..., description="URL yolu (endpoint path)")
    status_code: int = Field(..., description="HTTP yanÄ±t durum kodu")
    resource_type: str = Field(..., description="Playwright kaynak tipi (xhr veya fetch)")
    query_params: dict[str, Any] = Field(default_factory=dict, description="AyrÄ±ÅŸtÄ±rÄ±lmÄ±ÅŸ URL sorgu parametreleri")
    headers: dict[str, str] = Field(default_factory=dict, description="FiltrelenmiÅŸ kritik HTTP baÅŸlÄ±klarÄ±")
    request_payload: Any | None = Field(default=None, description="Varsa gÃ¶nderilen JSON veya metin gÃ¶vdesi")
    response_sample: str = Field(..., description="YanÄ±t JSON Ã¶rneÄŸi (maksimum 2000 karakter)")
    response_truncated: bool = Field(default=False, description="YanÄ±tÄ±n 2000 karakterde kesilip kesilmediÄŸi")


class CaptureSession(BaseModel):
    """TÃ¼m kayÄ±t oturumunu temsil eden kÃ¶k ÅŸema."""

    target_url: str
    captured_at: str
    total_captured: int
    endpoints: list[CapturedEndpoint]


class TrafficRecorder:
    """Playwright Ã¼zerinden XHR/Fetch trafiÄŸini dinleyen ve filtreleyen sÄ±nÄ±f."""

    def __init__(
        self,
        target_url: str,
        output_path: Path = Path("captured_traffic.json"),
        max_response_chars: int = 2000,
        profile_dir: Path = Path(".browser_profile"),
        user_wait_sec: int = 5,
    ) -> None:
        self.target_url = target_url
        self.output_path = output_path
        self.max_response_chars = max_response_chars
        self.profile_dir = profile_dir
        self.user_wait_sec = user_wait_sec
        self.captured_endpoints: list[CapturedEndpoint] = []
        self._counter: int = 0
        self._lock = asyncio.Lock()

    @staticmethod
    def _is_ignored_domain(url: str) -> bool:
        """URL'in analitik, reklam veya telemetri servislerine ait olup olmadÄ±ÄŸÄ±nÄ± kontrol eder."""
        try:
            parsed = urlparse(url)
            hostname = (parsed.hostname or "").lower()
            path = (parsed.path or "").lower()
            if not hostname:
                return True

            for keyword in IGNORED_DOMAIN_KEYWORDS:
                if keyword in hostname or f"/{keyword}/" in path:
                    return True
            return False
        except Exception:
            return True

    @staticmethod
    def _extract_relevant_headers(headers: dict[str, str]) -> dict[str, str]:
        """Sadece kimlik doÄŸrulama, CSRF, Cookie ve iÃ§erik tipiyle ilgili baÅŸlÄ±klarÄ± sÃ¼zer."""
        filtered: dict[str, str] = {}
        for key, value in headers.items():
            lower_key = key.lower()
            if any(lower_key == prefix or lower_key.startswith(prefix) for prefix in RELEVANT_HEADER_PREFIXES):
                filtered[key] = value
        return filtered

    @staticmethod
    def _parse_query_params(url: str) -> dict[str, Any]:
        """URL iÃ§indeki query string parametrelerini temiz bir sÃ¶zlÃ¼ÄŸe dÃ¶nÃ¼ÅŸtÃ¼rÃ¼r."""
        parsed = urlparse(url)
        raw_params = parse_qs(parsed.query, keep_blank_values=True)
        # Tek elemanlÄ± listeleri doÄŸrudan deÄŸere indirge
        return {k: v[0] if len(v) == 1 else v for k, v in raw_params.items()}

    @staticmethod
    def _extract_request_payload(request: Request) -> Any | None:
        """Ä°stek gÃ¶vdesini (varsa) JSON olarak, deÄŸilse dÃ¼z metin olarak Ã§Ä±karÄ±r."""
        try:
            post_data = request.post_data
            if not post_data:
                return None
            try:
                return json.loads(post_data)
            except json.JSONDecodeError:
                return post_data[:2000]
        except Exception:
            return None

    async def _handle_response(self, response: Response) -> None:
        """Her aÄŸ yanÄ±tÄ± geldiÄŸinde tetiklenen asenkron dinleyici."""
        try:
            request = response.request

            # 1. Sadece XHR ve Fetch isteklerini filtrele
            if request.resource_type not in ("xhr", "fetch"):
                return

            full_url = request.url

            # 2. Telemetri, reklam ve analitik domainlerini atla
            if self._is_ignored_domain(full_url):
                return

            # 3. YanÄ±t Content-Type baÅŸlÄ±ÄŸÄ±nÄ±n JSON olduÄŸundan emin ol
            content_type = response.headers.get("content-type", "").lower()
            if "application/json" not in content_type and "+json" not in content_type:
                return

            # 4. YanÄ±t gÃ¶vdesini gÃ¼venli ÅŸekilde oku ve geÃ§erli bir JSON olduÄŸunu doÄŸrula
            try:
                raw_text = await response.text()
            except Exception:
                # YÃ¶nlendirmeler veya tarayÄ±cÄ± kapanÄ±rken yarÄ±m kalan istekler hata fÄ±rlatabilir
                return

            if not raw_text or not raw_text.strip():
                return

            try:
                parsed_json = json.loads(raw_text)
                # Okunabilir/kompakt formatta yeniden serileÅŸtir
                normalized_json_str = json.dumps(parsed_json, ensure_ascii=False)
            except json.JSONDecodeError:
                return

            truncated = len(normalized_json_str) > self.max_response_chars
            response_sample = normalized_json_str[: self.max_response_chars]

            # Ä°stek baÅŸlÄ±klarÄ±nÄ± al (all_headers Cookie gibi hassas baÅŸlÄ±klarÄ± da iÃ§erir)
            try:
                all_req_headers = await request.all_headers()
            except Exception:
                all_req_headers = request.headers

            parsed_url = urlparse(full_url)
            base_url = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"

            async with self._lock:
                self._counter += 1
                endpoint = CapturedEndpoint(
                    id=self._counter,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    method=request.method.upper(),
                    url=base_url,
                    full_url=full_url,
                    domain=parsed_url.netloc,
                    path=parsed_url.path or "/",
                    status_code=response.status,
                    resource_type=request.resource_type,
                    query_params=self._parse_query_params(full_url),
                    headers=self._extract_relevant_headers(all_req_headers),
                    request_payload=self._extract_request_payload(request),
                    response_sample=response_sample,
                    response_truncated=truncated,
                )
                self.captured_endpoints.append(endpoint)

                console.print(
                    f"[bold green]âœ” YakalandÄ± #{endpoint.id}[/bold green] "
                    f"[cyan]{endpoint.method}[/cyan] [white]{endpoint.url}[/white] "
                    f"([yellow]{endpoint.status_code}[/yellow])"
                )

        except Exception as exc:
            console.print(f"[dim red]! Ä°stek iÅŸlenirken hata oluÅŸtu: {exc}[/dim red]")

    def save_to_disk(self) -> None:
        """Yakalanan tÃ¼m trafiÄŸi JSON ÅŸemasÄ± halinde diske yazar ve taze oturum baÅŸlÄ±klarÄ±nÄ± session_tokens.json'a aktarÄ±r."""
        now_iso = datetime.now(timezone.utc).isoformat()
        session_data = CaptureSession(
            target_url=self.target_url,
            captured_at=now_iso,
            total_captured=len(self.captured_endpoints),
            endpoints=self.captured_endpoints,
        )
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text(
            session_data.model_dump_json(indent=2),
            encoding="utf-8",
        )

        # En son yakalanan uÃ§ noktanÄ±n dinamik baÅŸlÄ±klarÄ±nÄ± session_tokens.json'a kaydet
        if self.captured_endpoints:
            latest_ep = self.captured_endpoints[-1]
            merged_headers: dict[str, str] = {}
            for ep in self.captured_endpoints:
                for hk, hv in ep.headers.items():
                    if hk.lower() not in ("accept-encoding", "accept-language", "content-length"):
                        merged_headers[hk.lower()] = hv
            merged_body_tokens: dict[str, Any] = {}
            for ep in self.captured_endpoints:
                if isinstance(ep.request_payload, dict):
                    merged_body_tokens.update(ep.request_payload)

            token_bundle = {
                "target_url": self.target_url,
                "healed_at": now_iso,
                "headers": merged_headers,
                "query_params": latest_ep.query_params,
                "body_tokens": merged_body_tokens,
                **merged_headers,
            }
            Path("session_tokens.json").write_text(
                json.dumps(token_bundle, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def print_summary_table(self) -> None:
        """Yakalanan uÃ§ noktalarÄ±n Ã¶zetini Rich tablosu olarak ekrana basar."""
        if not self.captured_endpoints:
            console.print(
                Panel(
                    "[bold yellow]HiÃ§bir geÃ§erli XHR/Fetch JSON isteÄŸi yakalanmadÄ±.[/bold yellow]",
                    title="Shadow-API Ã–zeti",
                    border_style="yellow",
                )
            )
            return

        table = Table(
            title=f"Shadow-API Yakalanan UÃ§ Noktalar ({len(self.captured_endpoints)} Adet)",
            show_header=True,
            header_style="bold magenta",
            show_lines=True,
        )
        table.add_column("#", style="dim", width=4, justify="right")
        table.add_column("Metot", style="bold cyan", width=8)
        table.add_column("Durum", width=6, justify="center")
        table.add_column("Domain", style="green", overflow="fold")
        table.add_column("Endpoint (Path)", style="bold white", overflow="fold")
        table.add_column("Sorgu / Payload", style="yellow", width=18)
        table.add_column("Kritik BaÅŸlÄ±klar", style="dim cyan", width=22)

        for ep in self.captured_endpoints:
            status_color = "green" if 200 <= ep.status_code < 300 else "yellow" if ep.status_code < 400 else "red"
            has_query = f"Query({len(ep.query_params)})" if ep.query_params else "-"
            has_body = "Body(âœ”)" if ep.request_payload is not None else "-"
            meta_info = f"{has_query} / {has_body}"
            header_keys = ", ".join(ep.headers.keys()) if ep.headers else "-"

            table.add_row(
                str(ep.id),
                ep.method,
                f"[{status_color}]{ep.status_code}[/{status_color}]",
                ep.domain,
                ep.path,
                meta_info,
                header_keys,
            )

        console.print()
        console.print(table)
        console.print(
            f"\n[bold green]ğŸ’¾ Trafik verisi baÅŸarÄ±yla kaydedildi:[/bold green] "
            f"[underline]{self.output_path.resolve()}[/underline]\n"
        )

    async def run(self) -> list[CapturedEndpoint]:
        """TarayÄ±cÄ±yÄ± baÅŸlatÄ±r, trafiÄŸi dinler ve kullanÄ±cÄ± Enter'a basana kadar bekler."""
        console.print(
            Panel.fit(
                f"[bold cyan]Hedef URL:[/bold cyan] {self.target_url}\n"
                f"[bold cyan]Ã‡Ä±ktÄ± DosyasÄ±:[/bold cyan] {self.output_path}\n\n"
                "[dim]TarayÄ±cÄ± aÃ§Ä±lÄ±yor... Ä°ÅŸlemlerinizi tamamladÄ±ktan sonra kaydÄ± bitirmek iÃ§in "
                "bu terminalde [bold white]ENTER[/bold white] tuÅŸuna basÄ±n.[/dim]",
                title="ğŸš€ Shadow-API Recorder",
                border_style="cyan",
            )
        )

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        # Headless Ã§alÄ±ÅŸmalardan kalan ekran dÄ±ÅŸÄ± (-32000) pencere konumunu sÄ±fÄ±rla
        prefs_file = self.profile_dir / "Default" / "Preferences"
        if prefs_file.exists():
            try:
                prefs_data = json.loads(prefs_file.read_text(encoding="utf-8"))
                if "browser" in prefs_data and "window_placement" in prefs_data["browser"]:
                    prefs_data["browser"].pop("window_placement", None)
                    prefs_file.write_text(json.dumps(prefs_data), encoding="utf-8")
            except Exception:
                pass

        from healer import DEFAULT_DESKTOP_UA, STEALTH_INIT_SCRIPT

        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir.resolve()),
                headless=False,
                user_agent=DEFAULT_DESKTOP_UA,
                locale="tr-TR",
                timezone_id="Europe/Istanbul",
                ignore_https_errors=True,
                accept_downloads=True,
                no_viewport=True,
                args=[
                    "--disable-http2",
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-accelerated-2d-canvas",
                    "--no-first-run",
                    "--no-service-autorun",
                    "--password-store=basic",
                    "--use-mock-keychain",
                    "--window-position=80,60",
                    "--window-size=1366,850",
                ],
            )
            await context.add_init_script(STEALTH_INIT_SCRIPT)

            page: Page = context.pages[0] if context.pages else await context.new_page()
            await page.bring_to_front()
            await page.add_init_script(STEALTH_INIT_SCRIPT)
            # TÃ¼m mevcut ve yeni aÃ§Ä±lacak sayfalarda yanÄ±tlarÄ± dinle
            for existing_page in context.pages:
                existing_page.on("response", lambda response: asyncio.create_task(self._handle_response(response)))
            context.on(
                "page",
                lambda new_p: new_p.on("response", lambda response: asyncio.create_task(self._handle_response(response))),
            )

            try:
                await page.goto(self.target_url, wait_until="commit", timeout=30_000)
            except Exception as exc:
                console.print(
                    f"[bold yellow]âš  Sayfa ilk yÃ¶nlendirmede uyarÄ± verdi ({exc}), "
                    "tarayÄ±cÄ±da manuel gezinebilirsiniz; trafik dinleme aktif...[/bold yellow]"
                )

            console.print(
                Panel.fit(
                    "[bold cyan]ğŸ‘ï¸ TarayÄ±cÄ± Penceresi Ekranda AÃ§Ä±ldÄ± (headless=False)[/bold cyan]\n"
                    "[dim]EÄŸer ekranda PerimeterX (BasÄ±lÄ± Tutun), Cloudflare Turnstile veya Captcha Ã§Ä±karsa lÃ¼tfen tarayÄ±cÄ±da tamamlayÄ±n.[/dim]\n"
                    f"[dim]KullanÄ±cÄ± kontrolÃ¼ ve sayfa stabilizasyonu iÃ§in {self.user_wait_sec} saniye bekleniyor...[/dim]",
                    title="Browser HazÄ±r",
                    border_style="cyan",
                )
            )
            if self.user_wait_sec > 0:
                await page.wait_for_timeout(self.user_wait_sec * 1000)

            console.print(
                "\n[bold reverse green] KAYIT AKTÄ°F [/bold reverse green] "
                "TarayÄ±cÄ±da etkileÅŸimde bulunun. Durdurmak iÃ§in [bold yellow]ENTER[/bold yellow]'a basÄ±n...\n"
            )

            # Bloklamadan kullanÄ±cÄ±nÄ±n Enter'a basmasÄ±nÄ± bekle
            await asyncio.to_thread(input)

            console.print("[dim]Oturum durumu (.browser_profile) kaydediliyor ve tarayÄ±cÄ± kapatÄ±lÄ±yor...[/dim]")
            try:
                await context.storage_state(path=str(self.profile_dir / "storage_state.json"))
            except Exception:
                pass
            await context.close()

        self.save_to_disk()
        self.print_summary_table()
        return self.captured_endpoints


@app.command()
def record(
    url: str = typer.Argument(..., help="Dinlenecek hedef web sitesinin URL'i (Ã¶rn: https://example.com)"),
    output: Path = typer.Option(
        Path("captured_traffic.json"),
        "--output",
        "-o",
        help="Yakalanan trafiÄŸin kaydedileceÄŸi JSON dosyasÄ±",
    ),
    max_chars: int = typer.Option(
        2000,
        "--max-chars",
        "-m",
        help="YanÄ±t JSON Ã¶rneÄŸi iÃ§in maksimum karakter sÄ±nÄ±rÄ±",
    ),
    user_wait: int = typer.Option(
        5,
        "--user-wait",
        "-w",
        help="Sayfa aÃ§Ä±ldÄ±ktan sonra kullanÄ±cÄ± doÄŸrulamasÄ± (PerimeterX vb.) iÃ§in tanÄ±nan baÅŸlangÄ±Ã§ bekleme sÃ¼resi (saniye)",
    ),
) -> None:
    """Verilen URL'yi gÃ¶rÃ¼nÃ¼r Chromium ile aÃ§ar ve XHR/Fetch JSON trafiÄŸini kaydeder."""
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    recorder = TrafficRecorder(target_url=url, output_path=output, max_response_chars=max_chars, user_wait_sec=user_wait)
    try:
        asyncio.run(recorder.run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Ä°ÅŸlem kullanÄ±cÄ± tarafÄ±ndan kesildi. Mevcut veriler kaydediliyor...[/yellow]")
        recorder.save_to_disk()
        recorder.print_summary_table()


if __name__ == "__main__":
    app()

