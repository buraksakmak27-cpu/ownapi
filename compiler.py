from __future__ import annotations

import ast
import copy
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from pydantic import BaseModel, Field
from rich.console import Console
from rich.panel import Panel

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from recorder import CapturedEndpoint, CaptureSession

console = Console(stderr=True, legacy_windows=False)

# Varsayılan Tarayıcı Parmak İzi (Browser Fingerprint Spoofing)
DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)

# httpx tarafından otomatik yönetilen alt seviye aktarım başlıkları
EXCLUDED_HEADERS: frozenset[str] = frozenset(
    {
        "accept-encoding",
        "accept-language",
        "content-length",
        "host",
        "connection",
        "keep-alive",
        "transfer-encoding",
        "te",
        "trailer",
        "upgrade",
        "proxy-authorization",
        "proxy-authenticate",
        "cache-control",
        "pragma",
        "priority",
    }
)

# Korunması gereken kritik kimlik doğrulama, oturum ve tarayıcı parmak izi başlıkları
ALLOWED_HEADER_PREFIXES: tuple[str, ...] = (
    "authorization",
    "cookie",
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
    "x-algolia-",
    "x-api-key",
    "api-key",
    "x-csrf",
    "x-xsrf",
    "csrf",
    "xsrf",
    "x-auth",
    "x-session",
    "x-token",
    "x-requested-with",
    "x-px",
    "px-",
    "_px",
    "pxcts",
    "x-perimeterx",
    "tafsessionid",
    "taftrackingid",
    "x-current-page-url",
)

# LLM promptuna dahil edilmeyecek 3. taraf reklam, piksel, telemetri ve analitik domain parçaları
THIRD_PARTY_IGNORE_KEYWORDS: tuple[str, ...] = (
    "yandex",
    "evergage",
    "useinsider",
    "openai",
    "creativecdn",
    "firebase",
    "google-analytics",
    "googletagmanager",
    "doubleclick",
    "googleadservices",
    "facebook",
    "criteo",
    "hotjar",
    "clarity.ms",
    "sentry",
    "mixpanel",
    "amplitude",
    "datadog",
    "newrelic",
    "adroll",
    "taboola",
    "outbrain",
    "tiktok",
    "snapchat",
    "quantummetric",
    "dynatrace",
    "adform",
    "tapad",
    "yabs",
    "pixel",
    "telemetry",
    "beacon",
)


def extract_root_domain(url_or_domain: str) -> str:
    """URL veya domain adından kök domaini (örn: web.flypgs.com -> flypgs.com) döndürür."""
    parsed = urlparse(url_or_domain if "://" in url_or_domain else f"https://{url_or_domain}")
    host = (parsed.hostname or url_or_domain).lower().strip()
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("com", "org", "gov", "edu", "net", "co"):
        return ".".join(parts[-3:])
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


class DynamicParamSpec(BaseModel):
    """LLM tarafından tespit edilen dinamik parametre tanımı."""

    name: str = Field(..., description="Python fonksiyon argümanı adı (snake_case, örn: query, page)")
    original_key: str = Field(..., description="Payload veya query_params içindeki orijinal anahtar adı")
    location: str = Field(..., description="Parametrenin yeri: 'body', 'query' veya 'path'")
    param_type: str = Field(default="str", description="Python tipi: 'str', 'int', 'float', 'bool'")
    default_value: Any = Field(..., description="Yakalanan son istekteki örnek/varsayılan değer")
    description: str = Field(default="", description="Parametrenin kısa açıklaması")


class RegistrySchema(BaseModel):
    """registry/<servis_adi>/schema.json için kalıcı servis şeması."""

    service_name: str
    target_url: str
    goal: str
    function_name: str
    explanation: str
    method: str
    endpoint_url: str
    dynamic_params: list[DynamicParamSpec] = Field(default_factory=list)
    created_at: str


class CompilationResult(BaseModel):
    """Derleme sonucunda üretilen meta veri ve kaynak kodlar."""

    service_name: str = "default"
    provider: str
    model: str
    matched_endpoint_id: int
    matched_method: str
    matched_url: str
    function_name: str
    explanation: str
    dynamic_params: list[DynamicParamSpec] = Field(default_factory=list)
    client_code: str
    mcp_code: str
    client_path: Path = Path("generated_client.py")
    mcp_path: Path = Path("generated_mcp.py")
    registry_client_path: Path | None = None
    registry_schema_path: Path | None = None


def derive_service_name(url_or_domain: str) -> str:
    """Hedef URL veya domain adından temiz bir servis adı türetir (örn: https://hn.algolia.com -> hn_algolia)."""
    parsed = urlparse(url_or_domain if "://" in url_or_domain else f"https://{url_or_domain}")
    host = (parsed.hostname or url_or_domain).lower().strip()
    if host.startswith("www."):
        host = host[4:]
    # Bilinen üst seviye uzantıları temizle
    for tld in (".com.tr", ".org.tr", ".gov.tr", ".edu.tr", ".co.uk", ".com", ".net", ".org", ".io", ".dev", ".app", ".ai", ".co", ".me"):
        if host.endswith(tld) and len(host) > len(tld):
            host = host[: -len(tld)]
            break
    sanitized = re.sub(r"[^a-z0-9]+", "_", host).strip("_")
    return sanitized or "shadow_service"


SYSTEM_PROMPT = """\
Sen kıdemli bir Sistem Mühendisi ve API Tersine Mühendislik (Reverse Engineering) uzmanısın.
Görevin, yakalanmış ve tekilleştirilmiş XHR/Fetch ağ trafiği (JSON) içinden kullanıcının belirttiği hedefe (`goal`) karşılık gelen ASIL uç noktayı (endpoint) tespit etmek ve bu uç noktayı doğrudan çağıran iki adet üretim kalitesinde Python dosyası (`generated_client.py` ve `generated_mcp.py`) üretmektir.

KESİN KURallar:
1. Sağlık kontrolü (`isalive`, `ping`, `health`), telemetri veya analitik uç noktalarını DEĞİL, doğrudan kullanıcının hedeflediği işi (örn. arama, listeleme, veri çekme) yapan asıl veri uç noktasını seç.
2. Başlık (Header) Filtrelemesi ve Browser Fingerprint Spoofing (ZORUNLU):
   - `accept-encoding`, `content-length`, `host`, `connection` gibi aktarım başlıklarını koda ekleme.
   - Buna karşılık tarayıcı parmak izi (fingerprint) ve kimlik başlıklarını (`user-agent`, `referer`, `origin`, `accept`, `sec-ch-ua*`, `sec-fetch-*`, `x-requested-with`, `x-algolia-*`, `Authorization`, `Cookie`, `Content-Type`) MUTLAKA koru.
   - Üretilen `headers` sözlüğünde eğer `user-agent` yoksa varsayılan olarak `"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"`, hedef sitenin `referer` başlığı ve `"accept": "application/json"` başlığı ZORUNLU olarak bulunmalıdır.
   - ÖNEMLİ: Eğer yakalanan isteğin `request_payload` alanı bir JSON nesnesi (dict/list) ise ve `content-type` başlığı `application/x-www-form-urlencoded` olarak görünüyorsa, `httpx` çağrısında gövdeyi mutlaka `json=payload` olarak gönder (`data=payload` KULLANMA).
3. Dinamik Parametre Çıkarımı:
   - Kullanıcının amacına göre (ör. arama sorgusu `query`, sayfa numarası `page`, limit `hitsPerPage` vb.) dinamik olması gereken parametreleri fonksiyon argümanı haline getir.
   - Fonksiyon argümanlarına yakalanan son istekteki değerleri varsayılan (default) değer olarak ata ki fonksiyon argümansız çağrıldığında bile doğrudan çalışabilsin.
   - Statik kalan diğer payload ve query parametrelerini fonksiyon içinde koru.
   - Uçuş/Seyahat Arama ve Gidiş-Dönüş (Round-Trip) Desteği:
     * Kullanıcı gidiş-dönüş sorgusu yaptığında (`return_date` parametresi dolu olduğunda), THY ve havayolu API'lerine gönderilen `originDestinationInformationList` payload şemasına hem gidiş (örn. IST -> ESB) hem de dönüş (örn. ESB -> IST) segmentlerini eksiksiz ekle ve `selectedBookerSearch` değerini 'R' yap.
     * `return_date` parametresi boş veya None olduğunda ise tek yönlü (One-Way) arama olarak tek segmentli yapıyı koru ve `selectedBookerSearch` değerini 'O' yap.
4. `generated_client.py` Gereksinimleri (TARAYICI İÇİ JAVASCRIPT ÇALIŞTIRMA VE ENTERPRISE WAF/BOT BYPASS ZORUNLU):
   - DIŞARIDAN DOĞRUDAN HTTP veya `curl_cffi` ISTEĞI ATMA. Akamai, Cloudflare ve cpr_chlge korumaları dışarıdan atılan istekleri anında 403 Forbidden veya 406 ile bloklar.
   - Bunun yerine tarayıcı oturumu üzerinden tarayıcının kendi JavaScript ortamında (`page.evaluate("fetch(...)")`) istek çalıştırılmalıdır.
   - Orijinal Gezinme URL'si (Referer):
     * Tarayıcı oturumunu başlatırken ve `headers` içindeki `referer` alanında kök domain yerine doğrudan genel rezervasyon ana giriş sayfasını (örn. `https://www.turkishairlines.com/tr-tr/ucak-bileti/rezervasyon/`) kullan ki timeout hatası oluşmadan sayfa hızlıca açılsın ve PerimeterX "basılı tut" doğrulaması doğrudan ekrana gelsin.
   - Dosyanın en başına `import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))` ve `from healer import SessionHealer, update_payload_deep` içe aktarımını ekle.
   - Temiz, tip güvenli (type-hinted) ve docstring içeren bir ana fonksiyon yaz.
   - Fonksiyon içinde dinamik parametreleri güncelle (`update_payload_deep`), ardından `healer = SessionHealer()` oluştur.
   - İsteği tarayıcı context'i içinde çalıştırmak ve sonucu almak için doğrudan `return healer.execute_in_browser(method=..., url=..., headers=headers, params=params, json_payload=payload)` çağrısını yap.
   - `if __name__ == "__main__":` bloğunda örnek bir sorgu çalıştır, süreyi ölç ve dönen JSON yanıtının anlamlı bir özetini terminale anlaşılır biçimde yazdır.
5. `generated_mcp.py` Gereksinimleri:
   - `from mcp.server.fastmcp import FastMCP` kullan (`mcp = FastMCP("ShadowAPI")`).
   - Aynı işlevi `@mcp.tool()` dekoratörüyle dışa aç.
   - `if __name__ == "__main__": mcp.run()` bloğunu ekle.
6. Yanıtını SADECE aşağıdaki JSON şemasında döndür (Markdown kod bloğu olmadan veya tek bir ```json bloğu içinde):
{
  "matched_endpoint_id": <int>,
  "function_name": "<snake_case_fonksiyon_adi>",
  "explanation": "<Neden bu uç noktanın seçildiği ve hangi parametrelerin dinamikleştirildiğinin kısa Türkçe açıklaması>",
  "dynamic_params": [
    {
      "name": "<arguman_adi>",
      "original_key": "<orijinal_anahtar>",
      "location": "body | query | path",
      "param_type": "str | int | float | bool",
      "default_value": <varsayilan_deger>,
      "description": "<açıklama>"
    }
  ],
  "client_code": "<generated_client.py tam Python kodu>",
  "mcp_code": "<generated_mcp.py tam Python kodu>"
}
"""


class ShadowAPICompiler:
    """Yakalanan ağ trafiğini LLM yardımıyla Python SDK ve MCP sunucusuna derler."""

    def __init__(
        self,
        traffic_path: Path = Path("captured_traffic.json"),
        client_output_path: Path = Path("generated_client.py"),
        mcp_output_path: Path = Path("generated_mcp.py"),
        registry_root: Path = Path("registry"),
    ) -> None:
        load_dotenv(override=False)
        self.traffic_path = traffic_path
        self.client_output_path = client_output_path
        self.mcp_output_path = mcp_output_path
        self.registry_root = registry_root
        self.provider, self.api_key, self.model = self._detect_llm_provider()

    @staticmethod
    def _detect_llm_provider() -> tuple[str, str, str]:
        """Ortam değişkenlerinden (.env) uygun LLM sağlayıcısını (Anthropic veya OpenAI) seçer."""
        anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        openai_key = os.getenv("OPENAI_API_KEY", "").strip()

        if anthropic_key:
            model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514").strip()
            return ("anthropic", anthropic_key, model)
        if openai_key:
            model = os.getenv("OPENAI_MODEL", "gpt-4o").strip()
            return ("openai", openai_key, model)

        raise RuntimeError(
            "LLM API anahtarı bulunamadı! Lütfen `.env` dosyasına veya ortam değişkenlerine "
            "`ANTHROPIC_API_KEY` veya `OPENAI_API_KEY` ekleyin."
        )

    @staticmethod
    def sanitize_headers(headers: dict[str, str], target_url: str = "") -> dict[str, str]:
        """
        httpx tarafından otomatik yönetilen aktarım başlıklarını temizler ve
        Browser Fingerprint Spoofing (User-Agent, Referer, Accept) başlıklarını zorunlu olarak ekler.
        """
        cleaned: dict[str, str] = {}
        for k, v in headers.items():
            lower_k = k.lower().strip()
            if lower_k in EXCLUDED_HEADERS:
                continue
            if any(lower_k == prefix or lower_k.startswith(prefix) for prefix in ALLOWED_HEADER_PREFIXES):
                cleaned[lower_k] = v

        # Browser Fingerprint Spoofing zorunlu varsayılanlar
        if "user-agent" not in cleaned:
            cleaned["user-agent"] = DEFAULT_USER_AGENT
        if "referer" not in cleaned and target_url:
            cleaned["referer"] = target_url if target_url.endswith("/") else f"{target_url}/"
        if "accept" not in cleaned:
            cleaned["accept"] = "application/json"

        return cleaned

    @classmethod
    def filter_relevant_endpoints(
        cls,
        endpoints: list[CapturedEndpoint],
        target_url: str = "",
    ) -> list[CapturedEndpoint]:
        """
        LLM promptuna gönderilecek uç noktaları süzer:
        1. 3. taraf reklam, piksel, telemetri ve analitik domainlerini (yandex, evergage, useinsider, openai, creativecdn, firebase vb.) eler.
        2. Eğer hedef URL belirtilmişse hedef root domain ile eşleşen ana veri uç noktalarını korur.
        3. Bot algılama sensör/challenge uç noktalarını (örn: /dRRXeAPrg/, /LSrVcDPgS/) veri API'si olmadığı için eler.
        """
        target_root = extract_root_domain(target_url) if target_url else ""
        filtered: list[CapturedEndpoint] = []
        for ep in endpoints:
            dom = ep.domain.lower()
            path = ep.path.lower()

            # 1. 3. taraf reklam, piksel ve analitik filtrelemesi
            if any(kw in dom for kw in THIRD_PARTY_IGNORE_KEYWORDS):
                continue

            # 2. Akamai / WAF sensör ve challenge veri yollarını filtrele
            if any(s in path for s in ("/drrxeaprg/", "/lsrvcdpgs/image", "/_incapsula_resource", "/challenge")):
                continue

            # 3. Hedef domain eşleşmesi (sadece hedef servise ait uç noktalar)
            if target_root:
                ep_root = extract_root_domain(dom)
                is_match = (
                    ep_root == target_root
                    or target_root in dom
                    or (target_root in ("algolia.com", "hackernews") and "algolia" in dom)
                )
                if not is_match:
                    continue

            filtered.append(ep)

        return filtered or endpoints

    @classmethod
    def deduplicate_endpoints(
        cls,
        endpoints: list[CapturedEndpoint],
        target_url: str = "",
    ) -> list[CapturedEndpoint]:
        """
        Aynı (method, domain, path) kombinasyonuna sahip tekrar eden istekleri tekilleştirir
        ve her uç noktanın EN SON yakalanan örneğini (kullanıcının yazmayı bitirdiği son payload'u) korur.
        """
        dedup_map: dict[tuple[str, str, str], CapturedEndpoint] = {}
        for ep in endpoints:
            key = (ep.method.upper(), ep.domain.lower(), ep.path)
            # Her yeni eşleşme öncekini ezer; böylece en son yakalanan örnek kalır.
            cleaned_ep = ep.model_copy(deep=True)
            ref_url = target_url or f"https://{ep.domain}"
            cleaned_ep.headers = cls.sanitize_headers(cleaned_ep.headers, target_url=ref_url)
            dedup_map[key] = cleaned_ep

        return list(dedup_map.values())

    def load_and_prepare_traffic(self) -> tuple[CaptureSession, list[CapturedEndpoint]]:
        """captured_traffic.json dosyasını okur, 3. taraf domainleri filtreler ve tekilleştirilmiş uç noktaları döndürür."""
        if not self.traffic_path.exists():
            raise FileNotFoundError(
                f"Trafik dosyası bulunamadı: {self.traffic_path}. Önce kayıt (record/build) yapmalısınız."
            )

        raw_content = self.traffic_path.read_text(encoding="utf-8")
        session = CaptureSession.model_validate_json(raw_content)

        if not session.endpoints:
            raise ValueError(f"{self.traffic_path} içinde yakalanmış hiçbir uç nokta bulunmuyor.")

        # 1. 3. taraf reklam, piksel, telemetri ve analitikleri filtrele (drop et)
        relevant_endpoints = self.filter_relevant_endpoints(session.endpoints, target_url=session.target_url)

        # 2. Tekilleştir ve son istek halini koru
        deduped = self.deduplicate_endpoints(relevant_endpoints, target_url=session.target_url)
        return session, deduped

    def _build_user_prompt(self, goal: str, target_url: str, endpoints: list[CapturedEndpoint]) -> str:
        """LLM'e gönderilecek kullanıcı istemini hazırlar."""
        serialized_endpoints = [
            {
                "id": ep.id,
                "method": ep.method,
                "url": ep.url,
                "domain": ep.domain,
                "path": ep.path,
                "status_code": ep.status_code,
                "query_params": ep.query_params,
                "headers": ep.headers,
                "request_payload": ep.request_payload,
                "response_sample": ep.response_sample[:1200],
            }
            for ep in endpoints
        ]

        return (
            f"Hedef Web Sitesi: {target_url}\n"
            f"Kullanıcının Hedefi (Goal): {goal}\n\n"
            f"Tekilleştirilmiş Uç Nokta Listesi (Her uç noktanın EN SON yakalanan hali):\n"
            f"{json.dumps(serialized_endpoints, ensure_ascii=False, indent=2)}\n\n"
            "Lütfen hedefe en uygun uç noktayı seç ve `generated_client.py` ile `generated_mcp.py` "
            "dosyalarını istenen JSON formatında üret."
        )

    def _call_anthropic(self, user_prompt: str) -> str:
        """Anthropic Claude API çağrısını gerçekleştirir (model yedeklemeli)."""
        from anthropic import Anthropic

        client = Anthropic(api_key=self.api_key)
        candidate_models = [
            self.model,
            "claude-sonnet-4-20250514",
            "claude-3-7-sonnet-latest",
            "claude-3-5-sonnet-latest",
            "claude-3-5-haiku-latest",
        ]
        # Sırayı koruyarak tekilleştir
        models_to_try = list(dict.fromkeys(candidate_models))

        last_err: Exception | None = None
        for model_name in models_to_try:
            try:
                response = client.messages.create(
                    model=model_name,
                    max_tokens=4096,
                    temperature=0.1,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": user_prompt}],
                )
                self.model = model_name
                text_blocks = [block.text for block in response.content if hasattr(block, "text")]
                return "\n".join(text_blocks)
            except Exception as exc:
                last_err = exc
                continue

        raise RuntimeError(f"Anthropic API çağrısı başarısız oldu: {last_err}")

    def _call_openai(self, user_prompt: str) -> str:
        """OpenAI API çağrısını gerçekleştirir."""
        from openai import OpenAI

        base_url = os.getenv("OPENAI_BASE_URL")
        client = OpenAI(api_key=self.api_key, base_url=base_url) if base_url else OpenAI(api_key=self.api_key)

        response = client.chat.completions.create(
            model=self.model,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        return response.choices[0].message.content or "{}"

    @staticmethod
    def _extract_json_dict(raw_text: str) -> dict[str, Any]:
        """LLM yanıtından JSON nesnesini güvenli biçimde ayıklar."""
        cleaned = raw_text.strip()
        # Eğer ```json ... ``` bloğu içindeyse ayıkla
        fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, flags=re.DOTALL)
        if fence_match:
            cleaned = fence_match.group(1).strip()
        else:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1 and end > start:
                cleaned = cleaned[start : end + 1]

        return json.loads(cleaned)

    @staticmethod
    def _validate_python_syntax(code: str, filename: str) -> bool:
        """Üretilen Python kodunun AST sözdizimi doğrulamasını yapar."""
        try:
            ast.parse(code, filename=filename)
            return True
        except SyntaxError:
            return False

    @staticmethod
    def _build_fallback_codes(
        endpoint: CapturedEndpoint,
        function_name: str,
        dynamic_params: list[DynamicParamSpec],
        explanation: str,
    ) -> tuple[str, str]:
        """LLM'in ürettiği kodda sözdizimi hatası olursa devreye giren deterministik kod üretici."""
        fn_name = re.sub(r"[^a-zA-Z0-9_]", "_", function_name or "call_shadow_api").lower()
        if not fn_name or fn_name[0].isdigit():
            fn_name = f"api_{fn_name}"

        # Eğer LLM dinamik parametre döndürmediyse, payload veya query içinden otomatik tahmin et
        if not dynamic_params:
            if isinstance(endpoint.request_payload, dict) and "query" in endpoint.request_payload:
                dynamic_params = [
                    DynamicParamSpec(
                        name="query",
                        original_key="query",
                        location="body",
                        param_type="str",
                        default_value=endpoint.request_payload.get("query", ""),
                        description="Arama sorgusu",
                    )
                ]
            elif "query" in endpoint.query_params or "q" in endpoint.query_params:
                k = "query" if "query" in endpoint.query_params else "q"
                dynamic_params = [
                    DynamicParamSpec(
                        name=k,
                        original_key=k,
                        location="query",
                        param_type="str",
                        default_value=endpoint.query_params[k],
                        description="Arama sorgusu",
                    )
                ]

        # Fonksiyon imzası oluştur
        sig_parts: list[str] = []
        for p in dynamic_params:
            py_type = p.param_type if p.param_type in ("str", "int", "float", "bool") else "Any"
            sig_parts.append(f"{p.name}: {py_type} = {repr(p.default_value)}")
        sig_str = ", ".join(sig_parts)

        # Eğer payload JSON ise ve content-type form-urlencoded ise httpx'in json= göndermesi için ayarla
        headers_dict = ShadowAPICompiler.sanitize_headers(
            endpoint.headers,
            target_url=f"https://{endpoint.domain}",
        )
        if headers_dict.get("sec-fetch-dest") == "document":
            headers_dict["sec-fetch-dest"] = "empty"
        if headers_dict.get("sec-fetch-mode") == "navigate":
            headers_dict["sec-fetch-mode"] = "cors"

        cleaned_payload = copy.deepcopy(endpoint.request_payload)
        if isinstance(cleaned_payload, dict):
            for k in list(cleaned_payload.keys()):
                v = cleaned_payload[k]
                if v is None:
                    cleaned_payload.pop(k, None)
                elif isinstance(v, dict) and all(sub_v is None for sub_v in v.values()):
                    cleaned_payload.pop(k, None)
                elif k.lower() in ("operationcode", "personnelflightsearch", "expatpnr", "ffredemption") and v in (False, None, "TK"):
                    cleaned_payload.pop(k, None)

        if isinstance(cleaned_payload, (dict, list)):
            if headers_dict.get("content-type", "").lower() == "application/x-www-form-urlencoded":
                headers_dict.pop("content-type", None)

        body_override_pairs: list[str] = []
        query_override_lines: list[str] = []
        for p in dynamic_params:
            if p.location == "body" and isinstance(cleaned_payload, (dict, list)):
                body_override_pairs.append(f"{repr(p.original_key)}: {p.name}")
            elif p.location == "query":
                query_override_lines.append(f"    params[{repr(p.original_key)}] = {p.name}")

        is_flight_search = isinstance(cleaned_payload, dict) and "originDestinationInformationList" in cleaned_payload
        if is_flight_search:
            body_updates = f"""    is_round_trip = bool(locals().get('return_date') and str(locals().get('return_date')).strip())
    if isinstance(payload, dict) and "originDestinationInformationList" in payload:
        od_list = payload["originDestinationInformationList"]
        if is_round_trip:
            payload["selectedBookerSearch"] = "R"
            if len(od_list) == 1:
                import copy
                outbound = od_list[0]
                inbound = copy.deepcopy(outbound)
                inbound["originAirportCode"] = outbound.get("destinationAirportCode")
                inbound["destinationAirportCode"] = outbound.get("originAirportCode")
                if "departureDate" in inbound and locals().get("return_date"):
                    inbound["departureDate"] = locals().get("return_date")
                payload["originDestinationInformationList"] = [outbound, inbound]
        else:
            payload["selectedBookerSearch"] = "O"
            if len(od_list) > 1:
                payload["originDestinationInformationList"] = [od_list[0]]
    if {repr(bool(body_override_pairs))}:
        payload = update_payload_deep(payload, {{{', '.join(body_override_pairs)}}})"""
        else:
            body_updates = (
                f"    payload = update_payload_deep(payload, {{{', '.join(body_override_pairs)}}})"
                if body_override_pairs
                else "    # Payload parametresi güncellemesi yok"
            )
        query_updates = "\n".join(query_override_lines)

        has_json_body = isinstance(cleaned_payload, (dict, list))
        has_raw_body = cleaned_payload is not None and not has_json_body

        request_call_args = [
            f"method={repr(endpoint.method)}",
            f"url={repr(endpoint.url)}",
            "params=params",
            "headers=headers",
        ]
        if has_json_body:
            request_call_args.append("json=payload")
        elif has_raw_body:
            request_call_args.append("content=payload")

        call_args_str = ", ".join(request_call_args)

        client_code = f'''from __future__ import annotations

import json
import pathlib
import sys
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from healer import SessionHealer, update_payload_deep


def {fn_name}({sig_str}) -> dict[str, Any]:
    """
    {explanation}
    Hedef Endpoint: {endpoint.method} {endpoint.url}
    """
    params: dict[str, Any] = {repr(endpoint.query_params)}
    headers: dict[str, str] = {repr(headers_dict)}
    payload: Any = {repr(cleaned_payload)}
{query_updates if query_updates else "    # Query parametresi güncellemesi yok"}
{body_updates}

    healer = SessionHealer()
    # Dışarıdan doğrudan HTTP atmak yerine tarayıcı oturumu üzerinden
    # tarayıcının kendi JavaScript ortamında isteği çalıştır (WAF / Akamai bypass)
    return healer.execute_in_browser(
        method={repr(endpoint.method)},
        url={repr(endpoint.url)},
        headers=headers,
        params=params,
        json_payload=payload if {repr(has_json_body)} else None,
    )


if __name__ == "__main__":
    start_ns = time.perf_counter()
    data = {fn_name}()
    elapsed_ms = (time.perf_counter() - start_ns) * 1000

    print(f"[Shadow-API] Yanıt Süresi: {{elapsed_ms:.2f}} ms")
    if isinstance(data, dict):
        if "hits" in data and isinstance(data["hits"], list):
            hits = data["hits"]
            print(f"[Shadow-API] Toplam Sonuç (nbHits): {{data.get('nbHits', len(hits))}} | Dönen: {{len(hits)}}")
            for idx, item in enumerate(hits[:5], start=1):
                title = item.get("title") or item.get("story_title") or item.get("name") or str(item)[:80]
                url = item.get("url") or item.get("story_url") or ""
                print(f"  {{idx}}. {{title}} {{f'({{url}})' if url else ''}}")
        elif "pages" in data and isinstance(data["pages"], list):
            pages = data["pages"]
            print(f"[Shadow-API] Bulunan Sayfa Sayısı: {{len(pages)}}")
            for idx, item in enumerate(pages[:5], start=1):
                title = item.get("title") or item.get("key") or str(item)[:80]
                desc = item.get("description") or ""
                key = item.get("key") or ""
                wiki_url = f"https://en.wikipedia.org/wiki/{{key}}" if key else ""
                print(f"  {{idx}}. {{title}} — {{desc}} {{f'({{wiki_url}})' if wiki_url else ''}}")
        else:
            print(f"[Shadow-API] Üst Düzey Anahtarlar: {{list(data.keys())}}")
            preview = json.dumps(data, ensure_ascii=False, indent=2)[:600]
            print(preview)
    else:
        print(str(data)[:600])
'''

        mcp_code = f'''from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from generated_client import {fn_name} as _shadow_client_fn

mcp = FastMCP("Shadow-API MCP Server")


@mcp.tool()
def {fn_name}({sig_str}) -> dict[str, Any]:
    """
    {explanation}
    """
    return _shadow_client_fn({", ".join(f"{p.name}={p.name}" for p in dynamic_params)})


if __name__ == "__main__":
    mcp.run()
'''
        return client_code, mcp_code

    def _save_to_registry(
        self,
        service_name: str,
        target_url: str,
        goal: str,
        function_name: str,
        explanation: str,
        method: str,
        endpoint_url: str,
        dynamic_params: list[DynamicParamSpec],
        client_code: str,
    ) -> tuple[Path, Path]:
        """Derlenen istemciyi ve meta veri şemasını registry/<servis_adi>/ altına kaydeder."""
        service_dir = self.registry_root / service_name
        service_dir.mkdir(parents=True, exist_ok=True)

        reg_client_path = service_dir / "client.py"
        reg_schema_path = service_dir / "schema.json"

        reg_client_path.write_text(client_code + "\n", encoding="utf-8")

        schema_obj = RegistrySchema(
            service_name=service_name,
            target_url=target_url,
            goal=goal,
            function_name=function_name,
            explanation=explanation,
            method=method,
            endpoint_url=endpoint_url,
            dynamic_params=dynamic_params,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        reg_schema_path.write_text(
            schema_obj.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        return reg_client_path, reg_schema_path

    def compile(self, goal: str, service_name: str | None = None) -> CompilationResult:
        """Trafiği analiz eder, LLM ile derler ve hem kök dosyaları hem de registry kaydını yazar."""
        session, deduped_endpoints = self.load_and_prepare_traffic()
        resolved_service = (
            re.sub(r"[^a-z0-9_]+", "_", service_name.lower().strip()).strip("_")
            if service_name
            else derive_service_name(session.target_url)
        )

        console.print(
            Panel.fit(
                f"[bold cyan]Servis Adı:[/bold cyan] [bold yellow]{resolved_service}[/bold yellow]\n"
                f"[bold cyan]Sağlayıcı:[/bold cyan] {self.provider.upper()} ([yellow]{self.model}[/yellow])\n"
                f"[bold cyan]Hedef (Goal):[/bold cyan] {goal}\n"
                f"[bold cyan]Filtreleme & Tekilleştirme:[/bold cyan] {len(session.endpoints)} toplam istek ➔ "
                f"[bold green]{len(deduped_endpoints)} ilgili uç nokta (3. taraf reklam/analitikler elendi)[/bold green]",
                title="🧠 Shadow-API Compiler",
                border_style="magenta",
            )
        )

        user_prompt = self._build_user_prompt(
            goal=goal,
            target_url=session.target_url,
            endpoints=deduped_endpoints,
        )

        with console.status("[bold magenta]LLM uç noktaları analiz ediyor ve SDK/MCP kodlarını derliyor...[/bold magenta]"):
            if self.provider == "anthropic":
                raw_response = self._call_anthropic(user_prompt)
            else:
                raw_response = self._call_openai(user_prompt)

        parsed = self._extract_json_dict(raw_response)

        matched_id = int(parsed.get("matched_endpoint_id", deduped_endpoints[-1].id))
        matched_ep = next((ep for ep in deduped_endpoints if ep.id == matched_id), deduped_endpoints[-1])

        function_name = str(parsed.get("function_name", "call_shadow_api"))
        explanation = str(parsed.get("explanation", "Hedef uç nokta başarıyla eşleştirildi."))
        raw_params = parsed.get("dynamic_params", [])
        dynamic_params = [DynamicParamSpec.model_validate(p) for p in raw_params if isinstance(p, dict)]

        client_code = str(parsed.get("client_code", "")).strip()
        mcp_code = str(parsed.get("mcp_code", "")).strip()

        # Üretilen kodların geçerliliğini, Self-Healing tünelini ve Browser Fingerprint Spoofing entegrasyonunu denetle
        lower_client = client_code.lower()
        if (
            not client_code
            or not mcp_code
            or "SessionHealer" not in client_code
            or "retry_with_fresh_session" not in client_code
            or "user-agent" not in lower_client
            or "referer" not in lower_client
            or not self._validate_python_syntax(client_code, "generated_client.py")
            or not self._validate_python_syntax(mcp_code, "generated_mcp.py")
        ):
            console.print(
                "[yellow]⚠ Deterministik Self-Healing & Taze Oturum Tüneli şablonu ile istemci kodu üretiliyor...[/yellow]"
            )
            client_code, mcp_code = self._build_fallback_codes(
                endpoint=matched_ep,
                function_name=function_name,
                dynamic_params=dynamic_params,
                explanation=explanation,
            )

        if "sys.path.insert" not in client_code and "from healer import" in client_code:
            client_code = client_code.replace(
                "from healer import",
                "import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))\nfrom healer import",
                1,
            )

        # Eşleşen uç noktanın güncel başlık ve tokenlarını session_tokens.json'a yaz
        token_bundle = {
            "target_url": matched_ep.headers.get("referer") or session.target_url,
            "healed_at": datetime.now(timezone.utc).isoformat(),
            "headers": matched_ep.headers,
            "query_params": matched_ep.query_params,
            "body_tokens": {},
            **matched_ep.headers,
        }
        Path("session_tokens.json").write_text(
            json.dumps(token_bundle, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        self.client_output_path.write_text(client_code + "\n", encoding="utf-8")
        self.mcp_output_path.write_text(mcp_code + "\n", encoding="utf-8")

        reg_client_path, reg_schema_path = self._save_to_registry(
            service_name=resolved_service,
            target_url=session.target_url,
            goal=goal,
            function_name=function_name,
            explanation=explanation,
            method=matched_ep.method,
            endpoint_url=matched_ep.url,
            dynamic_params=dynamic_params,
            client_code=client_code,
        )

        return CompilationResult(
            service_name=resolved_service,
            provider=self.provider,
            model=self.model,
            matched_endpoint_id=matched_ep.id,
            matched_method=matched_ep.method,
            matched_url=matched_ep.url,
            function_name=function_name,
            explanation=explanation,
            dynamic_params=dynamic_params,
            client_code=client_code,
            mcp_code=mcp_code,
            client_path=self.client_output_path,
            mcp_path=self.mcp_output_path,
            registry_client_path=reg_client_path,
            registry_schema_path=reg_schema_path,
        )

    def apply_fallback(self, result: CompilationResult) -> CompilationResult:
        """Çalışma zamanı hatası durumunda deterministik şablonla dosyaları yeniden üretir."""
        _, deduped_endpoints = self.load_and_prepare_traffic()
        matched_ep = next(
            (ep for ep in deduped_endpoints if ep.id == result.matched_endpoint_id),
            deduped_endpoints[-1],
        )
        client_code, mcp_code = self._build_fallback_codes(
            endpoint=matched_ep,
            function_name=result.function_name,
            dynamic_params=result.dynamic_params,
            explanation=result.explanation,
        )
        self.client_output_path.write_text(client_code + "\n", encoding="utf-8")
        self.mcp_output_path.write_text(mcp_code + "\n", encoding="utf-8")
        if result.registry_client_path:
            result.registry_client_path.write_text(client_code + "\n", encoding="utf-8")
        result.client_code = client_code
        result.mcp_code = mcp_code
        return result


def ensure_default_registry(registry_root: Path = Path("registry")) -> None:
    """
    Başlangıçta mevcut generated_client.py varsa ve registry/hackernews/ henüz yoksa
    otomatik olarak registry/hackernews/ altına kaydeder.
    """
    hn_dir = registry_root / "hackernews"
    hn_client = hn_dir / "client.py"
    hn_schema = hn_dir / "schema.json"
    gen_client = Path("generated_client.py")

    if gen_client.exists() and (not hn_client.exists() or not hn_schema.exists()):
        hn_dir.mkdir(parents=True, exist_ok=True)
        hn_client.write_text(gen_client.read_text(encoding="utf-8"), encoding="utf-8")
        default_schema = RegistrySchema(
            service_name="hackernews",
            target_url="https://hn.algolia.com",
            goal="Hacker News üzerinde makale araması yap",
            function_name="search_hacker_news_articles",
            explanation="Hacker News (Algolia) arama API uç noktası. Sorgu kelimesine göre makaleleri, puanlarını ve bağlantıları döndürür.",
            method="POST",
            endpoint_url="https://uj5wyc0l7x-dsn.algolia.net/1/indexes/Item_dev/query",
            dynamic_params=[
                DynamicParamSpec(
                    name="query",
                    original_key="query",
                    location="body",
                    param_type="str",
                    default_value="startup",
                    description="Aranacak makale başlığı, anahtar kelime veya konu",
                ),
                DynamicParamSpec(
                    name="page",
                    original_key="page",
                    location="body",
                    param_type="int",
                    default_value=0,
                    description="Getirilecek sayfa numarası (0'dan başlar)",
                ),
                DynamicParamSpec(
                    name="hits_per_page",
                    original_key="hitsPerPage",
                    location="body",
                    param_type="int",
                    default_value=30,
                    description="Sayfa başına döndürülecek maksimum makale sayısı",
                ),
            ],
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        hn_schema.write_text(default_schema.model_dump_json(indent=2) + "\n", encoding="utf-8")


