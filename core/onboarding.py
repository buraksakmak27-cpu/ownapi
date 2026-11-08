from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import os
import re
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from openai import OpenAI
from playwright.async_api import Page, Request, Response, async_playwright
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from compiler import DynamicParamSpec, RegistrySchema
from core.consent import ConsentManager
from core.token_store import TokenStore
from recorder import IGNORED_DOMAIN_KEYWORDS, RELEVANT_HEADER_PREFIXES

console = Console(stderr=True, legacy_windows=False)

MAX_LLM_FIELD_CHARS = 2000

STATIC_FILE_EXTENSIONS: tuple[str, ...] = (
    ".css",
    ".js",
    ".mjs",
    ".cjs",
    ".map",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".webp",
    ".avif",
    ".bmp",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".otf",
    ".mp4",
    ".webm",
    ".mp3",
    ".wav",
    ".ogg",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".wasm",
    ".webmanifest",
)


EXCLUDED_NOISE_URL_KEYWORDS: tuple[str, ...] = (
    "allowlist",
    "/litms/",
    "static.licdn.com",
    "/aero-v1/",
    "/sc/h/",
    "sensorcollect",
    "realtimefrontend",
    "realtime/connect",
    "demdex.net",
    "protechts.net",
    "ns1p.net",
    "ccm/collect",
    "rmkt/collect",
    "gmp/conversion",
    "getthirdpartytrackingpixels",
    "actions/app-config",
    "chameleonconfig",
    "voyagerglobalalerts",
    "badgingitemcounts",
    "presencestatuses",
    "awaystatus",
    "affiliatedmailboxes",
    "voyagerdashmysettings",
    "policy/notices",
    "cdn-cgi",
    "measurement",
    "analytics",
    "telemetry",
    "doubleclick",
    "clarity.ms",
    "sentry.io",
)

PRIORITY_ENDPOINT_KEYWORDS: tuple[str, ...] = (
    "search",
    "jobs",
    "query",
    "voyager",
    "api",
)


def _is_static_asset_url(url: str) -> bool:
    """
    URL yolunun statik bir dosyaya (.css, .js, resim, font, static CDN) veya
    izin listesi / telemetri (`allowlist`, `/litms/`, `sensorCollect` vb.) adresine
    ait olup olmadığını denetler.
    """
    if not url:
        return False
    try:
        url_l = url.lower().strip()
        if any(noise in url_l for noise in EXCLUDED_NOISE_URL_KEYWORDS):
            return True
        parsed = urlparse(url_l)
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower().strip()
        if host.startswith("static.") or "cdn." in host:
            return True
        if any(path.endswith(ext) for ext in STATIC_FILE_EXTENSIONS):
            return True
        if any(seg in path for seg in ("/static/", "/assets/", "/fonts/", "/images/", "/img/", "/favicon", "/aero-v1/")):
            if not any(api_kw in path for api_kw in ("/api/", "/v1/", "/v2/", "/graphql", ".json")):
                return True
        return False
    except Exception:
        return False


def _extract_url_search_params(url: str) -> dict[str, str]:
    """
    Hedef veya yakalanan URL içindeki `keywords`, `query`, `q`, `location` gibi
    dinamik arama parametrelerini URL-decode edilmiş temiz metin olarak çıkarır.
    """
    from urllib.parse import unquote_plus

    extracted: dict[str, str] = {}
    if not url:
        return extracted
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        for key in ("keywords", "query", "q", "search", "location", "geoId"):
            if key in qs and qs[key]:
                val = unquote_plus(str(qs[key][0])).strip()
                if val:
                    extracted[key] = val
    except Exception:
        pass
    return extracted


def _score_endpoint_priority(ep: dict[str, Any], target_url: str = "") -> int:
    """
    Yakalanan 200+ XHR/Fetch isteği arasından rastgele ilk GET isteğini seçmek yerine;
    URL, query_params ve gövde içinde `search`, `jobs`, `query`, `voyager`, `api` ve
    `keywords` geçen anlamlı veri uç noktalarını puanlayarak önceliklendirir.
    """
    full_u = str(ep.get("full_url") or ep.get("url") or "").strip()
    url_l = full_u.lower()
    if _is_static_asset_url(full_u):
        return -1000

    score = 0
    path_l = str(ep.get("path") or urlparse(full_u).path or "").lower()
    q_params = ep.get("query_params") or {}
    payload_str = str(ep.get("request_payload") or "").lower()
    resp_str = str(ep.get("response_sample") or "").strip()

    # 1. Öncelikli URL kelimeleri: search, jobs, query, voyager, api
    if "search" in url_l or "search" in payload_str:
        score += 45
    if "jobs" in url_l or "jobsearch" in url_l or "jobseeker" in url_l:
        score += 40
    if "query" in url_l or "graphql" in url_l:
        score += 30
    if "voyager" in url_l:
        score += 25
    if "/api/" in path_l or "api." in url_l or "server-request" in path_l:
        score += 20

    # 2. Arama parametresi (`keywords`, `q`, `query`) eşleşmesi
    q_keys_lower = {str(k).lower() for k in q_params.keys()}
    if "keywords" in q_keys_lower or "keywords=" in url_l or '"keywords"' in payload_str:
        score += 55
    elif q_keys_lower.intersection({"q", "query", "search", "keyword"}):
        score += 35

    target_params = _extract_url_search_params(target_url)
    for tk, tv in target_params.items():
        if tv and (tv.lower() in url_l or tv.lower() in payload_str):
            score += 30

    # 3. Yanıt içeriği kalitesi
    if resp_str.startswith("{") or resp_str.startswith("["):
        score += 25
    elif "0:{" in resp_str or '"response":' in resp_str:
        score += 20
    if len(resp_str) > 200:
        score += 10

    # Arka plan/prefech veya ilgisiz profil/mesajlaşma uç noktalarını düşür
    if any(low_kw in url_l for low_kw in ("messaging", "notifications", "mailbox", "/feed/", "peoplewhocanhelp", "jobalerttoggle")):
        score -= 35
    if '"isprefetch": true' in payload_str or '"isprefetch":true' in payload_str:
        score -= 25

    return score


def _truncate_for_llm(value: Any, max_chars: int = MAX_LLM_FIELD_CHARS) -> Any:
    """post_data / request_payload veya yanıt metnini maksimum `max_chars` (2000) karakterle sınırlar."""
    if value is None:
        return None
    if isinstance(value, str):
        return value[:max_chars]
    try:
        serialized = json.dumps(value, ensure_ascii=False)
        if len(serialized) <= max_chars:
            return value
        return serialized[:max_chars]
    except Exception:
        return str(value)[:max_chars]


def _sanitize_endpoint_for_llm(ep: dict[str, Any], max_chars: int = MAX_LLM_FIELD_CHARS) -> dict[str, Any]:
    """
    LLM'e gönderilecek tekil uç nokta kaydının post_data (request_payload), response_sample
    ve büyük header değerlerini 2000 karakter sınırı altında tutarak 128k token aşımını engeller.
    """
    cleaned = dict(ep)
    cleaned["request_payload"] = _truncate_for_llm(ep.get("request_payload"), max_chars=max_chars)
    resp_sample = str(ep.get("response_sample", "") or "")
    cleaned["response_sample"] = resp_sample[:max_chars]
    cleaned["response_truncated"] = len(resp_sample) > max_chars or bool(ep.get("response_truncated"))

    raw_hdrs = ep.get("headers")
    if isinstance(raw_hdrs, dict):
        cleaned["headers"] = {
            str(k): (str(v)[:500] if len(str(v)) > 500 else str(v))
            for k, v in raw_hdrs.items()
        }
    return cleaned


def _clean_and_validate_python_code(code: str) -> str:
    """
    LLM tarafından üretilen Python kodundaki olası Markdown çitlerini (```python ... ```)
    temizler ve `ast.parse` ile Python sözdizimi (syntax) doğrulaması yapar.
    """
    import ast

    cleaned = (code or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    # Syntax doğrulaması (geçersizse SyntaxError fırlatır ve çağıran katman onarım/fallback uygular)
    ast.parse(cleaned)
    return cleaned


def _to_camel_case(snake_str: str) -> str:
    """snake_case metni CamelCase formatına dönüştürür."""
    components = re.sub(r"[^a-zA-Z0-9_]", "_", snake_str).split("_")
    return "".join(x.capitalize() for x in components if x)


def _sanitize_service_name(name: str) -> str:
    """Servis adını geçerli bir klasör ve Python tanımlayıcısına (snake_case) çevirir."""
    clean = re.sub(r"[^a-zA-Z0-9_]", "_", name.strip().lower())
    clean = re.sub(r"_+", "_", clean).strip("_")
    if not clean:
        clean = f"service_{uuid.uuid4().hex[:6]}"
    if clean[0].isdigit():
        clean = f"svc_{clean}"
    return clean


async def _record_traffic_async(
    start_url: str,
    output_path: Path,
    service_name: str,
    tenant_id: str = "default",
    headless: bool = False,
    wait_seconds: int | None = None,
) -> dict[str, Any]:
    """
    Playwright ile temiz Chrome penceresi açarak kullanıcının oturumundaki
    XHR/Fetch JSON API isteklerini, yanıt örneklerini ve çerezlerini kaydeder.
    - wait_seconds None ise: CLI modunda terminalde ENTER basılmasını bekler.
    - wait_seconds belirtilmişse: Web Dashboard modunda tarayıcı içine yüzen bir
      'Kaydı Tamamla & Derle' butonu enjekte eder ve kullanıcı butona basana veya
      süre dolana kadar dinler.
    """
    profile_dir = PROJECT_ROOT / ".browser_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    captured_endpoints: list[dict[str, Any]] = []
    endpoint_counter = 0
    token_store = TokenStore()

    async with async_playwright() as p:
        is_headless_env = bool(
            headless or (sys.platform.startswith("linux") and not os.getenv("DISPLAY"))
        )
        launch_kwargs: dict[str, Any] = {
            "user_data_dir": str(profile_dir.resolve()),
            "headless": is_headless_env,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ],
        }
        context = await p.chromium.launch_persistent_context(**launch_kwargs)

        page = context.pages[0] if context.pages else await context.new_page()
        if not headless:
            try:
                await page.bring_to_front()
            except Exception:
                pass

        async def on_response(res: Response) -> None:
            nonlocal endpoint_counter
            try:
                req = res.request
                resource_type = req.resource_type
                if resource_type not in ("xhr", "fetch"):
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
                    if any(lk == p or lk.startswith(p) for p in RELEVANT_HEADER_PREFIXES):
                        filtered_headers[lk] = v

                # post_data okuma: sıkıştırılmış (gzip) veya binary (octet-stream) verilerde
                # UnicodeDecodeError fırlatılmasını try-except ile güvenle yakala ve 2000 karakterle sınırla
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
                    try:
                        buf = req.post_data_buffer
                        if buf:
                            import gzip

                            raw_bytes = gzip.decompress(buf) if buf[:2] == b"\x1f\x8b" else buf
                            decoded_post = raw_bytes.decode("utf-8", errors="ignore").strip()
                            if decoded_post:
                                try:
                                    payload = _truncate_for_llm(json.loads(decoded_post), max_chars=MAX_LLM_FIELD_CHARS)
                                except Exception:
                                    payload = decoded_post[:MAX_LLM_FIELD_CHARS]
                    except (UnicodeDecodeError, Exception):
                        payload = None

                status = res.status
                sample = ""
                is_truncated = False
                try:
                    text = await res.text()
                    if text:
                        sample = text[:MAX_LLM_FIELD_CHARS]
                        is_truncated = len(text) > MAX_LLM_FIELD_CHARS
                except (UnicodeDecodeError, Exception):
                    try:
                        body_bytes = await res.body()
                        if body_bytes:
                            import gzip

                            raw_body = gzip.decompress(body_bytes) if body_bytes[:2] == b"\x1f\x8b" else body_bytes
                            decoded_body = raw_body.decode("utf-8", errors="ignore").strip()
                            if decoded_body:
                                sample = decoded_body[:MAX_LLM_FIELD_CHARS]
                                is_truncated = len(decoded_body) > MAX_LLM_FIELD_CHARS
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
                    "status_code": status,
                    "resource_type": resource_type,
                    "query_params": parse_qs(parsed_url.query),
                    "headers": filtered_headers,
                    "request_payload": payload,
                    "response_sample": sample,
                    "response_truncated": is_truncated,
                })
            except (UnicodeDecodeError, Exception):
                # Binary / gzip / octet-stream kaynaklı decode hatalarını akışı bozmadan yoksay
                return

        page.on("response", on_response)

        console.print(
            Panel.fit(
                f"[bold green]🚀 Tarayıcı Açılıyor:[/bold green] [white]{start_url}[/white]\n"
                "[yellow]Hedef işlemi (arama, form doldurma, gezinme) kendi oturumunuzla yapın.[/yellow]\n"
                + (
                    f"[bold cyan]Web Dashboard Modu: {wait_seconds} sn içinde işlemi yapın veya tarayıcıdaki 'Kaydı Bitir' butonuna tıklayın...[/bold cyan]"
                    if wait_seconds is not None
                    else "[bold white]Trafik kaydını tamamlamak için terminalde ENTER'a basın...[/bold white]"
                ),
                title="OwnAPI User-Delegated Traffic Recorder",
                border_style="green",
            )
        )

        try:
            await page.goto(start_url, wait_until="domcontentloaded")
        except Exception as e:
            console.print(f"[dim]Sayfa gezinme uyarısı: {e}[/dim]")

        if wait_seconds is not None:
            total_steps = max(2, int(wait_seconds * 2))
            for step_i in range(total_steps):
                remaining = max(0, int(wait_seconds - (step_i * 0.5)))
                try:
                    if page.is_closed():
                        break
                    stop_clicked = await page.evaluate(
                        """({ count, rem, svc }) => {
                            if (window.__ownapi_stop_recording) return true;
                            let bar = document.getElementById('__ownapi_rec_banner');
                            if (!bar && document.body) {
                                bar = document.createElement('div');
                                bar.id = '__ownapi_rec_banner';
                                bar.style.cssText = 'position:fixed;top:14px;right:14px;z-index:2147483647;background:#11131C;color:#F3F4F6;border:2px solid #6366F1;border-radius:12px;padding:12px 16px;font-family:system-ui,sans-serif;font-size:13px;box-shadow:0 12px 32px rgba(0,0,0,0.65);display:flex;align-items:center;gap:12px;';
                                bar.innerHTML = `
                                    <div>
                                        <div style="font-weight:700;color:#818CF8;">🔴 OwnAPI Recording: ${svc}</div>
                                        <div id="__ownapi_rec_stat" style="font-size:11px;color:#9CA3AF;margin-top:2px;">Yakalanan XHR/Fetch: ${count} | Kalan: ${rem}s</div>
                                    </div>
                                    <button id="__ownapi_rec_btn" style="background:#10B981;color:#fff;border:none;border-radius:8px;padding:7px 12px;font-weight:700;cursor:pointer;font-size:12px;">
                                        ✅ Kaydı Bitir & Derle
                                    </button>
                                `;
                                document.body.appendChild(bar);
                                document.getElementById('__ownapi_rec_btn').onclick = () => {
                                    window.__ownapi_stop_recording = true;
                                    bar.style.borderColor = '#10B981';
                                    bar.innerHTML = '<div style="font-weight:700;color:#10B981;">✔ Kayıt tamamlandı, GPT-4o ile derleniyor...</div>';
                                };
                            } else if (bar) {
                                const st = document.getElementById('__ownapi_rec_stat');
                                if (st) st.textContent = `Yakalanan XHR/Fetch: ${count} | Otomatik Bitiş: ${rem}s`;
                            }
                            return Boolean(window.__ownapi_stop_recording);
                        }""",
                        {"count": len(captured_endpoints), "rem": remaining, "svc": service_name},
                    )
                    if stop_clicked:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.5)
        else:
            await asyncio.to_thread(input, "\n👉 İşlem tamamlandığında ENTER'a basın...")

        # Eğer sayfada hiç XHR/Fetch tetiklenmediyse (örn. doğrudan JSON API URL'si veya statik sayfa),
        # hedef URL'nin kendisini varsayılan GET uç noktası olarak kaydet ki derleme kesintisiz çalışsın.
        if not captured_endpoints:
            try:
                parsed_start = urlparse(start_url)
                body_preview = ""
                try:
                    body_preview = (await page.evaluate("() => document.body ? document.body.innerText.slice(0, 1500) : ''")) or ""
                except Exception:
                    pass
                captured_endpoints.append({
                    "id": 1,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "method": "GET",
                    "url": f"{parsed_start.scheme}://{parsed_start.netloc}{parsed_start.path or '/'}",
                    "full_url": start_url,
                    "domain": (parsed_start.hostname or "").lower(),
                    "path": parsed_start.path or "/",
                    "status_code": 200,
                    "resource_type": "fetch",
                    "query_params": parse_qs(parsed_start.query),
                    "headers": {"accept": "application/json, text/plain, */*"},
                    "request_payload": None,
                    "response_sample": body_preview[:1500],
                    "response_truncated": False,
                })
            except Exception:
                pass

        console.print(f"[bold green]✔ Dinleme sonlandırıldı. Toplam {len(captured_endpoints)} XHR/Fetch isteği yakalandı.[/bold green]")

        # Oturum çerezlerini TokenStore'a kaydet
        try:
            cookies = await context.cookies()
            user_agent = await page.evaluate("() => navigator.userAgent")
            token_store.save_session(
                tenant_id=tenant_id,
                service=service_name,
                cookies=cookies,
                user_agent=user_agent,
                metadata={"target_url": start_url},
            )
        except Exception:
            pass

        try:
            await context.close()
        except Exception:
            pass

    traffic_session = {
        "target_url": start_url,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "total_endpoints": len(captured_endpoints),
        "endpoints": captured_endpoints,
    }
    output_path.write_text(json.dumps(traffic_session, ensure_ascii=False, indent=2), encoding="utf-8")
    return traffic_session


def _normalize_query_params(raw_params: dict[str, Any]) -> dict[str, Any]:
    """parse_qs tarafından üretilen {'key': ['val']} yapısını {'key': 'val'} yapısına sadeleştirir."""
    normalized: dict[str, Any] = {}
    for k, v in (raw_params or {}).items():
        if isinstance(v, list) and len(v) == 1:
            normalized[k] = v[0]
        else:
            normalized[k] = v
    return normalized


def _build_fallback_code(
    service_name: str,
    target_url: str,
    endpoint: dict[str, Any],
    description: str,
) -> tuple[str, RegistrySchema]:
    """LLM erişimi olmadığında veya hata aldığında çalışan standart BaseOwnClient kodunu üretir."""
    camel_name = _to_camel_case(service_name)
    target_search_params = _extract_url_search_params(target_url)
    ep_search_params = _extract_url_search_params(str(endpoint.get("full_url") or endpoint.get("url") or ""))
    merged_search_params = {**target_search_params, **ep_search_params}

    is_search = bool(merged_search_params) or any(
        k in (description or "").lower() or k in service_name.lower()
        for k in ("search", "ara", "bul", "query", "list", "get", "jobs", "ilan")
    )
    fn_name = f"search_{service_name}" if is_search else f"execute_{service_name}"
    method = endpoint.get("method", "GET").upper()
    ep_url = endpoint.get("url", target_url)
    headers = endpoint.get("headers", {}) or {}
    query_params = _normalize_query_params(endpoint.get("query_params", {}) or {})
    payload = endpoint.get("request_payload")

    dynamic_params: list[DynamicParamSpec] = []
    if "keywords" in query_params or "keywords" in merged_search_params:
        kw_default = str(query_params.get("keywords") or merged_search_params.get("keywords") or "AI Engineer")
        if "keywords" not in query_params:
            query_params["keywords"] = kw_default
        dynamic_params.append(
            DynamicParamSpec(
                name="keywords",
                original_key="keywords",
                location="query",
                param_type="str",
                default_value=kw_default,
                description="Aranacak iş/pozisyon veya anahtar kelime (örn. 'AI Engineer').",
            )
        )
    elif query_params:
        first_k = next(iter(query_params.keys()))
        first_v = query_params[first_k][0] if isinstance(query_params[first_k], list) else query_params[first_k]
        param_arg_name = "keywords" if first_k.lower() == "keywords" else "query"
        dynamic_params.append(
            DynamicParamSpec(
                name=param_arg_name,
                original_key=first_k,
                location="query",
                param_type="str",
                default_value=str(first_v),
                description=f"{first_k} sorgu parametresi",
            )
        )
    elif payload and isinstance(payload, dict):
        first_k = next(iter(payload.keys()))
        dynamic_params.append(
            DynamicParamSpec(
                name="query",
                original_key=first_k,
                location="body",
                param_type="str",
                default_value=str(payload[first_k]),
                description=f"{first_k} gövde parametresi",
            )
        )

    param_var = dynamic_params[0].name if dynamic_params else "query"
    default_q_repr = repr(dynamic_params[0].default_value) if dynamic_params else "'test'"
    sig_params = [f"{param_var}: str = {default_q_repr}"] if dynamic_params else []
    method_sig = ", ".join(["self"] + sig_params)
    module_fn_sig = ", ".join(sig_params + ["tenant_id: str = 'default'", "mode: str = 'local'"])
    call_args = ", ".join([f"{param_var}={param_var}"] if dynamic_params else [])

    query_update_line = ""
    body_update_line = ""
    if dynamic_params and dynamic_params[0].location == "query":
        orig_key_repr = repr(dynamic_params[0].original_key)
        query_update_line = f"if params is not None:\n            params[{orig_key_repr}] = {param_var}"
    elif dynamic_params and dynamic_params[0].location == "body":
        orig_key_repr = repr(dynamic_params[0].original_key)
        body_update_line = f"if isinstance(payload, dict):\n            payload[{orig_key_repr}] = {param_var}"

    client_code = f'''from __future__ import annotations

import copy
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from core.base_client import BaseOwnClient
from core.token_store import TokenStore


class {camel_name}Client(BaseOwnClient):
    """{service_name} servisi için OwnAPI User-Delegated Browser Middleware istemcisi."""

    def __init__(
        self,
        tenant_id: str = "default",
        token_store: TokenStore | None = None,
        mode: str = "local",
    ) -> None:
        super().__init__(
            service_name={repr(service_name)},
            base_url={repr(target_url)},
            tenant_id=tenant_id,
            token_store=token_store,
            mode=mode,
        )

    def {fn_name}({method_sig}) -> dict[str, Any]:
        endpoint_url = {repr(ep_url)}
        headers: dict[str, str] = {repr(headers)}
        params: dict[str, Any] = {repr(query_params)}
        payload: Any = {repr(payload) if payload is not None else "None"}

        {query_update_line}
        {body_update_line}

        res = self.request(
            method={repr(method)},
            url=endpoint_url,
            headers=headers,
            params=params if params else None,
            json_payload=payload if payload is not None else None,
        )
        return res if isinstance(res, dict) else {{"data": res}}


def {fn_name}({module_fn_sig}) -> dict[str, Any]:
    """
    {(description or service_name + ' servisini kullanıcının gerçek tarayıcı oturumu üzerinden çağırır.').replace('"', "'")}
    """
    client = {camel_name}Client(tenant_id=tenant_id, mode=mode)
    return client.{fn_name}({call_args})


if __name__ == "__main__":
    result = {fn_name}()
    print("[OwnAPI] Sonuç:", str(result)[:500])
'''

    schema_obj = RegistrySchema(
        service_name=service_name,
        target_url=target_url,
        goal=description or f"{service_name} API çağrısı",
        function_name=fn_name,
        explanation=f"{service_name} servisi için otomatik derlenmiş OwnAPI istemcisi.",
        method=method,
        endpoint_url=ep_url,
        dynamic_params=dynamic_params,
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    return client_code, schema_obj


def _select_meaningful_endpoints(endpoints: list[dict[str, Any]], target_url: str = "") -> list[dict[str, Any]]:
    """
    Yakalanan 200+ XHR/Fetch isteği arasından:
    1. Statik dosyaları (`.css`, `.js`, resimler, `static.licdn.com`, `/aero-v1/`) ve
       izin listesi/telemetri (`allowlist`, `/litms/`, `sensorCollect`, `realtimeFrontend`) URL'lerini eler.
    2. URL ve parametrelerde `search`, `jobs`, `query`, `voyager`, `api`, `keywords` geçenleri
       `_score_endpoint_priority` ile puanlayarak en alakalı veri uç noktalarını en üste taşır.
    3. Tüm istek gövdelerini ve yanıt örneklerini maksimum 2000 karakterle sınırlar (truncate eder).
    """
    scored_candidates: list[tuple[int, int, dict[str, Any]]] = []
    fallback_non_static: list[tuple[int, int, dict[str, Any]]] = []

    for idx, ep in enumerate(endpoints):
        full_u = str(ep.get("full_url") or ep.get("url") or "").strip()
        if _is_static_asset_url(full_u):
            continue

        sanitized_ep = _sanitize_endpoint_for_llm(ep, max_chars=MAX_LLM_FIELD_CHARS)
        status = sanitized_ep.get("status_code", 0)
        if not (200 <= status < 400):
            continue

        priority_score = _score_endpoint_priority(sanitized_ep, target_url=target_url)
        if priority_score < -100:
            continue

        fallback_non_static.append((priority_score, -idx, sanitized_ep))
        resp = str(sanitized_ep.get("response_sample", "")).strip()
        url_l = full_u.lower()
        has_priority_kw = any(kw in url_l for kw in PRIORITY_ENDPOINT_KEYWORDS) or "keywords" in url_l
        if resp.startswith("{") or resp.startswith("[") or "0:{" in resp or has_priority_kw:
            scored_candidates.append((priority_score, -idx, sanitized_ep))

    if scored_candidates:
        scored_candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in scored_candidates]

    if fallback_non_static:
        fallback_non_static.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in fallback_non_static[:10]]

    if endpoints:
        return [_sanitize_endpoint_for_llm(ep, max_chars=MAX_LLM_FIELD_CHARS) for ep in endpoints[:5]]
    return []


def _compile_with_llm(
    service_name: str,
    target_url: str,
    description: str,
    endpoints: list[dict[str, Any]],
) -> tuple[str, RegistrySchema, dict[str, Any]]:
    """
    OpenAI GPT-4o kullanarak statik dosyalardan/allowlist'lerden arındırılmış,
    `search`/`jobs`/`query`/`voyager`/`api` önceliklendirilmiş ve 2000 karakterle
    truncate edilmiş API uç noktalarını analiz edip BaseOwnClient kodunu derler.
    """
    load_dotenv(override=False)
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY bulunamadı!")

    client = OpenAI(api_key=api_key)
    model = os.getenv("OPENAI_MODEL", "gpt-4o").strip()

    filtered_endpoints = _select_meaningful_endpoints(endpoints, target_url=target_url)
    if not filtered_endpoints:
        raise ValueError("Yakalanan trafikte anlamlı bir JSON API uç noktası bulunamadı.")

    # LLM'e gönderilecek uç noktaları tekrar 2000 karakter sınırından geçir (128k token koruması)
    llm_ready_endpoints = [_sanitize_endpoint_for_llm(ep, max_chars=MAX_LLM_FIELD_CHARS) for ep in filtered_endpoints[:8]]
    camel_name = _to_camel_case(service_name)
    target_search_params = _extract_url_search_params(target_url)

    prompt = f"""\
Aşağıda kullanıcının `{start_url_to_domain(target_url)}` sitesinde yaptığı işlem sırasında yakalanan (statik `.css`/`.js`/resim/allowlist dosyalarından arındırılmış, `search`/`jobs`/`query`/`voyager`/`api` önceliğine göre sıralanmış ve gövdeleri maksimum 2000 karakterle sınırlandırılmış) XHR/Fetch API uç noktaları verilmiştir.

Kullanıcı Hedefi/Açıklaması: {description or service_name}
Servis Adı: {service_name}
Hedef URL: {target_url}
Hedef URL Arama Parametreleri: {json.dumps(target_search_params, ensure_ascii=False)}

Yakalanan Öncelikli Uç Noktalar:
{json.dumps(llm_ready_endpoints, ensure_ascii=False, indent=2)}

GÖREVİN:
1. AKILLI ENDPOINT SEÇİMİ: Rastgele bir meta veri (`litms`, `allowlist`, `metadata/user`) veya bildirim uç noktası SEÇME! URL, `query_params` veya `request_payload` içinde `search`, `jobs`, `query`, `voyager`, `api` veya `keywords` geçen EN UYGUN ASIL iş/arama/veri uç noktasını (`matched_endpoint_id`) seç.
2. PARAMETRE EŞLEME (`keywords` / `query`): Hedef URL'de veya seçilen uç noktada `keywords` (örneğin `"AI Engineer"`), `query`, `q` veya `location` gibi arama/filtreleme parametreleri varsa, bunları `client.py` fonksiyon imzasına dinamik argüman (örn. `keywords: str = "AI Engineer"`) olarak enjekte et ve `dynamic_params` şemasına ekle ki Live Playground veya AI Ajanı üzerinden arama kelimesi dinamik olarak değiştirilebilsin.
3. Uç noktanın `query_params` ve `headers` alanlarındaki ZORUNLU parametreleri (örneğin `referer`, `csrf-token` veya `x-` başlıkları) eksiksiz koru. `query_params` içindeki tek elemanlı listeleri (`["deger"]`) düz string (`"deger"`) olarak gönder.
4. `core.base_client.BaseOwnClient` sınıfından miras alan, `self.request(...)` metodunu kullanan, `tenant_id: str = "default"` ve `mode: str = "local"` parametrelerini destekleyen eksiksiz, temiz ve hatasız bir `client.py` Python kodu üret.

KESİN PYTHON SÖZDİZİMİ (STRICT PYTHON SYNTAX) KURALLARI:
- Ürettiğin `client_code` doğrudan `ast.parse()` ile derlenecektir; kesinlikle `SyntaxError` veya `IndentationError` içermemelidir.
- Fonksiyon imzalarında ASLA boş veya fazladan virgül bırakma (örneğin dinamik parametre yoksa `def fn(self) -> dict[str, Any]:` yaz, ASLA `def fn(self, ,):` yazma).
- Python sözlük/liste tanımlamalarında JSON sabitleri olan `true`, `false`, `null` KULLANMA; mutlaka geçerli Python sabitleri olan `True`, `False`, `None` kullan.
- `client_code` alanına Markdown kod bloğu işaretleri (```python ... ```) KOYMA, yalnızca saf Python kaynak kodu döndür.
- Tüm parantez `()`, köşeli parantez `[]`, süslü parantez `{{}}` ve tırnak işaretlerinin eksiksiz kapandığından emin ol.
- `self.request(...)` zaten ayrıştırılmış bir `dict` (sözlük) döndürür; gelen yanıt üzerinde `.text` özniteliği çağırma, doğrudan `res` döndür veya `res.get("text", json.dumps(res, ensure_ascii=False))` kullan.

Kod yapısı MUTLAKA şu standartta olmalıdır:
from __future__ import annotations
import copy
import json
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from core.base_client import BaseOwnClient
from core.token_store import TokenStore

class {camel_name}Client(BaseOwnClient):
    def __init__(self, tenant_id: str = "default", token_store: TokenStore | None = None, mode: str = "local") -> None:
        super().__init__(service_name="{service_name}", base_url="{target_url}", tenant_id=tenant_id, token_store=token_store, mode=mode)

    def <fonksiyon_adi>(self, <varsa_dinamik_parametreler_varsayilan_degerli>) -> dict[str, Any]:
        res = self.request(method=..., url=..., headers=..., params=..., json_payload=...)
        return res if isinstance(res, dict) else {{"data": res}}

def <fonksiyon_adi>(<varsa_dinamik_parametreler_varsayilan_degerli>, tenant_id: str = "default", mode: str = "local") -> dict[str, Any]:
    \"\"\"Docstring\"\"\"
    client = {camel_name}Client(tenant_id=tenant_id, mode=mode)
    return client.<fonksiyon_adi>(...)

Yanıtını SADECE aşağıdaki JSON formatında ver:
{{
  "matched_endpoint_id": <int>,
  "function_name": "<snake_case_fonksiyon_adi>",
  "explanation": "<Kısa Türkçe açıklama>",
  "dynamic_params": [
    {{
      "name": "<arguman_adi>",
      "original_key": "<orijinal_anahtar>",
      "location": "query | body | path",
      "param_type": "str | int | float | bool",
      "default_value": <varsayilan_deger>,
      "description": "<açıklama>"
    }}
  ],
  "client_code": "<tam python kodu>"
}}
"""

    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        response_format={"type": "json_object"},
    )

    content = resp.choices[0].message.content or "{}"
    parsed = json.loads(content)

    matched_id = parsed.get("matched_endpoint_id", 1)
    matched_ep = next((e for e in filtered_endpoints if e.get("id") == matched_id), filtered_endpoints[0])

    dyn_params: list[DynamicParamSpec] = []
    for dp in parsed.get("dynamic_params", []):
        try:
            dyn_params.append(DynamicParamSpec(**dp))
        except Exception:
            pass

    # Hedef URL'de `keywords` varsa ve LLM dynamic_params listesine eklemediyse otomatik tamamla
    if "keywords" in target_search_params and not any(p.name == "keywords" for p in dyn_params):
        dyn_params.insert(
            0,
            DynamicParamSpec(
                name="keywords",
                original_key="keywords",
                location="query",
                param_type="str",
                default_value=target_search_params["keywords"],
                description="Aranacak pozisyon, iş unvanı veya anahtar kelime.",
            ),
        )

    fn_name = parsed.get("function_name", f"search_{service_name}")
    raw_client_code = parsed.get("client_code", "")
    client_code = _clean_and_validate_python_code(raw_client_code)

    schema_obj = RegistrySchema(
        service_name=service_name,
        target_url=target_url,
        goal=description or f"{service_name} API sorgusu",
        function_name=fn_name,
        explanation=parsed.get("explanation", f"{service_name} için otomatik derlenmiş OwnAPI aracı."),
        method=matched_ep.get("method", "GET").upper(),
        endpoint_url=matched_ep.get("url", target_url),
        dynamic_params=dyn_params,
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    return client_code, schema_obj, matched_ep


def _repair_client_with_llm(
    service_name: str,
    target_url: str,
    description: str,
    matched_endpoint: dict[str, Any],
    all_endpoints: list[dict[str, Any]],
    broken_code: str,
    function_name: str,
    error_detail: str,
) -> tuple[str, str]:
    """
    Auto-Test (Validator Loop) sırasında hata alındığında, hatayı, mevcut kodu ve
    2000 karakterle sınırlandırılmış orijinal trafik şemasını GPT-4o'ya geri besleyerek kodu otomatik onarır.
    """
    load_dotenv(override=False)
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        fallback_code, fallback_schema = _build_fallback_code(service_name, target_url, matched_endpoint, description)
        return fallback_code, fallback_schema.function_name

    client = OpenAI(api_key=api_key)
    model = os.getenv("OPENAI_MODEL", "gpt-4o").strip()
    camel_name = _to_camel_case(service_name)
    filtered_endpoints = [_sanitize_endpoint_for_llm(ep, max_chars=MAX_LLM_FIELD_CHARS) for ep in _select_meaningful_endpoints(all_endpoints)[:6]]
    sanitized_matched = _sanitize_endpoint_for_llm(matched_endpoint, max_chars=MAX_LLM_FIELD_CHARS)

    repair_prompt = f"""\
Ürettiğin `registry/{service_name}/client.py` kodu Auto-Test (Validator Loop) aşamasında çalıştırıldığında HATA verdi veya geçersiz yanıt döndürdü.

Servis Adı: {service_name}
Hedef URL: {target_url}
Beklenen Ana Fonksiyon Adı: {function_name}

ALINAN HATA / BAŞARISIZ YANIT:
{str(error_detail)[:MAX_LLM_FIELD_CHARS]}

MEVCUT HATALI KOD (`client.py`):
```python
{broken_code[:4000]}
```

SEÇİLEN ORİJİNAL UÇ NOKTA KAYDI (`captured_traffic.json` - 2000 karakter truncate):
{json.dumps(sanitized_matched, ensure_ascii=False, indent=2)}

ALTERNATİF YAKALANAN UÇ NOKTALAR:
{json.dumps(filtered_endpoints, ensure_ascii=False, indent=2)}

GÖREVİN:
1. Hatayı analiz et (örneğin SyntaxError, eksik query_params, yanlış URL, eksik header, yanlış JSON body, argümanların varsayılan değerinin olmaması veya yanlış uç nokta seçimi).
2. DİKKAT: `self.request(...)` metodu HTTP Response nesnesi DEĞİL, doğrudan `dict` (sözlük) döndürür! Bu nedenle `response.text` ÇAĞIRMA; gelen yanıt zaten `dict` ise `.text` aramaya kalkmadan doğrudan `res` (sözlük) döndür veya string gerekiyorsa `res.get("text", json.dumps(res, ensure_ascii=False)) if isinstance(res, dict) else str(res)` olarak işle.
3. KESİN PYTHON SÖZDİZİMİ (STRICT PYTHON SYNTAX) KURALLARI:
   - Fonksiyon imzalarında ASLA boş virgül (`def fn(self, ,):`) bırakma.
   - Python sözlüklerinde JSON sabitleri (`true`, `false`, `null`) DEĞİL, Python sabitleri (`True`, `False`, `None`) kullan.
   - `client_code` içine ```python çitleri koyma; doğrudan `ast.parse()` ile hatasız derlenen saf Python kodu döndür.
4. Tüm argümanlara yakalanan trafikten geçerli varsayılan değerler (`default_value`) ver ki `{function_name}()` parametresiz çağrıldığında doğrudan 200 OK geçerli JSON döndürsün.
5. `BaseOwnClient` sınıfından miras alan (`{camel_name}Client(BaseOwnClient)`) ve modül seviyesinde `{function_name}(..., tenant_id: str = "default", mode: str = "local")` fonksiyonunu tanımlayan düzeltilmiş tam Python kodunu üret.

Yanıtını SADECE aşağıdaki JSON formatında ver:
{{
  "function_name": "{function_name}",
  "fix_explanation": "<Yapılan düzeltmenin kısa Türkçe açıklaması>",
  "client_code": "<düzeltilmiş tam python kodu>"
}}
"""

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": repair_prompt}],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        parsed = json.loads(resp.choices[0].message.content or "{}")
        repaired_code = _clean_and_validate_python_code(parsed.get("client_code", ""))
        repaired_fn = parsed.get("function_name", function_name).strip() or function_name
        fix_exp = parsed.get("fix_explanation", "Otomatik LLM onarımı uygulandı.")
        console.print(f"[bold cyan]🔧 GPT-4o Onarım Açıklaması:[/bold cyan] {fix_exp}")
        if repaired_code:
            return repaired_code, repaired_fn
    except Exception as exc:
        console.print(f"[dim yellow]⚠️ LLM onarım çağrısı başarısız ({exc}), deterministik şablona dönülüyor...[/dim yellow]")

    fallback_code, fallback_schema = _build_fallback_code(service_name, target_url, matched_endpoint, description)
    return fallback_code, fallback_schema.function_name


def _is_valid_json_response(result: Any) -> tuple[bool, str]:
    """
    Test çağrısından dönen yanıtın geçerli ve hatasız bir JSON/veri yanıtı olup olmadığını denetler.
    """
    if result is None:
        return False, "Yanıt None döndü."
    if isinstance(result, str):
        cleaned = result.strip()
        if not cleaned:
            return False, "Yanıt boş string ('') döndü."
        return True, f"Geçerli metin/JSON yanıtı ({len(cleaned)} karakter)"
    if not isinstance(result, (dict, list)):
        return False, f"Yanıt JSON (dict/list/str) değil: {type(result).__name__}"
    if isinstance(result, list):
        return True, f"Geçerli JSON listesi ({len(result)} öğe)"
    if not result:
        return False, "Yanıt boş sözlük ({}) döndü."

    # SessionHealer ayrıştırma hatası veya HTTP 4xx/5xx kontrolü
    if result.get("error") == "Yanıt JSON formatında ayrıştırılamadı":
        return False, f"JSON ayrıştırma hatası (status={result.get('status_code')}): {str(result.get('text', ''))[:250]}"

    status_code = result.get("status_code") or result.get("statusCode") or result.get("status")
    if isinstance(status_code, int) and status_code >= 400:
        return False, f"HTTP Hata Kodu ({status_code}): {json.dumps(result, ensure_ascii=False)[:300]}"

    if result.get("success") is False and (result.get("error") or result.get("errors")):
        return False, f"API Başarısız Yanıtı: {json.dumps(result, ensure_ascii=False)[:300]}"

    return True, f"Geçerli JSON nesnesi (Anahtarlar: {list(result.keys())[:6]})"


def _load_and_call_client(
    client_path: Path,
    function_name: str,
    tenant_id: str = "default",
    mode: str = "local",
) -> Any:
    """Üretilen client.py modülünü dinamik yükler ve ana fonksiyonu varsayılan argümanlarla çağırır."""
    mod_name = f"own_api_validator_{client_path.parent.name}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:4]}"
    spec = importlib.util.spec_from_file_location(mod_name, str(client_path.resolve()))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{client_path} yüklenemedi.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)

    fn = getattr(module, function_name, None)
    if fn is None:
        # Modül içindeki ilk public fonksiyonu bul
        import inspect
        funcs = [
            obj for name, obj in inspect.getmembers(module, inspect.isfunction)
            if obj.__module__ == mod_name and not name.startswith("_")
        ]
        if not funcs:
            raise AttributeError(f"`{function_name}` fonksiyonu {client_path.name} içinde bulunamadı.")
        fn = funcs[0]

    import inspect
    sig = inspect.signature(fn)
    kwargs: dict[str, Any] = {}
    if "tenant_id" in sig.parameters:
        kwargs["tenant_id"] = tenant_id
    if "mode" in sig.parameters:
        kwargs["mode"] = mode

    return fn(**kwargs)


def validate_and_repair_client(
    service_name: str,
    target_url: str,
    description: str,
    client_path: Path,
    schema_obj: RegistrySchema,
    matched_endpoint: dict[str, Any],
    all_endpoints: list[dict[str, Any]],
    validation_runs: int = 3,
    max_repair_attempts: int = 3,
    tenant_id: str = "default",
    mode: str = "local",
) -> dict[str, Any]:
    """
    Compiler + Validator Loop (Auto-Test Katmanı):
    Üretilen `client.py` dosyasını otomatik olarak `validation_runs` (3) kez test çağrısına tabi tutar.
    Eğer 3 çağrının 3'ü de başarılı JSON döndürürse 'BAŞARILI' olarak kaydeder.
    Hata alınırsa hatayı GPT-4o'ya geri besleyip kodu düzelttirir ve 3'lü testi tekrarlar.
    """
    console.print(
        Panel.fit(
            f"[bold cyan]🧪 Compiler + Validator Loop (Auto-Test Katmanı) Başlatıldı[/bold cyan]\n"
            f"[white]Servis:[/white] [magenta]{service_name}[/magenta] | "
            f"[white]Fonksiyon:[/white] [yellow]{schema_obj.function_name}()[/yellow]\n"
            f"[dim]Hedef: {validation_runs}/{validation_runs} ardışık başarılı JSON yanıtı doğrulaması[/dim]",
            title="OwnAPI Auto-Validator",
            border_style="cyan",
        )
    )

    current_code = client_path.read_text(encoding="utf-8")
    current_fn_name = schema_obj.function_name
    repair_count = 0
    last_error = ""

    for attempt in range(max_repair_attempts + 1):
        passed_runs = 0
        run_logs: list[dict[str, Any]] = []
        failed_reason = ""

        for run_idx in range(1, validation_runs + 1):
            console.print(
                f"[bold yellow]▶ Auto-Test Çağrısı [{run_idx}/{validation_runs}][/bold yellow] "
                f"(Onarım Turu #{attempt})..."
            )
            start_t = time.perf_counter()
            try:
                res = _load_and_call_client(
                    client_path=client_path,
                    function_name=current_fn_name,
                    tenant_id=tenant_id,
                    mode=mode,
                )
                elapsed_ms = (time.perf_counter() - start_t) * 1000.0
                is_ok, detail = _is_valid_json_response(res)
                if is_ok:
                    passed_runs += 1
                    run_logs.append({
                        "run": run_idx,
                        "status": "BAŞARILI",
                        "elapsed_ms": round(elapsed_ms, 2),
                        "detail": detail,
                    })
                    console.print(
                        f"  [bold green]✔ Test {run_idx}/{validation_runs} BAŞARILI[/bold green] "
                        f"({elapsed_ms:.0f} ms) — [dim]{detail}[/dim]"
                    )
                else:
                    failed_reason = f"Test {run_idx}/{validation_runs} geçersiz yanıt döndürdü: {detail}"
                    run_logs.append({
                        "run": run_idx,
                        "status": "BAŞARISIZ",
                        "elapsed_ms": round(elapsed_ms, 2),
                        "detail": detail,
                    })
                    console.print(f"  [bold red]✖ {failed_reason}[/bold red]")
                    break
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - start_t) * 1000.0
                tb_str = traceback.format_exc(limit=4)
                failed_reason = f"Test {run_idx}/{validation_runs} çalışma zamanı hatası: {exc}\n{tb_str}"
                run_logs.append({
                    "run": run_idx,
                    "status": "HATA",
                    "elapsed_ms": round(elapsed_ms, 2),
                    "detail": str(exc),
                })
                console.print(f"  [bold red]✖ Test {run_idx}/{validation_runs} HATA:[/bold red] {exc}")
                break

        if passed_runs == validation_runs:
            table = Table(
                title=f"✅ OwnAPI Auto-Test Doğrulama Raporu ({service_name} — {validation_runs}/{validation_runs} BAŞARILI)",
                show_header=True,
                header_style="bold green",
                show_lines=True,
            )
            table.add_column("Test Çağrısı", justify="center", style="bold cyan")
            table.add_column("Durum", justify="center", style="bold green")
            table.add_column("Süre (ms)", justify="right", style="yellow")
            table.add_column("JSON Doğrulama Özeti", style="white")
            for rlog in run_logs:
                table.add_row(
                    f"#{rlog['run']}/{validation_runs}",
                    f"🟢 {rlog['status']}",
                    f"{rlog['elapsed_ms']:.1f} ms",
                    rlog["detail"],
                )
            console.print()
            console.print(table)
            console.print()
            return {
                "validation_status": "BAŞARILI",
                "passed_runs": passed_runs,
                "required_runs": validation_runs,
                "repair_attempts": repair_count,
                "function_name": current_fn_name,
                "runs": run_logs,
            }

        last_error = failed_reason
        if attempt < max_repair_attempts:
            repair_count += 1
            console.print(
                f"\n[bold yellow]🔄 Auto-Repair #{repair_count}/{max_repair_attempts}: "
                f"Hata GPT-4o'ya geri besleniyor ve `client.py` yeniden derleniyor...[/bold yellow]"
            )
            repaired_code, repaired_fn = _repair_client_with_llm(
                service_name=service_name,
                target_url=target_url,
                description=description,
                matched_endpoint=matched_endpoint,
                all_endpoints=all_endpoints,
                broken_code=current_code,
                function_name=current_fn_name,
                error_detail=last_error,
            )
            current_code = repaired_code
            current_fn_name = repaired_fn
            schema_obj.function_name = current_fn_name
            client_path.write_text(current_code.strip() + "\n", encoding="utf-8")

    console.print(
        f"[bold red]⚠️ Auto-Test Katmanı {max_repair_attempts} onarım denemesine rağmen "
        f"{validation_runs}/{validation_runs} doğrulamayı tamamlayamadı.[/bold red]"
    )
    return {
        "validation_status": "KISMİ_VEYA_HATALI",
        "passed_runs": 0,
        "required_runs": validation_runs,
        "repair_attempts": repair_count,
        "function_name": current_fn_name,
        "last_error": last_error,
    }


def start_url_to_domain(url: str) -> str:
    try:
        return urlparse(url).netloc or url
    except Exception:
        return url


def onboard_new_site(
    service_name: str,
    start_url: str,
    description: str = "",
    headless: bool = False,
    registry_root: Path | str = PROJECT_ROOT / "registry",
    tenant_id: str = "default",
    mode: str = "local",
    auto_validate: bool = True,
    validation_runs: int = 3,
    interactive_tos: bool = False,
    wait_seconds: int | None = None,
) -> dict[str, Any]:
    """
    Kullanıcı kod yazmadan yeni bir siteyi OwnAPI mimarisine ekler:
    1. ToS / Kullanıcı Delegasyon Onay Mekanizmasını (`ConsentManager`) çalıştırır ve loglar.
    2. Temiz Chrome açıp trafiği ve oturum çerezlerini dinler.
    3. `registry/{service_name}/captured_traffic.json` dosyasına kaydeder.
    4. OpenAI GPT-4o (veya fallback şablonu) ile `BaseOwnClient` uyumlu `client.py`,
       `schema.json` ve `__init__.py` dosyalarını derler.
    5. Compiler + Validator Loop (Auto-Test Katmanı): Üretilen `client.py` dosyasını otomatik
       olarak 3 kez test çağrısına tabi tutar; hata alınırsa GPT-4o'ya hatayı geri verip
       kodu düzelttirir ve 3/3 başarılı JSON alındığında 'BAŞARILI' olarak kaydeder.
    """
    reg_root = Path(registry_root)
    clean_name = _sanitize_service_name(service_name)
    service_dir = reg_root / clean_name
    service_dir.mkdir(parents=True, exist_ok=True)

    traffic_path = service_dir / "captured_traffic.json"
    client_path = service_dir / "client.py"
    schema_path = service_dir / "schema.json"
    init_path = service_dir / "__init__.py"

    # 1. ToS / Kullanıcı Delegasyon Onayı
    consent_mgr = ConsentManager()
    consent_record = consent_mgr.ensure_consent(
        tenant_id=tenant_id,
        service=clean_name,
        action="onboarding",
        mode=mode,
        interactive=interactive_tos,
        force_banner=True,
    )

    console.print(
        Panel.fit(
            f"[bold cyan]🌟 Yeni Servis Onboarding Başlatıldı:[/bold cyan] [white]{clean_name}[/white]\n"
            f"[dim]Hedef URL:[/dim] [green]{start_url}[/green]\n"
            f"[dim]Açıklama:[/dim] [yellow]{description or 'Belirtilmedi'}[/yellow]\n"
            f"[dim]Çalışma Modu:[/dim] [cyan]{mode.upper()}[/cyan]\n"
            f"[dim]Hedef Dizin:[/dim] [magenta]{service_dir}[/magenta]",
            title="OwnAPI Onboarding Engine",
            border_style="cyan",
        )
    )

    # 2. Trafiği Kaydet (Eğer zaten captured_traffic.json yoksa veya boşsa tarayıcı aç)
    try:
        traffic_data = asyncio.run(
            _record_traffic_async(
                start_url=start_url,
                output_path=traffic_path,
                service_name=clean_name,
                tenant_id=tenant_id,
                headless=headless,
                wait_seconds=wait_seconds,
            )
        )
    except RuntimeError:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            traffic_data = pool.submit(
                asyncio.run,
                _record_traffic_async(
                    start_url=start_url,
                    output_path=traffic_path,
                    service_name=clean_name,
                    tenant_id=tenant_id,
                    headless=headless,
                    wait_seconds=wait_seconds,
                ),
            ).result()

    endpoints = traffic_data.get("endpoints", [])
    if not endpoints:
        console.print("[bold red]❌ Ağ trafiği yakalanamadı veya oturum erken sonlandırıldı.[/bold red]")
        return {
            "success": False,
            "error": "Ağ trafiği yakalanamadı.",
            "service_name": clean_name,
        }

    # 3. LLM veya Fallback ile Derle
    console.print("\n[bold yellow]🧠 Yakalanan API uç noktaları analiz ediliyor ve kod derleniyor...[/bold yellow]")
    client_code = ""
    schema_obj: RegistrySchema | None = None
    matched_ep: dict[str, Any] = {}

    try:
        client_code, schema_obj, matched_ep = _compile_with_llm(clean_name, start_url, description, endpoints)
        console.print("[bold green]✔ OpenAI GPT-4o ile akıllı derleme tamamlandı![/bold green]")
    except Exception as exc:
        console.print(f"[dim yellow]⚠️ OpenAI API çağrısı atlandı/hata verdi ({exc}), standart BaseOwnClient şablonu kullanılıyor...[/dim yellow]")
        meaningful = _select_meaningful_endpoints(endpoints)
        matched_ep = meaningful[0] if meaningful else endpoints[0]
        client_code, schema_obj = _build_fallback_code(clean_name, start_url, matched_ep, description)

    # 4. Dosyaları Diske Yaz
    client_path.write_text(client_code.strip() + "\n", encoding="utf-8")

    # 5. Compiler + Validator Loop (3x Auto-Test & GPT-4o Self-Repair)
    validation_report: dict[str, Any] = {
        "validation_status": "ATLANDI",
        "passed_runs": 0,
        "required_runs": validation_runs,
    }
    if auto_validate:
        validation_report = validate_and_repair_client(
            service_name=clean_name,
            target_url=start_url,
            description=description,
            client_path=client_path,
            schema_obj=schema_obj,
            matched_endpoint=matched_ep,
            all_endpoints=endpoints,
            validation_runs=validation_runs,
            max_repair_attempts=3,
            tenant_id=tenant_id,
            mode=mode,
        )

    # Şema JSON dosyasına doğrulama durumunu da ekle
    schema_dict = schema_obj.model_dump()
    schema_dict["validation_status"] = validation_report.get("validation_status", "BAŞARILI")
    schema_dict["validation_passed_runs"] = validation_report.get("passed_runs", validation_runs)
    schema_path.write_text(json.dumps(schema_dict, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    camel_name = _to_camel_case(clean_name)
    init_content = f'''"""OwnAPI Registry Module: {clean_name}."""

from .client import {schema_obj.function_name}, {camel_name}Client

__all__ = ["{schema_obj.function_name}", "{camel_name}Client"]
'''
    init_path.write_text(init_content, encoding="utf-8")

    val_badge = (
        f"[bold green]🟢 {validation_report['validation_status']} "
        f"({validation_report.get('passed_runs', 0)}/{validation_runs} Test Başarılı)[/bold green]"
        if validation_report.get("validation_status") == "BAŞARILI"
        else f"[bold yellow]🟡 {validation_report.get('validation_status')}[/bold yellow]"
    )

    console.print(
        Panel.fit(
            f"[bold green]✨ Servis Başarıyla Derlendi, Doğrulandı ve Kaydedildi![/bold green]\n"
            f"[white]Servis Adı:[/white] [cyan]{clean_name}[/cyan]\n"
            f"[white]Fonksiyon:[/white] [green]{schema_obj.function_name}[/green]\n"
            f"[white]Endpoint:[/white] [yellow]{schema_obj.method} {schema_obj.endpoint_url}[/yellow]\n"
            f"[white]Auto-Test Durumu:[/white] {val_badge}\n"
            f"[white]Parametreler:[/white] {', '.join(p.name for p in schema_obj.dynamic_params) or 'Yok'}\n\n"
            f"[dim]Oluşturulan Dosyalar:[/dim]\n"
            f" • {client_path.name}\n"
            f" • {schema_path.name}\n"
            f" • {init_path.name}\n"
            f" • {traffic_path.name}",
            title="OwnAPI Onboarding Tamamlandı",
            border_style="green",
        )
    )

    return {
        "success": True,
        "service_name": clean_name,
        "function_name": schema_obj.function_name,
        "endpoint_url": schema_obj.endpoint_url,
        "method": schema_obj.method,
        "validation_status": validation_report.get("validation_status"),
        "validation_report": validation_report,
        "consent_record": consent_record,
        "dynamic_params": [p.model_dump() for p in schema_obj.dynamic_params],
        "files_created": [
            str(traffic_path),
            str(client_path),
            str(schema_path),
            str(init_path),
        ],
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="OwnAPI Otomatik Site Ekleme (Onboarding + Auto-Test)")
    parser.add_argument("service_name", help="Eklenecek servis adı (örn: biletix)")
    parser.add_argument("start_url", help="Giriş sayfası URL'si")
    parser.add_argument("--desc", default="", help="İsteğe bağlı servis açıklaması")
    parser.add_argument("--mode", default="local", choices=["local", "cloud"], help="Çalışma modu (local veya cloud)")
    args = parser.parse_args()
    onboard_new_site(args.service_name, args.start_url, args.desc, mode=args.mode)
