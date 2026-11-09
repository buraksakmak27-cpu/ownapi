from __future__ import annotations

import asyncio
import copy
import json
import os
import subprocess
import sys
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from playwright.async_api import Page, Request, Response, async_playwright
from rich.console import Console
from rich.panel import Panel

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from recorder import IGNORED_DOMAIN_KEYWORDS
from core.token_store import TokenStore

console = Console(stderr=True, legacy_windows=False)

# Yenilenmesi gereken kritik kimlik doÄŸrulama, oturum ve dinamik tarayÄ±cÄ± baÅŸlÄ±ÄŸÄ± Ã¶nekleri
HEALABLE_HEADER_PREFIXES: tuple[str, ...] = (
    "cookie",
    "authorization",
    "x-csrf-token",
    "x-csrf",
    "x-xsrf",
    "csrf",
    "xsrf",
    "x-algolia",
    "x-api-key",
    "api-key",
    "x-auth",
    "x-session",
    "x-token",
    "x-requested-with",
    "user-agent",
    "referer",
    "origin",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
    "x-px",
    "px-",
    "_px",
    "pxcts",
    "x-perimeterx",
    "tafsessionid",
    "taftrackingid",
    "x-current-page-url",
)

# URL query string veya JSON body iÃ§inde taÅŸÄ±nabilen token/oturum anahtarÄ± Ã¶nekleri
HEALABLE_TOKEN_KEYS: tuple[str, ...] = (
    "x-algolia",
    "api_key",
    "apikey",
    "api-key",
    "token",
    "access_token",
    "auth",
    "csrf",
    "xsrf",
    "sessionid",
    "session_id",
    "_px",
    "px",
    "taf",
    "amadeus",
)

PROJECT_ROOT: Path = Path(__file__).resolve().parent


def _clean_profile_dir(profile_dir: Path) -> None:
    """Eski kirlenmiÅŸ Akamai blok Ã§erezlerini ve geÃ§ersiz oturum durumunu temizler."""
    if not profile_dir.exists():
        profile_dir.mkdir(parents=True, exist_ok=True)
        return
    targets = [
        profile_dir / "storage_state.json",
        profile_dir / "Default" / "Network" / "Cookies",
        profile_dir / "Default" / "Network" / "Cookies-journal",
        profile_dir / "Default" / "Cookies",
        profile_dir / "Default" / "Cookies-journal",
    ]
    for item in targets:
        if item.exists():
            try:
                item.unlink()
            except Exception:
                pass


def _root_domain(hostname: str) -> str:
    """Subdomain farklarÄ±nÄ± yoksaymak iÃ§in kÃ¶k domain adÄ±nÄ± dÃ¶ndÃ¼rÃ¼r (Ã¶rn: web.flypgs.com -> flypgs.com)."""
    parts = hostname.lower().strip(".").split(".")
    if len(parts) >= 3 and parts[-2] in ("com", "org", "net", "gov", "edu", "co"):
        return ".".join(parts[-3:])
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return hostname.lower()


def _extract_response_text(res: Any) -> str:
    """
    YanÄ±t nesnesi zaten bir dict (sÃ¶zlÃ¼k) ise `.text` Ã¶zniteliÄŸi aramaya kalkmadan
    doÄŸrudan `res.get('text')` veya `json.dumps(res)` olarak gÃ¼venli biÃ§imde metne Ã§evirir.
    """
    if res is None:
        return ""
    if isinstance(res, str):
        return res
    if isinstance(res, dict):
        if "text" in res and isinstance(res["text"], str):
            return res["text"]
        if "response_sample" in res and isinstance(res["response_sample"], str):
            return res["response_sample"]
        try:
            return json.dumps(res, ensure_ascii=False)
        except Exception:
            return str(res)
    if isinstance(res, list):
        try:
            return json.dumps(res, ensure_ascii=False)
        except Exception:
            return str(res)
    if hasattr(res, "text"):
        attr = getattr(res, "text")
        return attr() if callable(attr) else str(attr)
    return str(res)


class ResponseDict(dict):
    """
    Hem standart Python sÃ¶zlÃ¼ÄŸÃ¼ (`dict`) olarak Ã§alÄ±ÅŸan hem de istemci kodlarÄ±nda
    `response.text`, `response.json()` veya `response['response_sample']` eriÅŸimlerini
    AttributeError / KeyError fÄ±rlatmadan destekleyen hibrit sÃ¶zlÃ¼k sÄ±nÄ±fÄ±.
    """

    @property
    def text(self) -> str:
        return _extract_response_text(dict(self))

    @property
    def status_code(self) -> int:
        val = self.get("status_code", self.get("status", 200))
        return int(val) if isinstance(val, (int, float)) else 200

    @property
    def ok(self) -> bool:
        return bool(self.get("ok", 200 <= self.status_code < 400))

    def json(self) -> dict[str, Any]:
        return dict(self)

    def __getitem__(self, key: Any) -> Any:
        if key not in self and key in ("text", "response_sample", "content"):
            return self.text
        return super().__getitem__(key)


def _merge_cookie_strings(base_cookie: str, new_cookie: str) -> str:
    """Ä°ki Cookie baÅŸlÄ±ÄŸÄ± dizgesini anahtar bazÄ±nda birleÅŸtirir (yeni deÄŸerler eskileri gÃ¼nceller)."""
    cookie_map: dict[str, str] = {}
    for raw in (base_cookie, new_cookie):
        if not raw:
            continue
        for part in raw.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                cookie_map[k.strip()] = v.strip()
    return "; ".join(f"{k}={v}" for k, v in cookie_map.items())


def update_payload_deep(payload: Any, updates: dict[str, Any]) -> Any:
    """
    Ä°stek gÃ¶vdesindeki (body) hem Ã¼st dÃ¼zey hem de iÃ§ iÃ§e (nested, Ã¶rn: flightSearchList[0])
    alanlarÄ± verilen anahtar-deÄŸer Ã§iftleriyle gÃ¼nceller.
    """
    if not isinstance(payload, (dict, list)) or not updates:
        return payload

    updated = copy.deepcopy(payload)

    def _walk(node: Any) -> bool:
        matched_any = False
        if isinstance(node, dict):
            for k in list(node.keys()):
                if k in updates and updates[k] is not None:
                    node[k] = updates[k]
                    matched_any = True
                if isinstance(node[k], (dict, list)):
                    if _walk(node[k]):
                        matched_any = True
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    if _walk(item):
                        matched_any = True
        return matched_any

    _walk(updated)
    # EÄŸer iÃ§ iÃ§e hiÃ§bir yerde eÅŸleÅŸmediyse ve Ã¼st dÃ¼zey dict ise Ã¼st dÃ¼zeye de ekle
    if isinstance(updated, dict):
        for uk, uv in updates.items():
            if uv is not None and uk in updated:
                updated[uk] = uv
    return updated


def _find_chrome_executable() -> str:
    """KullanÄ±cÄ±nÄ±n sistemindeki orijinal Google Chrome yÃ¼rÃ¼tÃ¼lebilir dosya yolunu bulur."""
    candidates = [
        os.environ.get("PROGRAMFILES", r"C:\Program Files") + r"\Google\Chrome\Application\chrome.exe",
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)") + r"\Google\Chrome\Application\chrome.exe",
        os.environ.get("LOCALAPPDATA", "") + r"\Google\Chrome\Application\chrome.exe",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return "chrome"


async def _ensure_cdp_chrome(
    profile_dir: Path,
    port: int = 9222,
    initial_url: str = "https://www.turkishairlines.com/tr-tr/",
) -> None:
    """EÄŸer portta aÃ§Ä±k Chrome oturumu yoksa kullanÄ±cÄ±nÄ±n normal Chrome uygulamasÄ±nÄ± remote debugging portuyla baÅŸlatÄ±r."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.5) as resp:
            if resp.status == 200:
                return
    except Exception:
        pass

    chrome_exe = _find_chrome_executable()
    profile_dir.mkdir(parents=True, exist_ok=True)
    console.print(
        Panel.fit(
            f"[bold cyan]ğŸš€ KullanÄ±cÄ±nÄ±n GerÃ§ek Google Chrome TarayÄ±cÄ±sÄ± BaÅŸlatÄ±lÄ±yor (CDP Port {port})[/bold cyan]\n"
            f"[dim]Yol:[/dim] [white]{chrome_exe}[/white]\n"
            f"[dim]Hedef:[/dim] [green]{initial_url}[/green]",
            title="Chrome DevTools Protocol (CDP)",
            border_style="cyan",
        )
    )
    subprocess.Popen(
        [
            chrome_exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir.resolve()}",
            "--no-first-run",
            "--no-default-browser-check",
            initial_url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(30):
        await asyncio.sleep(0.5)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.5) as resp:
                if resp.status == 200:
                    break
        except Exception:
            pass


class SessionHealer:
    """
    Oturum Ã§erezleri (Cookie), CSRF, Authorization veya dinamik baÅŸlÄ±klar sÃ¼resi dolduÄŸunda
    (401, 403 veya 429 hatalarÄ±nda) hedef siteyi ziyaret ederek taze kimlik bilgilerini
    yakalayan, session_tokens.json dosyasÄ±na kaydeden ve isteÄŸi taze oturum tÃ¼neliyle
    tekrarlayan Self-Healing motoru.
    """

    def __init__(
        self,
        traffic_path: Path | str = PROJECT_ROOT / "captured_traffic.json",
        tokens_path: Path | str = PROJECT_ROOT / "session_tokens.json",
        profile_dir: Path | str = PROJECT_ROOT / ".browser_profile",
        timeout_ms: int = 15_000,
        user_wait_sec: int = 5,
        challenge_wait_sec: int = 45,
        target_url: str | None = None,
        tenant_id: str = "default",
        service: str = "thy",
        token_store: TokenStore | None = None,
        mode: str = "local",
        cloud_cdp_url: str | None = None,
        non_interactive: bool = False,
    ) -> None:
        self.traffic_path = Path(traffic_path)
        self.tokens_path = Path(tokens_path)
        self.profile_dir = Path(profile_dir)
        self.timeout_ms = timeout_ms
        self.user_wait_sec = user_wait_sec
        self.challenge_wait_sec = challenge_wait_sec
        self.target_url = target_url
        self.tenant_id = tenant_id or "default"
        self.service = service or "thy"
        self.token_store = token_store or TokenStore(legacy_tokens_path=self.tokens_path)
        self.mode = (mode or os.getenv("OWNAPI_BROWSER_MODE", "local")).strip().lower()
        if self.mode not in ("local", "cloud"):
            self.mode = "local"
        self.cloud_cdp_url = cloud_cdp_url or os.getenv("OWNAPI_CLOUD_CDP_URL", "").strip() or None
        self.non_interactive = bool(
            non_interactive
            or os.getenv("OWNAPI_NON_INTERACTIVE", "").strip().lower() in ("1", "true", "yes")
        )

    @staticmethod
    def _reset_window_placement(profile_path: Path) -> None:
        """KalÄ±cÄ± profilde kaydedilmiÅŸ ekran dÄ±ÅŸÄ± / simge durumundaki pencere konumunu temizler."""
        prefs_file = profile_path / "Default" / "Preferences"
        if prefs_file.exists():
            try:
                prefs_data = json.loads(prefs_file.read_text(encoding="utf-8"))
                if "browser" in prefs_data and "window_placement" in prefs_data["browser"]:
                    prefs_data["browser"].pop("window_placement", None)
                    prefs_file.write_text(json.dumps(prefs_data), encoding="utf-8")
            except Exception:
                pass

    @staticmethod
    async def _detect_challenge(page: Page) -> bool:
        """Sayfada PerimeterX, Akamai, Cloudflare veya Captcha doÄŸrulama ekranÄ± olup olmadÄ±ÄŸÄ±nÄ± denetler."""
        try:
            content = (await page.content()).lower()
            return any(
                kw in content
                for kw in (
                    "px-captcha",
                    "perimeterx",
                    "press & hold",
                    "basÄ±lÄ± tutun",
                    "robot olmadÄ±ÄŸÄ±nÄ±zÄ±",
                    "human verification",
                    "cpr_chlge",
                    "cf-turnstile",
                    "hcaptcha",
                    "recaptcha",
                )
            )
        except Exception:
            return False

    async def _handle_human_challenge_if_present(self, page: Page, wait_sec: int | None = None) -> None:
        """
        PerimeterX veya Captcha ekranÄ± tespit edilirse kullanÄ±cÄ±ya doÄŸrulamayÄ±
        tamamlamasÄ± iÃ§in sÃ¼re tanÄ±r. Zorunlu headless=False sayesinde pencere gÃ¶rÃ¼nÃ¼rdÃ¼r.
        SÃ¼re dolduÄŸunda pencereyi kapatmak yerine kullanÄ±cÄ± ENTER tuÅŸuna basana kadar bekler.
        """
        max_wait = wait_sec if wait_sec is not None else self.challenge_wait_sec
        is_challenge = await self._detect_challenge(page)
        if is_challenge:
            console.print(
                Panel.fit(
                    "[bold red]ğŸ›¡ï¸ PerimeterX / GÃ¼venlik DoÄŸrulamasÄ± AlgÄ±landÄ±![/bold red]\n"
                    "[bold white]LÃ¼tfen ekranda aÃ§Ä±lan Chrome tarayÄ±cÄ± penceresinde 'BasÄ±lÄ± Tutun' veya Captcha doÄŸrulamasÄ±nÄ± tamamlayÄ±n.[/bold white]\n"
                    f"[dim]KullanÄ±cÄ± doÄŸrulamasÄ± iÃ§in {max_wait} saniye otomatik dinleniyor...[/dim]",
                    title="DoÄŸrulama EkranÄ±",
                    border_style="red",
                )
            )
            challenge_resolved = False
            for _ in range(max_wait * 2):
                await asyncio.sleep(0.5)
                if not await self._detect_challenge(page):
                    console.print("[bold green]âœ” DoÄŸrulama otomatik olarak algÄ±landÄ± ve tamamlandÄ±, veri akÄ±ÅŸÄ±na devam ediliyor![/bold green]")
                    challenge_resolved = True
                    await asyncio.sleep(1.0)
                    return

            if not challenge_resolved:
                console.print(
                    "\n[bold yellow]ğŸ‘‰ DoÄŸrulamayÄ± tamamladÄ±ysanÄ±z devam etmek iÃ§in terminalde [bold green]ENTER[/bold green] tuÅŸuna basÄ±n...[/bold yellow]"
                )
                await asyncio.to_thread(input)
                console.print("[bold green]âœ” KullanÄ±cÄ± onayÄ± alÄ±ndÄ±, iÅŸleme devam ediliyor![/bold green]")
                await asyncio.sleep(1.0)
        elif self.user_wait_sec > 0:
            console.print(
                f"[dim yellow]ğŸ‘ï¸ TarayÄ±cÄ± penceresi ekranda aÃ§Ä±k (headless=False). "
                f"KullanÄ±cÄ± kontrolÃ¼ ve sayfa stabilizasyonu iÃ§in {self.user_wait_sec} sn bekleniyor...[/dim yellow]"
            )
            await page.wait_for_timeout(self.user_wait_sec * 1000)

    def _get_target_info(self) -> tuple[str, set[str]]:
        """captured_traffic.json iÃ§inden hedef URL'yi ve API domainlerini okur."""
        if not self.traffic_path.exists():
            return "https://www.google.com", set()

        try:
            data = json.loads(self.traffic_path.read_text(encoding="utf-8"))
            target_url = str(data.get("target_url", "")).strip()
            api_domains: set[str] = set()
            for ep in data.get("endpoints", []):
                domain = ep.get("domain")
                if domain:
                    api_domains.add(str(domain).lower())
            return target_url, api_domains
        except Exception:
            return "", set()

    @staticmethod
    def _is_ignored(url: str) -> bool:
        try:
            parsed = urlparse(url)
            hostname = (parsed.hostname or "").lower()
            if not hostname:
                return True
            return any(kw in hostname for kw in IGNORED_DOMAIN_KEYWORDS)
        except Exception:
            return True

    @classmethod
    def _extract_healable_headers(cls, headers: dict[str, str]) -> dict[str, str]:
        extracted: dict[str, str] = {}
        for k, v in headers.items():
            lower_k = k.lower().strip()
            if any(lower_k == prefix or lower_k.startswith(prefix) for prefix in HEALABLE_HEADER_PREFIXES):
                extracted[lower_k] = v
        return extracted

    @classmethod
    def _extract_healable_query_params(cls, url: str) -> dict[str, str]:
        extracted: dict[str, str] = {}
        parsed = urlparse(url)
        raw_params = parse_qs(parsed.query, keep_blank_values=False)
        for k, values in raw_params.items():
            lower_k = k.lower().strip()
            if values and any(lower_k == prefix or lower_k.startswith(prefix) for prefix in HEALABLE_TOKEN_KEYS):
                extracted[k] = values[-1]
        return extracted

    @classmethod
    def _extract_healable_body_tokens(cls, request: Request) -> dict[str, Any]:
        """TÃ¼m istek gÃ¶vdesi alanlarÄ±nÄ± dinamik sÃ¶zlÃ¼ÄŸe baÄŸlar."""
        extracted: dict[str, Any] = {}
        try:
            post_data = request.post_data
            if not post_data:
                return extracted
            parsed = json.loads(post_data)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    extracted[k] = v
        except Exception:
            pass
        return extracted

    async def aheal(
        self,
        target_url: str | None = None,
        retry_request: dict[str, Any] | None = None,
        proxy: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Playwright launch_persistent_context (headless=False,
        args=['--disable-blink-features=AutomationControlled']) ile doÄŸrudan sayfaya gider.
        KullanÄ±cÄ± doÄŸrulamayÄ± geÃ§ip ENTER'a basÄ±nca tÃ¼m Ã§erezleri (_abck, bm_sz, ak_bmsc, _px3 vb.)
        ve gerÃ§ek User-Agent'Ä± alarak session_tokens.json ve TokenStore'a kaydeder.
        """
        target_goto = target_url or "https://www.turkishairlines.com/tr-tr/ucak-bileti/rezervasyon/"

        console.print(
            Panel.fit(
                f"[bold yellow]âš¡ Self-Healing & Taze Oturum TÃ¼neli Devrede![/bold yellow]\n"
                f"[cyan]Hedef Gezinme URL'si:[/cyan] {target_goto}\n"
                "[dim]PerimeterX ve Akamai doÄŸrulamasÄ± doÄŸrudan bu sayfada Ã§Ã¶zÃ¼lÃ¼yor...[/dim]",
                title="Shadow-API SessionHealer",
                border_style="yellow",
            )
        )

        self.profile_dir.mkdir(parents=True, exist_ok=True)

        cookies: list[dict[str, Any]] = []
        user_agent: str = ""
        active_tenant = tenant_id or self.tenant_id

        is_headless_env = bool(
            self.mode == "cloud"
            or (sys.platform.startswith("linux") and not os.getenv("DISPLAY"))
        )
        launch_kwargs: dict[str, Any] = {
            "user_data_dir": str(self.profile_dir.resolve()),
            "headless": is_headless_env,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ],
        }

        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(**launch_kwargs)
            stored_pw_cookies = self.token_store.get_playwright_cookies(
                tenant_id=active_tenant,
                service=self.service,
                default_url=target_goto,
            )
            if stored_pw_cookies:
                try:
                    await context.add_cookies(stored_pw_cookies)
                except Exception:
                    pass

            page = context.pages[0] if context.pages else await context.new_page()

            console.print(f"[bold cyan]ğŸŒ TarayÄ±cÄ± aÃ§Ä±lÄ±yor:[/bold cyan] [white]{target_goto}[/white]")
            try:
                await page.goto(target_goto, wait_until="domcontentloaded")
            except Exception as e:
                console.print(f"[dim]Sayfa gezinme bildirimi: {e}[/dim]")

            await asyncio.to_thread(input, "Sayfa yÃ¼klendikten ve varsa doÄŸrulamayÄ± geÃ§tikten sonra ENTER'a basÄ±n...")
            console.print("[bold green]âœ” Devam ediliyor... Ã‡erezler ve gerÃ§ek User-Agent alÄ±nÄ±yor.[/bold green]")

            try:
                cookies = await context.cookies()
            except Exception:
                cookies = []

            try:
                user_agent = await page.evaluate("() => navigator.userAgent")
            except Exception:
                user_agent = ""

            try:
                await context.storage_state(path=str(self.profile_dir / "storage_state.json"))
            except Exception:
                pass

            await context.close()

        cookie_parts = [f"{c['name']}={c['value']}" for c in cookies if c.get("name")]
        cookie_str = "; ".join(cookie_parts)

        parsed_target = urlparse(target_goto)
        origin_url = f"{parsed_target.scheme}://{parsed_target.netloc}" if parsed_target.scheme and parsed_target.netloc else "https://www.turkishairlines.com"

        fresh_headers = {
            "cookie": cookie_str,
            "user-agent": user_agent,
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": origin_url,
            "referer": target_goto,
        }

        token_bundle: dict[str, Any] = {
            "target_url": target_goto,
            "healed_at": datetime.now(timezone.utc).isoformat(),
            "cookie": cookie_str,
            "user-agent": user_agent,
            "headers": fresh_headers,
            "query_params": {},
            "body_tokens": {},
            **fresh_headers,
        }

        self.token_store.save_session(
            tenant_id=active_tenant,
            service=self.service,
            cookies=cookies,
            user_agent=user_agent,
            metadata=token_bundle,
        )

        console.print(
            f"[bold green]âœ” Self-Healing tamamlandÄ±![/bold green] "
            f"Tenant: [cyan]{active_tenant}[/cyan] | "
            f"Ã‡erez SayÄ±sÄ±: [cyan]{len(cookies)}[/cyan] | "
            f"User-Agent: [cyan]{user_agent[:45]}...[/cyan]"
        )

        return token_bundle

    def heal(
        self,
        target_url: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Senkron kodlardan doÄŸrudan Ã§aÄŸrÄ±labilen Self-Healing metodu."""
        return self._run_sync(self.aheal(target_url=target_url, tenant_id=tenant_id))

    async def aexecute_in_browser(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_payload: Any = None,
        target_url: str | None = None,
        user_wait_sec: int | None = None,
        challenge_wait_sec: int | None = None,
        tenant_id: str | None = None,
        mode: str | None = None,
        non_interactive: bool | None = None,
        form_params: dict[str, Any] | None = None,
        auto_fill_form: bool | None = None,
    ) -> dict[str, Any]:
        """
        OwnAPI User-Delegated Browser Middleware:
        - mode='local' (varsayÄ±lan): KullanÄ±cÄ±nÄ±n gerÃ§ek yerel Chrome oturumunu aÃ§ar
          (headless=False, args=['--disable-blink-features=AutomationControlled']).
        - mode='cloud': Bulut tabanlÄ± Docker / Remote Browser (CDP / Browserless) oturumuna baÄŸlanÄ±r
          veya izole bulut konteyner baÄŸlamÄ±nda Ã§alÄ±ÅŸtÄ±rÄ±r.
        - non_interactive=True (veya OWNAPI_NON_INTERACTIVE=1): Captcha/WAF Ã§Ä±ktÄ±ÄŸÄ±nda terminalde
          input() ile beklemek yerine gÃ¶revi HITLQueueManager kuyruÄŸuna ekler ve
          PENDING_USER_APPROVAL yanÄ±tÄ± dÃ¶ndÃ¼rÃ¼r.
        TokenStore'daki izole Ã§erezleri tarayÄ±cÄ±ya enjekte eder, hedef sayfaya gider ve
        doÄŸrudan tarayÄ±cÄ± iÃ§i page.evaluate(fetch) ile hedef isteÄŸi yÃ¼rÃ¼tÃ¼r.
        """
        req_headers = dict(headers or {})
        req_params = dict(params or {})
        req_payload = copy.deepcopy(json_payload)
        active_tenant = tenant_id or self.tenant_id
        active_mode = (mode or self.mode or "local").strip().lower()
        if active_mode not in ("local", "cloud"):
            active_mode = "local"
        env_ni = os.getenv("OWNAPI_NON_INTERACTIVE", "").strip().lower() in ("1", "true", "yes")
        active_non_interactive = (
            bool(non_interactive or env_ni)
            if non_interactive is not None
            else bool(self.non_interactive or env_ni)
        )

        full_req_url = f"{url}?{urlencode(req_params, doseq=True)}" if req_params else url
        parsed_req = urlparse(full_req_url)
        default_origin = (
            f"{parsed_req.scheme}://{parsed_req.netloc}/"
            if parsed_req.scheme and parsed_req.netloc
            else "https://www.turkishairlines.com/tr-tr/"
        )

        is_thy_domain = (
            self.service.lower() == "thy"
            or "turkishairlines.com" in full_req_url.lower()
        )
        is_thy_availability = (
            "turkishairlines.com" in full_req_url.lower()
            and urlparse(full_req_url).path.rstrip("/") == "/api/v1/availability"
        )

        raw_visit_url = target_url or self.target_url or default_origin
        if is_thy_domain:
            # THY iÃ§in direkt boÅŸ API linkine (/api/...) veya eski 404 rezervasyon alt sayfasÄ±na gitmek yerine
            # her zaman Ã¶nce ana sayfada (https://www.turkishairlines.com/tr-tr/) oturum ve Ã§erezleri tazele
            visit_url = "https://www.turkishairlines.com/tr-tr/"
        else:
            parsed_visit = urlparse(raw_visit_url)
            if (
                raw_visit_url.rstrip("/") == full_req_url.rstrip("/")
                or "/api/" in parsed_visit.path.lower()
            ):
                # HITL veya Auto-Repair sÄ±rasÄ±nda target_url yanlÄ±ÅŸlÄ±kla API uÃ§ noktasÄ± olarak gelirse
                # ÅŸemadaki target_url veya kÃ¶k domain ana sayfasÄ±na yÃ¶nlendir
                schema_file = PROJECT_ROOT / "registry" / self.service / "schema.json"
                resolved_target = default_origin
                if schema_file.exists():
                    try:
                        s_data = json.loads(schema_file.read_text(encoding="utf-8"))
                        cand = str(s_data.get("target_url", "")).strip()
                        if cand and "/api/" not in urlparse(cand).path.lower():
                            resolved_target = cand
                    except Exception:
                        pass
                visit_url = resolved_target
            else:
                visit_url = raw_visit_url

        if is_thy_domain:
            req_headers["Referer"] = "https://www.turkishairlines.com/"
            req_headers["Origin"] = "https://www.turkishairlines.com"
            req_headers["referer"] = "https://www.turkishairlines.com/"
            req_headers["origin"] = "https://www.turkishairlines.com"
            req_headers.setdefault("x-country", "TR")
            req_headers.setdefault("x-language", "tr")
            req_headers.setdefault("accept", "application/json, text/plain, */*")
            req_headers.setdefault("content-type", "application/json")

        if is_thy_availability and (
            not isinstance(req_payload, dict)
            or not req_payload.get("originDestinationInformationList")
        ):
            try:
                from registry.thy.client import build_thy_availability_payload
                req_payload = build_thy_availability_payload()
            except Exception:
                req_payload = {
                    "selectedBookerSearch": "R",
                    "selectedCabinClass": "ECONOMY",
                    "moduleType": "TICKETING",
                    "passengerTypeList": [{"quantity": 1, "code": "ADULT"}],
                    "originDestinationInformationList": [
                        {
                            "originAirportCode": "IST",
                            "originCountryCode": "TR",
                            "originMultiPort": True,
                            "originDomestic": True,
                            "destinationAirportCode": "ESB",
                            "destinationCountryCode": "TR",
                            "destinationMultiPort": False,
                            "destinationDomestic": True,
                            "departureDate": "02-03-2027",
                            "originCityCode": "IST",
                            "destinationCityCode": "ANK",
                            "originCity": "Ä°stanbul",
                            "destinationCity": "Ankara",
                        },
                        {
                            "originAirportCode": "ESB",
                            "originCountryCode": "TR",
                            "originMultiPort": False,
                            "originDomestic": True,
                            "destinationAirportCode": "IST",
                            "destinationCountryCode": "TR",
                            "destinationMultiPort": True,
                            "destinationDomestic": True,
                            "departureDate": "09-03-2027",
                            "originCityCode": "ANK",
                            "destinationCityCode": "IST",
                            "originCity": "Ankara",
                            "destinationCity": "Ä°stanbul",
                        },
                    ],
                    "savedDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                    "preselectedOptionDetails": [],
                }

        mode_label = (
            f"â˜ï¸ CLOUD ({self.cloud_cdp_url or 'Docker/Headless Container'})"
            if active_mode == "cloud"
            else "ğŸ’» LOCAL (User Chrome Session)"
        )

        console.print(
            Panel.fit(
                f"[bold cyan]ğŸŒ User-Delegated Browser Fetch TÃ¼neli Devrede![/bold cyan]\n"
                f"[dim]Servis / Tenant:[/dim] [magenta]{self.service}[/magenta] ([cyan]{active_tenant}[/cyan])\n"
                f"[dim]Ã‡alÄ±ÅŸma Modu:[/dim] [yellow]{mode_label}[/yellow]\n"
                f"[dim]Metot / UÃ§ Nokta:[/dim] [green]{method.upper()}[/green] [white]{full_req_url}[/white]\n"
                f"[dim]Oturum SayfasÄ±:[/dim] [cyan]{visit_url}[/cyan]",
                title="OwnAPI Browser Middleware",
                border_style="cyan",
            )
        )

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        # KalÄ±cÄ± profil Ã§erezlerini koru - _clean_profile_dir KESÄ°NLÄ°KLE Ã§aÄŸrÄ±lmaz

        def _build_validate_payload(p_load: Any) -> dict[str, Any]:
            if not isinstance(p_load, dict):
                return {}
            val = {
                "selectedBookerSearch": p_load.get("selectedBookerSearch", "R"),
                "selectedCabinClass": p_load.get("selectedCabinClass", "ECONOMY"),
                "moduleType": p_load.get("moduleType", "TICKETING"),
                "passengerTypeList": p_load.get("passengerTypeList", [{"quantity": 1, "code": "ADULT"}]),
                "originDestinationInformationList": [],
            }
            for s in p_load.get("originDestinationInformationList", []):
                if isinstance(s, dict):
                    val["originDestinationInformationList"].append({
                        "originAirportCode": s.get("originAirportCode"),
                        "originMultiPort": s.get("originMultiPort", False),
                        "destinationAirportCode": s.get("destinationAirportCode"),
                        "destinationMultiPort": s.get("destinationMultiPort", False),
                        "departureDate": s.get("departureDate"),
                    })
            return val

        def _build_thy_search_state_url(
            p_load: Any,
            cid: str | None = None,
            candidate_url: str | None = None,
        ) -> str:
            """
            THY /api/v1/availability isteğinden önce Playwright'ın gidip sayfa state'ini
            oluşturması gereken gidiş-dönüş (veya tek yön) arama URL'sini üretir.
            """
            active_cid = cid or str(uuid.uuid4())
            if candidate_url and "turkishairlines.com" in candidate_url.lower():
                parsed_c = urlparse(candidate_url)
                if (
                    "/ucak-bileti/rezervasyon/" in parsed_c.path
                    and "origin=" in parsed_c.query
                    and "departureDate=" in parsed_c.query
                ):
                    if not cid or f"cId={cid}" in parsed_c.query:
                        return candidate_url

            try:
                from registry.thy.client import build_thy_search_url_from_payload
                return build_thy_search_url_from_payload(
                    p_load if isinstance(p_load, dict) else None,
                    cid=active_cid,
                )
            except Exception:
                from urllib.parse import urlencode

                orig = "IST"
                dest = "ESB"
                dep_date = "02-03-2027"
                ret_date: str | None = "09-03-2027"
                trip_type = "R"
                cabin = "ECONOMY"
                adults = 1
                is_domestic = True

                if isinstance(p_load, dict):
                    trip_type = str(p_load.get("selectedBookerSearch") or "R").upper()
                    cabin = str(p_load.get("selectedCabinClass") or "ECONOMY").upper()
                    od_l = p_load.get("originDestinationInformationList")
                    if isinstance(od_l, list) and len(od_l) >= 1 and isinstance(od_l[0], dict):
                        seg0 = od_l[0]
                        orig = str(seg0.get("originAirportCode") or orig)
                        dest = str(seg0.get("destinationAirportCode") or dest)
                        dep_date = str(seg0.get("departureDate") or dep_date)
                        is_domestic = bool(
                            seg0.get("originDomestic", True) and seg0.get("destinationDomestic", True)
                        )
                        if len(od_l) >= 2 and isinstance(od_l[1], dict):
                            ret_date = str(od_l[1].get("departureDate") or "") or None
                        elif trip_type == "O":
                            ret_date = None

                base_path = (
                    "/tr-tr/ucak-bileti/rezervasyon/ic-hat-musaitlik/"
                    if is_domestic
                    else "/tr-tr/ucak-bileti/rezervasyon/rezervasyon-durumu/"
                )
                q_params: dict[str, Any] = {
                    "cId": active_cid,
                    "origin": orig,
                    "destination": dest,
                    "departureDate": dep_date,
                }
                if ret_date:
                    q_params["returnDate"] = ret_date
                q_params["tripType"] = "R" if ret_date else "O"
                q_params["cabinClass"] = cabin
                q_params["ADT"] = adults
                return f"https://www.turkishairlines.com{base_path}?{urlencode(q_params)}"

        def _is_populated_availability(json_obj_or_str: Any) -> bool:
            if isinstance(json_obj_or_str, str):
                try:
                    json_obj_or_str = json.loads(json_obj_or_str)
                except Exception:
                    return False
            if not isinstance(json_obj_or_str, dict):
                return False
            d = json_obj_or_str.get("data")
            if isinstance(d, dict):
                if "showBanner" in d and not d.get("originDestinationInformationList"):
                    return False
                od_l = d.get("originDestinationInformationList", [])
                if isinstance(od_l, list) and od_l:
                    for od_item in od_l:
                        if isinstance(od_item, dict) and (od_item.get("originDestinationOptionList") or od_item.get("flightOptionList") or od_item.get("options")):
                            return True
                air_l = d.get("availabilityInfoResponse")
                if isinstance(air_l, list) and any(len(sub) > 0 for sub in air_l if isinstance(sub, list)):
                    return True
                if d.get("economyStartingPrice") or d.get("businessStartingPrice"):
                    return True
            return False

        validate_url = "https://www.turkishairlines.com/api/v1/availability/validate" if is_thy_availability else None
        val_payload = _build_validate_payload(req_payload) if is_thy_availability else None

        fetch_js = """async ({ validateUrl, validateBody, url, method, headers, body }) => {
            const forbidden = [
                'user-agent', 'cookie', 'host',
                'content-length', 'connection', 'keep-alive', 'accept-encoding'
            ];
            const safeHeaders = {};
            for (const [k, v] of Object.entries(headers || {})) {
                const lk = k.toLowerCase().trim();
                if (!forbidden.includes(lk) && !lk.startsWith('sec-')) {
                    safeHeaders[k] = v;
                }
            }
            const isThy = (url || '').toLowerCase().includes('turkishairlines.com');
            if (isThy) {
                safeHeaders['Referer'] = headers?.['Referer'] || headers?.['referer'] || 'https://www.turkishairlines.com/';
                safeHeaders['Origin'] = headers?.['Origin'] || headers?.['origin'] || 'https://www.turkishairlines.com';
            }
            const customReferrer = safeHeaders['Referer'] || safeHeaders['referer'] || (isThy ? 'https://www.turkishairlines.com/' : undefined);
            const hasBody = body !== null && body !== undefined && method !== 'GET' && method !== 'HEAD';
            if (hasBody && !safeHeaders['content-type'] && !safeHeaders['Content-Type']) {
                safeHeaders['content-type'] = 'application/json';
            }
            if (!safeHeaders['accept'] && !safeHeaders['Accept']) {
                safeHeaders['accept'] = 'application/json, text/plain, */*';
            }

            // 1. THY ise oturum durumunu hazÄ±rlamak iÃ§in /api/v1/availability/validate Ã§aÄŸrÄ±sÄ±
            if (validateUrl && validateBody) {
                try {
                    const valOpts = {
                        method: 'POST',
                        headers: safeHeaders,
                        credentials: 'include',
                        referrer: customReferrer,
                        body: typeof validateBody === 'string' ? validateBody : JSON.stringify(validateBody)
                    };
                    const valRes = await fetch(validateUrl, valOpts);
                    const valText = await valRes.text();
                    if (valText.includes('PXDqLTvkTO') || valText.includes('blockScript')) {
                        return { status: valRes.status, ok: false, text: valText, step: 'validate' };
                    }
                } catch (e) {
                    // validate hatasÄ±nÄ± yut ve ana isteÄŸe devam et
                }
            }

            // 2. Ana hedef fetch() Ã§aÄŸrÄ±sÄ±
            const opts = {
                method: method || 'GET',
                headers: safeHeaders,
                credentials: 'include'
            };
            if (customReferrer) {
                opts.referrer = customReferrer;
            }
            if (hasBody) {
                opts.body = typeof body === 'string' ? body : JSON.stringify(body);
            }
            try {
                const res = await fetch(url, opts);
                const text = await res.text();
                return { status: res.status, ok: res.ok, text, step: 'main' };
            } catch (e) {
                try {
                    const omitOpts = { ...opts, credentials: 'omit' };
                    const res2 = await fetch(url, omitOpts);
                    const text2 = await res2.text();
                    return { status: res2.status, ok: res2.ok, text: text2, step: 'main_omit' };
                } catch (e2) {
                    return { status: 0, ok: false, text: '', error: String(e), step: 'main' };
                }
            }
        }"""

        eval_result: dict[str, Any] = {}

        async with async_playwright() as p:
            remote_browser = None
            if active_mode == "cloud" and self.cloud_cdp_url:
                console.print(f"[bold cyan]â˜ï¸ Cloud Remote Browser baÄŸlantÄ±sÄ± kuruluyor:[/bold cyan] [white]{self.cloud_cdp_url}[/white]")
                remote_browser = await p.chromium.connect_over_cdp(self.cloud_cdp_url)
                context = remote_browser.contexts[0] if remote_browser.contexts else await remote_browser.new_context()
            elif active_mode == "cloud":
                # Bulut/Docker konteyner hazÄ±rlÄ±k modu (headless izole profil)
                cloud_kwargs: dict[str, Any] = {
                    "user_data_dir": str(self.profile_dir.resolve()),
                    "headless": True,
                    "args": [
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                }
                context = await p.chromium.launch_persistent_context(**cloud_kwargs)
            else:
                is_headless_env = bool(
                    sys.platform.startswith("linux") and not os.getenv("DISPLAY")
                )
                launch_kwargs: dict[str, Any] = {
                    "user_data_dir": str(self.profile_dir.resolve()),
                    "headless": is_headless_env,
                    "args": [
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                }
                context = await p.chromium.launch_persistent_context(**launch_kwargs)

            # TokenStore'daki kayÄ±tlÄ± kiracÄ±/servis Ã§erezlerini tarayÄ±cÄ± baÄŸlamÄ±na otomatik enjekte et
            stored_pw_cookies = self.token_store.get_playwright_cookies(
                tenant_id=active_tenant,
                service=self.service,
                default_url=visit_url,
            )
            if stored_pw_cookies:
                try:
                    await context.add_cookies(stored_pw_cookies)
                except Exception:
                    pass

            page = context.pages[0] if context.pages else await context.new_page()
            if active_mode == "local":
                try:
                    await page.bring_to_front()
                except Exception:
                    pass

            # TarayÄ±cÄ± aÄŸ trafiÄŸini dinleyerek asÄ±l /api/v1/availability istek ve yanÄ±tlarÄ±nÄ± yakala
            captured_browser_request: dict[str, Any] = {}
            captured_browser_response: dict[str, Any] = {}

            def handle_request(req):
                try:
                    p_path = urlparse(req.url).path.rstrip('/')
                    if p_path == "/api/v1/availability" and req.method == "POST":
                        captured_browser_request["url"] = req.url
                        captured_browser_request["headers"] = dict(req.headers)
                        captured_browser_request["post_data"] = req.post_data_json if req.post_data else None
                        captured_browser_request["raw_post_data"] = req.post_data
                except Exception:
                    pass

            def handle_response(res):
                try:
                    p_path = urlparse(res.url).path.rstrip('/')
                    if p_path == "/api/v1/availability" and res.request.method == "POST":
                        async def _read():
                            try:
                                captured_browser_response["status"] = res.status
                                captured_browser_response["text"] = await res.text()
                            except Exception:
                                pass
                        asyncio.create_task(_read())
                except Exception:
                    pass

            if is_thy_availability:
                page.on("request", handle_request)
                page.on("response", handle_response)

            async def _prepare_thy_search_page_state(
                page_obj: Any,
                p_load: Any,
                hdrs: dict[str, Any],
                cid: str | None = None,
            ) -> str:
                """
                WAF/Captcha geçildikten veya HITL onayı (resume) alındıktan sonra,
                doğrudan /api/v1/availability isteği atmadan önce:
                1. Tarayıcıda ön doğrulama (pax-type-codes, availability/validate) ve storage state'ini hazırlar,
                2. İlgili gidiş-dönüş arama URL'sine (page.goto) giderek sayfa state'ini oluşturur,
                3. Başlıkları (Referer, x-current-page-url, x-client-id) aktif arama sayfasıyla senkronize eder.
                """
                active_cid = (
                    cid
                    or hdrs.get("x-client-id")
                    or hdrs.get("x-clientid")
                    or str(uuid.uuid4())
                )
                search_state_url = _build_thy_search_state_url(
                    p_load,
                    cid=active_cid,
                    candidate_url=raw_visit_url,
                )
                parsed_s = urlparse(search_state_url)
                route_path = (
                    f"{parsed_s.path}?{parsed_s.query}"
                    if parsed_s.query
                    else parsed_s.path
                )

                hdrs["x-client-id"] = active_cid
                hdrs["x-clientid"] = active_cid
                hdrs["x-current-page-url"] = route_path
                hdrs["Referer"] = search_state_url
                hdrs["referer"] = search_state_url
                hdrs["Origin"] = "https://www.turkishairlines.com"
                hdrs["origin"] = "https://www.turkishairlines.com"

                val_body = _build_validate_payload(p_load)

                # Adım 1: Arama sayfasına gitmeden önce session/localStorage ve validate state'ini hazırla
                try:
                    await page_obj.evaluate(
                        """async ({ cid, validateBody, fullPayload, routePath }) => {
                            try {
                                const stateObj = {
                                    cId: cid,
                                    bookingSearch: fullPayload || validateBody,
                                    updatedAt: new Date().toISOString()
                                };
                                sessionStorage.setItem('thy_booking_search_state', JSON.stringify(stateObj));
                                sessionStorage.setItem('cId', cid);
                                localStorage.setItem('thy_last_cid', cid);
                            } catch (_) {}

                            const commonHeaders = {
                                'accept': 'application/json, text/plain, */*',
                                'content-type': 'application/json',
                                'x-country': 'TR',
                                'x-language': 'tr',
                                'x-client-id': cid,
                                'x-clientid': cid,
                                'x-current-page-url': '/tr-tr'
                            };
                            try {
                                const odList = (validateBody && validateBody.originDestinationInformationList) || [];
                                await fetch('https://www.turkishairlines.com/api/v1/passenger/pax-type-codes', {
                                    method: 'POST',
                                    headers: commonHeaders,
                                    credentials: 'include',
                                    body: JSON.stringify({
                                        selectedBookerSearch: (validateBody && validateBody.selectedBookerSearch) || 'R',
                                        moduleType: 'TICKETING',
                                        originDestinationInformationList: odList.slice(0, 1)
                                    })
                                });
                            } catch (_) {}

                            if (validateBody) {
                                try {
                                    await fetch('https://www.turkishairlines.com/api/v1/availability/validate', {
                                        method: 'POST',
                                        headers: commonHeaders,
                                        credentials: 'include',
                                        body: JSON.stringify(validateBody)
                                    });
                                } catch (_) {}
                            }
                        }""",
                        {
                            "cid": active_cid,
                            "validateBody": val_body,
                            "fullPayload": p_load,
                            "routePath": route_path,
                        },
                    )
                except Exception:
                    pass

                # Adım 2: Sayfada arama formu varsa Generic Form Automation ile otonom doldur
                try:
                    await _run_smart_form_automation(page_obj, p_load)
                except Exception:
                    pass

                # Adım 3: İlgili gidiş-dönüş arama URL'sine (search_state_url) giderek sayfa state'ini oluştur
                console.print(
                    f"[bold cyan]🧭 THY arama URL'sine gidilerek sayfa state'i oluşturuluyor:[/bold cyan] [white]{search_state_url}[/white]"
                )
                try:
                    await page_obj.goto(
                        search_state_url,
                        wait_until="domcontentloaded",
                        timeout=15_000,
                    )
                except Exception as nav_exc:
                    console.print(f"[dim]THY arama sayfası gezinme bildirimi: {nav_exc}[/dim]")

                await asyncio.sleep(3.0)
                return search_state_url

            async def _run_smart_form_automation(
                page_obj: Any,
                p_load: Any = None,
            ) -> dict[str, Any]:
                """
                Genel Amaçlı Akıllı Form Otomasyonu (Generic Form Automation):
                1. Sayfa yüklendiğinde origin/destination ve departure/return date input'larını
                   semantik olarak (placeholder, name, aria-label, id, label) bulup otonom doldurur.
                2. Autocomplete listesi çıkarsa ilk eşleşen sonuca tıklar.
                3. Arama / uçuş bul butonunu otomatik tespit edip tıklayarak SPA state'ini oluşturur.
                """
                if auto_fill_form is False:
                    return {"executed": False}
                try:
                    from core.form_automation import (
                        extract_semantic_form_params,
                        run_generic_form_automation,
                    )

                    active_body = p_load if p_load is not None else req_payload
                    sem_params = extract_semantic_form_params(
                        json_payload=active_body,
                        params=req_params,
                        url=full_req_url,
                        target_url=raw_visit_url,
                        explicit_form_params=form_params,
                    )
                    has_route_or_date = bool(
                        sem_params.get("origin")
                        or sem_params.get("destination")
                        or sem_params.get("departure_date")
                        or sem_params.get("return_date")
                    )
                    if not has_route_or_date and not auto_fill_form and not form_params:
                        return {"executed": False}

                    return await run_generic_form_automation(
                        page=page_obj,
                        form_params=sem_params,
                        json_payload=active_body,
                        params=req_params,
                        url=full_req_url,
                        target_url=raw_visit_url,
                        submit_form=True,
                    )
                except Exception as fa_exc:
                    console.print(f"[dim]Generic Form Automation uyarısı: {fa_exc}[/dim]")
                    return {"executed": False}

            console.print(f"[bold cyan]🌐 Tarayıcı açılıyor:[/bold cyan] [white]{visit_url}[/white]")
            try:
                await page.goto(visit_url, wait_until="domcontentloaded", timeout=12_000)
            except Exception as e:
                console.print(f"[dim]Sayfa gezinme bildirimi: {e}[/dim]")

            wait_seconds = 3 if is_thy_availability else 1
            console.print(f"[dim]Frontend oturumunun yerleşmesi için {wait_seconds} saniye bekleniyor...[/dim]")
            await asyncio.sleep(wait_seconds)

            from core.hitl_queue import HITLQueueManager

            hitl_mgr = HITLQueueManager()
            active_hitl_task_id: str | None = None
            is_resuming_hitl = os.getenv("OWNAPI_HITL_RESUMING", "0").strip() == "1"
            pause_and_wait = (
                not is_resuming_hitl
                and os.getenv("OWNAPI_HITL_PAUSE_AND_WAIT", "1").strip().lower() not in ("0", "false", "no")
            )
            pause_timeout_sec = float(os.getenv("OWNAPI_HITL_PAUSE_TIMEOUT", "300"))

            # Eğer THY availability çağrısıysa ve ana sayfada WAF engeli yoksa (veya HITL resume modundaysak),
            # doğrudan /api/v1/availability isteği atmadan ÖNCE arama URL'sine gidip sayfa state'ini oluştur!
            if is_thy_availability and (is_resuming_hitl or not await self._detect_challenge(page)):
                await _prepare_thy_search_page_state(
                    page,
                    req_payload,
                    req_headers,
                    cid=req_headers.get("x-client-id") or req_headers.get("x-clientid"),
                )
                val_payload = _build_validate_payload(req_payload)
            elif not is_thy_availability and (is_resuming_hitl or not await self._detect_challenge(page)):
                # Diğer tüm siteler (Pegasus, Enuygun, Obilet veya yeni eklenen siteler) için
                # Generic Form Automation katmanını çalıştırarak SPA state'ini oluştur!
                await _run_smart_form_automation(page, req_payload)

            # Eğer arama URL'sine gidildiğinde sayfa kendi başına dolu /api/v1/availability yanıtı aldıysa onu kullan
            pre_captured = captured_browser_response.get("text", "")
            if is_thy_availability and pre_captured and _is_populated_availability(pre_captured):
                console.print("[bold green]✔ Arama sayfası state oluşumu sırasında dolu uçuş yanıtı yakalandı![/bold green]")
                eval_result = {
                    "status": captured_browser_response.get("status", 200),
                    "ok": True,
                    "text": pre_captured,
                    "step": "search_page_navigation",
                }
            else:
                # 1. Doğrudan açık tarayıcı sekmesi içinden fetch() isteğini tetikle
                console.print(f"[dim]Tarayıcı içi fetch({method.upper()} {full_req_url}) isteği gönderiliyor...[/dim]")
                try:
                    eval_result = await asyncio.wait_for(
                        page.evaluate(
                            fetch_js,
                            {
                                "validateUrl": validate_url,
                                "validateBody": val_payload,
                                "url": full_req_url,
                                "method": method.upper(),
                                "headers": req_headers,
                                "body": req_payload,
                            },
                        ),
                        timeout=15.0,
                    )
                except Exception as eval_exc:
                    console.print(f"[dim]In-page fetch zaman aşımı/uyarısı ({eval_exc}), context.request.fetch kullanılıyor...[/dim]")
                    eval_result = {"status": 0, "ok": False, "text": "", "error": str(eval_exc)}

                if eval_result.get("status", 0) == 0:
                    try:
                        ctx_resp = await context.request.fetch(
                            full_req_url,
                            method=method.upper(),
                            headers=req_headers,
                            data=json.dumps(req_payload) if isinstance(req_payload, (dict, list)) else req_payload,
                            timeout=15_000,
                        )
                        ctx_text = await ctx_resp.text()
                        eval_result = {
                            "status": ctx_resp.status,
                            "ok": ctx_resp.ok,
                            "text": ctx_text,
                            "step": "context_request",
                        }
                    except Exception as ctx_err:
                        console.print(f"[dim]Context request uyarısı: {ctx_err}[/dim]")

            resp_text = _extract_response_text(
                eval_result.get("text", "") if isinstance(eval_result, dict) else eval_result
            )
            resp_status = eval_result.get("status", 0) if isinstance(eval_result, dict) else 200
            is_challenge = (
                "PXDqLTvkTO" in resp_text
                or "blockScript" in resp_text
                or resp_status in (403, 429)
                or "wrapper--notfound" in resp_text
                or "Reference Code:" in resp_text
                or (not (eval_result.get("ok") if isinstance(eval_result, dict) else True) and await self._detect_challenge(page))
            )

            # 2. Eğer PerimeterX / Akamai / Cloudflare doğrulaması geldiyse
            if is_challenge:
                console.print("[bold yellow]⚠️  Güvenlik / İnsan doğrulaması tespit edildi![/bold yellow]")
                try:
                    await page.evaluate(
                        """(text) => {
                            try {
                                const data = JSON.parse(text);
                                if (data && data.blockScript) {
                                    window._pxAppId = data.appId;
                                    window._pxJsRef = data.jsRef;
                                    const s = document.createElement('script');
                                    s.src = data.blockScript;
                                    document.body.appendChild(s);
                                }
                            } catch (_) {}
                        }""",
                        resp_text,
                    )
                except Exception:
                    pass

                if not is_resuming_hitl:
                    hitl_task = hitl_mgr.enqueue_task(
                        tenant_id=active_tenant,
                        service=self.service,
                        method=method.upper(),
                        url=url,
                        params=req_params,
                        headers=req_headers,
                        json_payload=req_payload,
                        target_url=_build_thy_search_state_url(req_payload) if is_thy_availability else visit_url,
                        reason=f"Captcha / WAF / PerimeterX doğrulaması algılandı (HTTP {resp_status})",
                        in_place_paused=pause_and_wait,
                    )
                    active_hitl_task_id = hitl_task["task_id"]

                    if not pause_and_wait:
                        try:
                            await context.close()
                        except Exception:
                            pass
                        if remote_browser is not None:
                            try:
                                await remote_browser.close()
                            except Exception:
                                pass
                        return ResponseDict(
                            {
                                "status": "PAUSED",
                                "legacy_status": "PENDING_USER_APPROVAL",
                                "agent_state": "paused",
                                "task_id": hitl_task["task_id"],
                                "service": self.service,
                                "tenant_id": active_tenant,
                                "reason": hitl_task["reason"],
                                "message": "Ajan PAUSED durumuna alındı; Web Dashboard üzerinden onay bekleniyor",
                            }
                        )

                    console.print(
                        f"[bold yellow]⏸️  [HITL PAUSED] Ajan görevi ({hitl_task['task_id']}) öldürülmedi, 'PAUSED' durumuna alındı.[/bold yellow]\n"
                        f"[cyan]👉 Web Dashboard üzerinden onay verildiğinde kaldığı yerden ('resume') devam edecek...[/cyan]"
                    )
                    resumed_task = await hitl_mgr.await_task_resume(
                        task_id=hitl_task["task_id"],
                        poll_interval=0.5,
                        timeout_sec=pause_timeout_sec,
                    )
                    if resumed_task is None:
                        try:
                            await context.close()
                        except Exception:
                            pass
                        if remote_browser is not None:
                            try:
                                await remote_browser.close()
                            except Exception:
                                pass
                        return ResponseDict(
                            {
                                "status": "PAUSED",
                                "legacy_status": "PENDING_USER_APPROVAL",
                                "agent_state": "paused",
                                "task_id": hitl_task["task_id"],
                                "service": self.service,
                                "tenant_id": active_tenant,
                                "reason": hitl_task["reason"],
                                "message": "Kullanıcı doğrulaması bekleniyor (.sessions/hitl_state.json)",
                            }
                        )

                    console.print(
                        f"[bold green]▶️  [HITL RESUMED] Kullanıcı onayı alındı ({hitl_task['task_id']}), ajan kaldığı yerden devam ediyor...[/bold green]"
                    )
                else:
                    await asyncio.sleep(2.0)

                # WAF/Captcha geçildikten ve HITL onayı alındıktan sonra:
                # Doğrudan API isteği atmadan ÖNCE Generic Form Automation ve arama URL'si ile sayfa state'ini oluştur!
                if is_thy_availability:
                    fresh_waf_cid = str(uuid.uuid4())
                    await _prepare_thy_search_page_state(
                        page,
                        req_payload,
                        req_headers,
                        cid=fresh_waf_cid,
                    )
                    val_payload = _build_validate_payload(req_payload)
                else:
                    await _run_smart_form_automation(page, req_payload)

                captured_after_waf = captured_browser_response.get("text", "")
                if is_thy_availability and captured_after_waf and _is_populated_availability(captured_after_waf):
                    console.print("[bold green]✔ WAF sonrası arama sayfası state oluşumundan dolu uçuş yanıtı yakalandı![/bold green]")
                    eval_result = {
                        "status": captured_browser_response.get("status", 200),
                        "ok": True,
                        "text": captured_after_waf,
                    }
                else:
                    eval_result = await page.evaluate(
                        fetch_js,
                        {
                            "validateUrl": validate_url,
                            "validateBody": val_payload,
                            "url": full_req_url,
                            "method": method.upper(),
                            "headers": req_headers,
                            "body": req_payload,
                        },
                    )

            # 3. THY Özel Kontrolü: Eğer Error-DS-30006 dönerse veya availabilityInfoResponse boş ([[], []]) geldiyse
            if is_thy_availability:
                resp_text = _extract_response_text(eval_result)
                is_empty_air = False
                try:
                    chk_res = json.loads(resp_text)
                    if isinstance(chk_res, dict):
                        data_part = chk_res.get("data")
                        if isinstance(data_part, dict) and data_part.get("availabilityInfoResponse") == [[], []]:
                            is_empty_air = True
                except Exception:
                    pass

                is_populated = _is_populated_availability(resp_text)
                is_ds_error = (
                    "Error-DS-30006" in resp_text
                    or "DS-30006" in resp_text
                    or "işleminizi gerçekleştiremiyoruz" in resp_text
                    or (is_empty_air and not is_populated)
                )

                if is_ds_error or not is_populated:
                    if is_empty_air:
                        console.print("\n[bold yellow]⚠️  Boş availabilityInfoResponse: [[], []] alındı (oturum durumu henüz hazır değil).[/bold yellow]")
                    else:
                        console.print("\n[bold yellow]⚠️  Backend oturum/şema uyarısı alındı.[/bold yellow]")

                    if not is_resuming_hitl:
                        if active_hitl_task_id is None:
                            hitl_task = hitl_mgr.enqueue_task(
                                tenant_id=active_tenant,
                                service=self.service,
                                method=method.upper(),
                                url=url,
                                params=req_params,
                                headers=req_headers,
                                json_payload=req_payload,
                                target_url=_build_thy_search_state_url(req_payload),
                                reason="THY oturum/müsaitlik ön etkileşimi veya WAF doğrulaması gerekiyor",
                                in_place_paused=pause_and_wait,
                            )
                            active_hitl_task_id = hitl_task["task_id"]
                        else:
                            hitl_task, _ = hitl_mgr.get_task(active_hitl_task_id, tenant_id=active_tenant)
                            hitl_task = hitl_task or {"task_id": active_hitl_task_id, "reason": "THY doğrulaması gerekiyor"}

                        if not pause_and_wait:
                            try:
                                await context.close()
                            except Exception:
                                pass
                            if remote_browser is not None:
                                try:
                                    await remote_browser.close()
                                except Exception:
                                    pass
                            return ResponseDict(
                                {
                                    "status": "PAUSED",
                                    "legacy_status": "PENDING_USER_APPROVAL",
                                    "agent_state": "paused",
                                    "task_id": hitl_task["task_id"],
                                    "service": self.service,
                                    "tenant_id": active_tenant,
                                    "reason": hitl_task["reason"],
                                    "message": "Ajan PAUSED durumuna alındı; Web Dashboard üzerinden onay bekleniyor",
                                }
                            )

                        console.print(
                            f"[bold yellow]⏸️  [HITL PAUSED] THY görevi ({hitl_task['task_id']}) 'PAUSED' durumuna alındı.[/bold yellow]\n"
                            f"[cyan]👉 Web Dashboard üzerinden onay verildiğinde kaldığı yerden ('resume') devam edecek...[/cyan]"
                        )
                        resumed_task = await hitl_mgr.await_task_resume(
                            task_id=hitl_task["task_id"],
                            poll_interval=0.5,
                            timeout_sec=pause_timeout_sec,
                        )
                        if resumed_task is None:
                            try:
                                await context.close()
                            except Exception:
                                pass
                            if remote_browser is not None:
                                try:
                                    await remote_browser.close()
                                except Exception:
                                    pass
                            return ResponseDict(
                                {
                                    "status": "PAUSED",
                                    "legacy_status": "PENDING_USER_APPROVAL",
                                    "agent_state": "paused",
                                    "task_id": hitl_task["task_id"],
                                    "service": self.service,
                                    "tenant_id": active_tenant,
                                    "reason": hitl_task["reason"],
                                    "message": "Kullanıcı doğrulaması bekleniyor (.sessions/hitl_state.json)",
                                }
                            )

                        console.print(
                            f"[bold green]▶️  [HITL RESUMED] Kullanıcı onayı alındı ({hitl_task['task_id']}), THY görevi kaldığı yerden devam ediyor...[/bold green]"
                        )
                    else:
                        await asyncio.sleep(2.0)

                    captured_text = captured_browser_response.get("text", "")
                    if captured_text and _is_populated_availability(captured_text):
                        console.print("[bold green]✔ Tarayıcı trafiğinden dolu uçuş yanıtı başarıyla yakalandı ve kullanılıyor![/bold green]")
                        eval_result = {
                            "status": captured_browser_response.get("status", 200),
                            "ok": True,
                            "text": captured_text,
                        }
                    else:
                        fresh_call_cid = str(uuid.uuid4())
                        retry_headers = dict(captured_browser_request.get("headers") or req_headers)
                        retry_url = captured_browser_request.get("url") or full_req_url
                        retry_payload = captured_browser_request.get("post_data") or req_payload

                        # HITL resume sonrası doğrudan /api/v1/availability isteği atmadan ÖNCE
                        # Playwright tarayıcısını gidiş-dönüş parametreleriyle arama URL'sine götür ve sayfa state'ini oluştur!
                        await _prepare_thy_search_page_state(
                            page,
                            retry_payload,
                            retry_headers,
                            cid=fresh_call_cid,
                        )
                        val_payload = _build_validate_payload(retry_payload)

                        captured_after_nav = captured_browser_response.get("text", "")
                        if captured_after_nav and _is_populated_availability(captured_after_nav):
                            console.print("[bold green]✔ Arama sayfasına gezinme sonrası dolu uçuş yanıtı yakalandı![/bold green]")
                            eval_result = {
                                "status": captured_browser_response.get("status", 200),
                                "ok": True,
                                "text": captured_after_nav,
                            }
                        else:
                            eval_result = await page.evaluate(
                                fetch_js,
                                {
                                    "validateUrl": validate_url,
                                    "validateBody": val_payload,
                                    "url": retry_url,
                                    "method": method.upper(),
                                    "headers": retry_headers,
                                    "body": retry_payload,
                                },
                            )

            # 4. Taze çerezleri ve User-Agent'ı TokenStore'a (.sessions/{tenant_id}/{service}_tokens.json) kaydet
            try:
                cookies = await context.cookies()
                user_agent = await page.evaluate("() => navigator.userAgent")
                cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get("name"))
                parsed_visit = urlparse(visit_url)
                origin_val = f"{parsed_visit.scheme}://{parsed_visit.netloc}" if parsed_visit.scheme and parsed_visit.netloc else "https://www.turkishairlines.com"

                token_bundle = {
                    "target_url": visit_url,
                    "healed_at": datetime.now(timezone.utc).isoformat(),
                    "cookie": cookie_str,
                    "user-agent": user_agent,
                    "headers": {
                        "cookie": cookie_str,
                        "user-agent": user_agent,
                        "accept": "application/json",
                        "content-type": "application/json",
                        "origin": origin_val,
                        "referer": visit_url,
                    },
                    "query_params": {},
                    "body_tokens": {},
                }
                self.token_store.save_session(
                    tenant_id=active_tenant,
                    service=self.service,
                    cookies=cookies,
                    user_agent=user_agent,
                    metadata=token_bundle,
                )
                await context.storage_state(path=str(self.profile_dir / "storage_state.json"))
            except Exception:
                pass

            try:
                await context.close()
            except Exception:
                pass
            if remote_browser is not None:
                try:
                    await remote_browser.close()
                except Exception:
                    pass

        # Dönen yanıtı parse et ve döndür (dict ise .text aramaya kalkmadan doğrudan işle)
        if isinstance(eval_result, dict) and "text" not in eval_result and "status" not in eval_result:
            final_res_obj: Any = ResponseDict(eval_result)
            if active_hitl_task_id:
                hitl_mgr.update_task_state(active_hitl_task_id, status="COMPLETED", response=dict(final_res_obj))
            return final_res_obj

        final_text = _extract_response_text(eval_result)
        status_val = eval_result.get("status", 200) if isinstance(eval_result, dict) else 200
        is_ok = bool(eval_result.get("ok", 200 <= status_val < 400)) if isinstance(eval_result, dict) else True

        try:
            parsed_json = json.loads(final_text)
            if isinstance(parsed_json, dict):
                final_res_obj = ResponseDict(parsed_json)
                if active_hitl_task_id:
                    hitl_mgr.update_task_state(active_hitl_task_id, status="COMPLETED", response=dict(final_res_obj))
                return final_res_obj
            if active_hitl_task_id:
                hitl_mgr.update_task_state(active_hitl_task_id, status="COMPLETED", response=parsed_json)
            return parsed_json
        except Exception:
            if is_ok and final_text and not (isinstance(eval_result, dict) and eval_result.get("error")):
                final_res_obj = ResponseDict(
                    {
                        "success": True,
                        "status_code": status_val,
                        "ok": True,
                        "text": final_text,
                        "response_sample": final_text[:3000],
                    }
                )
                if active_hitl_task_id:
                    hitl_mgr.update_task_state(active_hitl_task_id, status="COMPLETED", response=dict(final_res_obj))
                return final_res_obj
            final_res_obj = ResponseDict(
                {
                    "status_code": status_val,
                    "text": final_text,
                    "response_sample": final_text[:3000],
                    "error": (eval_result.get("error") if isinstance(eval_result, dict) else None)
                    or "Yanıt JSON formatında ayrıştırılamadı",
                }
            )
            if active_hitl_task_id:
                hitl_mgr.update_task_state(active_hitl_task_id, status="COMPLETED", response=dict(final_res_obj))
            return final_res_obj


    def execute_in_browser(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_payload: Any = None,
        target_url: str | None = None,
        user_wait_sec: int | None = None,
        challenge_wait_sec: int | None = None,
        tenant_id: str | None = None,
        mode: str | None = None,
        non_interactive: bool | None = None,
        form_params: dict[str, Any] | None = None,
        auto_fill_form: bool | None = None,
    ) -> dict[str, Any]:
        """
        OwnAPI User-Delegated Browser Middleware:
        İsteği seçilen modda ('local' yerel Chrome veya 'cloud' Docker/Remote Browser)
        doğrudan tarayıcının kendi JavaScript ortamında (page.evaluate fetch) çalıştırır.
        """
        return self._run_sync(
            self.aexecute_in_browser(
                method=method,
                url=url,
                headers=headers,
                params=params,
                json_payload=json_payload,
                target_url=target_url,
                user_wait_sec=user_wait_sec,
                challenge_wait_sec=challenge_wait_sec,
                tenant_id=tenant_id,
                mode=mode,
                non_interactive=non_interactive,
                form_params=form_params,
                auto_fill_form=auto_fill_form,
            )
        )

    def retry_with_fresh_session(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        json_payload: Any = None,
        target_url: str | None = None,
    ) -> tuple[dict[str, str], dict[str, Any], Any, Any]:
        """
        401/403/429 alÄ±ndÄ±ÄŸÄ±nda taze oturum aÃ§ar, token ve baÅŸlÄ±klarÄ± yeniler ve
        isteÄŸi taze oturum tÃ¼neli Ã¼zerinden tekrarlar.
        DÃ¶nÃ¼ÅŸ: (gÃ¼ncel_headers, gÃ¼ncel_params, gÃ¼ncel_payload, tÃ¼nel_yanÄ±tÄ±_veya_None)
        """
        visit_url = target_url or headers.get("referer") or headers.get("origin")
        bundle = self._run_sync(
            self.aheal(
                target_url=visit_url,
                retry_request={
                    "method": method,
                    "url": url,
                    "headers": headers,
                    "params": params,
                    "json_payload": json_payload,
                },
            )
        )
        tunnel_resp = bundle.get("_tunnel_response")
        new_headers, new_params, new_payload = self.apply_to_request(
            headers,
            params,
            json_payload,
            endpoint_url=url,
        )
        return new_headers, new_params, new_payload, tunnel_resp

    @staticmethod
    def _run_sync(coro: Any) -> Any:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()
        return asyncio.run(coro)

    def load_tokens(self, tenant_id: str | None = None, service: str | None = None) -> dict[str, Any]:
        """TokenStore (veya geriye dÃ¶nÃ¼k uyumlulukla session_tokens.json) Ã¼zerinden oturum Ã§erezlerini okur."""
        t_id = tenant_id or self.tenant_id
        svc = service or self.service
        return self.token_store.get_session(t_id, svc)

    def load_profile_cookies(self, endpoint_url: str | None = None) -> str:
        """KalÄ±cÄ± tarayÄ±cÄ± profilindeki (.browser_profile/storage_state.json) Ã§erezleri okur."""
        state_file = self.profile_dir / "storage_state.json"
        if not state_file.exists():
            return ""
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            cookies = state.get("cookies", [])
            if not isinstance(cookies, list) or not cookies:
                return ""
            ep_host = (urlparse(endpoint_url).hostname or "").lower() if endpoint_url else ""
            ep_root = _root_domain(ep_host) if ep_host else ""
            matched: list[str] = []
            for c in cookies:
                if not isinstance(c, dict) or not c.get("name"):
                    continue
                c_domain = str(c.get("domain", "")).lstrip(".").lower()
                c_root = _root_domain(c_domain) if c_domain else ""
                if not ep_root or not c_root or ep_root == c_root:
                    matched.append(f"{c['name']}={c.get('value', '')}")
            return "; ".join(matched)
        except Exception:
            return ""

    def apply_to_request(
        self,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        payload: Any = None,
        endpoint_url: str | None = None,
    ) -> Any:
        """
        Mevcut istek baÅŸlÄ±klarÄ±nÄ± (headers), query parametrelerini (params) ve gÃ¶vdesini (payload)
        hem kalÄ±cÄ± profil (.browser_profile) Ã§erezleriyle hem de session_tokens.json iÃ§indeki
        dinamik baÅŸlÄ±k ve token'larla gÃ¼nceller.
        Geriye uyumluluk iÃ§in payload verilmemiÅŸse (headers, params), verilmiÅŸse (headers, params, payload) dÃ¶ndÃ¼rÃ¼r.
        """
        merged_headers = dict(headers)
        merged_params = dict(params) if params is not None else {}
        merged_payload = copy.deepcopy(payload)

        profile_cookie_str = self.load_profile_cookies(endpoint_url)
        if profile_cookie_str:
            merged_headers["cookie"] = _merge_cookie_strings(
                merged_headers.get("cookie", ""),
                profile_cookie_str,
            )

        saved = self.load_tokens()
        if saved:
            saved_target = str(saved.get("target_url", "")).lower()
            same_service = False
            if endpoint_url and saved_target:
                ep_host = (urlparse(endpoint_url).hostname or "").lower()
                target_host = (urlparse(saved_target).hostname or "").lower()
                if ep_host and target_host:
                    same_service = (_root_domain(ep_host) == _root_domain(target_host)) or (
                        "algolia" in ep_host and ("algolia" in target_host or "hn.algolia" in target_host)
                    )
            elif not saved_target:
                same_service = True

            if same_service:
                saved_headers = saved.get("headers")
                if isinstance(saved_headers, dict):
                    for k, v in saved_headers.items():
                        if not v:
                            continue
                        if k.lower() == "cookie":
                            merged_headers["cookie"] = _merge_cookie_strings(merged_headers.get("cookie", ""), str(v))
                        else:
                            merged_headers[k] = str(v)

                saved_params = saved.get("query_params")
                if isinstance(saved_params, dict):
                    for k, v in saved_params.items():
                        if v is not None and k in merged_params:
                            merged_params[k] = v

                saved_body_tokens = saved.get("body_tokens")
                if isinstance(saved_body_tokens, dict) and saved_body_tokens and merged_payload is not None:
                    merged_payload = update_payload_deep(merged_payload, saved_body_tokens)

        if payload is not None:
            return merged_headers, merged_params, merged_payload
        return merged_headers, merged_params


if __name__ == "__main__":
    healer = SessionHealer()
    healer.heal()

