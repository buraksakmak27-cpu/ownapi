from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from compiler import RegistrySchema
from core.api_keys import (
    PLAN_CATALOG,
    SITE_LIMIT_WARNING_MESSAGE,
    SiteLimitExceededError,
    consume_credit,
    generate_api_key,
    get_or_create_tenant,
    get_tenant_info,
    register_site_usage,
    upgrade_plan,
)
from core.auto_repair import AutoRepairEngine
from core.consent import USER_DELEGATION_DISCLAIMER, TOS_VERSION, ConsentManager
from core.hitl_queue import HITLQueueManager
from core.onboarding import onboard_new_site, validate_and_repair_client
from core.token_store import TokenStore
from own_api.sdk import OwnAPIClient

STATIC_DIR = PROJECT_ROOT / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="OwnAPI Enterprise REST Server & Live Playground",
    description=(
        "Plaid-Style User-Delegated Browser Middleware, 3x Auto-Validated Registry Moat, "
        "Self-Healing 2.0 Nightly Auto-Repair & Human-in-the-Loop (HITL) Queue API."
    ),
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class CallRequest(BaseModel):
    service: str = Field(..., description="Çağrılacak kayıtlı servis adı (örn: trendyol, enuygun, wikipedia, thy)")
    params: dict[str, Any] = Field(default_factory=dict, description="Servise iletilecek dinamik parametreler")
    tenant_id: str = Field(default="default", description="Multi-tenant oturum izolasyonu kimliği")
    api_key: str | None = Field(default=None, description="Opsiyonel sk-ownapi-... API anahtarı")
    mode: str | None = Field(default=None, description="Çalışma modu: 'local' (User Chrome) veya 'cloud' (Docker/CDP)")
    non_interactive: bool = Field(
        default=False,
        description="True ise Captcha/WAF durumunda görevi HITL onay kuyruğuna ekler",
    )


class AskRequest(BaseModel):
    prompt: str = Field(..., description="Doğal dil ajanına sorulacak soru veya talimat")
    service: str | None = Field(default=None, description="Opsiyonel hedef site/konektör adı")
    tenant_id: str = Field(default="default", description="Kiracı ID'si")
    api_key: str | None = Field(default=None, description="Opsiyonel sk-ownapi-... API anahtarı")
    mode: str | None = Field(default=None, description="Çalışma modu: 'local' veya 'cloud'")


class ValidateRequest(BaseModel):
    runs: int = Field(default=3, ge=1, le=10, description="Ardışık doğrulama testi sayısı (varsayılan: 3)")
    tenant_id: str = Field(default="default", description="Kiracı ID'si")
    mode: str = Field(default="local", description="Çalışma modu: 'local' veya 'cloud'")


class AutoRepairRequest(BaseModel):
    runs: int = Field(default=1, ge=1, le=5, description="Her servis için yapılacak sağlık testi sayısı")
    auto_heal: bool = Field(default=True, description="Başarısız servisleri otomatik tamir et")
    services: list[str] | None = Field(default=None, description="Belirli servis alt kümesi (None ise tümü)")
    tenant_id: str = Field(default="default", description="Kiracı ID'si")
    mode: str = Field(default="local", description="Çalışma modu: 'local' veya 'cloud'")


class OnboardRequest(BaseModel):
    service_name: str = Field(..., description="Eklenecek yeni servis adı (örn: biletix, stripe_docs)")
    target_url: str = Field(..., description="Hedef web sitesi URL'si (örn: https://www.biletix.com)")
    description: str = Field(default="", description="Servisin amacı / yapılacak işlem açıklaması")
    consent_accepted: bool = Field(
        default=True,
        description="Plaid tarzı Yasal Onay: Kullanıcının kendi hesabı ve tarayıcısıyla vekaleten eriştiğinin onayı",
    )
    tenant_id: str = Field(default="default", description="Kiracı ID'si")
    mode: str = Field(default="local", description="Çalışma modu: 'local' veya 'cloud'")
    wait_seconds: int = Field(default=20, ge=3, le=120, description="Tarayıcı kayıt süresi (sn)")
    validation_runs: int = Field(default=3, ge=1, le=5, description="Auto-Test doğrulama tekrarı (varsayılan: 3)")


class GenerateKeyRequest(BaseModel):
    tenant_id: str = Field(default="default", description="Anahtar üretilecek kiracı ID'si")
    plan: str | None = Field(default=None, description="Paket tipi (None ise mevcut paketi korunur)")


class BillingUpgradeRequest(BaseModel):
    tenant_id: str = Field(default="default", description="Yükseltilecek kiracı ID'si")
    plan: str = Field(default="starter_cloud", description="Hedef paket: 'starter_cloud' ($10), 'growth_cloud' ($49) veya 'free'")


class HITLResolveRequest(BaseModel):
    otp_code: str | None = Field(default=None, description="Opsiyonel SMS / E-Posta / 2FA doğrulama kodu (örn: 123456)")
    tenant_id: str | None = Field(default=None, description="Opsiyonel kiracı ID'si")


def _mask_api_key(api_key: str) -> str:
    clean = str(api_key or "").strip()
    if len(clean) <= 16:
        return clean
    return f"{clean[:10]}••••••••••••••••{clean[-6:]}"


def _build_account_payload(tenant_id: str = "default") -> dict[str, Any]:
    rec = get_tenant_info(tenant_id)
    plan_key = str(rec.get("plan", "free"))
    plan_meta = PLAN_CATALOG.get(plan_key, PLAN_CATALOG["free"])
    max_credits = int(plan_meta.get("credits", 1000))
    rem_credits = int(rec.get("credits", max_credits))
    plan_used = max(0, max_credits - rem_credits)
    consumed = int(rec.get("total_consumed", plan_used))
    full_key = str(rec.get("api_key", ""))
    max_sites = int(rec.get("max_sites", plan_meta.get("max_sites", 2)))
    active_sites = list(rec.get("active_sites", [])) if isinstance(rec.get("active_sites"), list) else []
    return {
        "success": True,
        "tenant_id": rec.get("tenant_id", tenant_id),
        "plan": plan_key,
        "plan_label": rec.get("plan_label", plan_meta["label"]),
        "mode": rec.get("mode", plan_meta["mode"]),
        "price_usd": int(rec.get("price_usd", plan_meta["price_usd"])),
        "credits": rem_credits,
        "remaining_credits": rem_credits,
        "max_credits": max_credits,
        "total_credits": max_credits,
        "used_credits": plan_used,
        "total_consumed": consumed,
        "max_sites": max_sites,
        "active_sites": active_sites,
        "active_sites_count": len(active_sites),
        "api_key": full_key,
        "masked_api_key": _mask_api_key(full_key),
        "created_at": rec.get("created_at"),
        "updated_at": rec.get("updated_at"),
        "plans": PLAN_CATALOG,
    }


@app.get("/api/v1/account")
async def get_account_endpoint(tenant_id: str = Query(default="default")) -> dict[str, Any]:
    """
    Kullanıcının mevcut paketini (`free`, `starter_cloud`, `growth_cloud`),
    kalan/toplam kredisini, çalışma modunu (`local`/`cloud`) ve maskelenmiş/tam
    `sk-ownapi-...` anahtarını döndürür.
    """
    try:
        return _build_account_payload(tenant_id=tenant_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/v1/keys/generate")
async def generate_key_endpoint(req: GenerateKeyRequest | None = None) -> dict[str, Any]:
    """Kullanıcıya yeni bir `sk-ownapi-...` API anahtarı üretir ve döndürür."""
    cfg = req or GenerateKeyRequest()
    try:
        current = get_tenant_info(cfg.tenant_id)
        target_plan = (cfg.plan or current.get("plan") or "free").strip().lower()
        new_key = generate_api_key(tenant_id=current.get("tenant_id", cfg.tenant_id), plan=target_plan)
        payload = _build_account_payload(tenant_id=new_key.tenant_id)
        payload["message"] = "Yeni API anahtarı başarıyla üretildi."
        return payload
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/v1/billing/upgrade")
async def billing_upgrade_endpoint(req: BillingUpgradeRequest) -> dict[str, Any]:
    """
    Kullanıcının seçtiği pakete (`starter_cloud` $10 / 10.000 kredi veya
    `growth_cloud` $49 / 100.000 kredi) geçişini yapar, yeni `sk-ownapi-...`
    anahtarını ve `mode="cloud"` bilgisini döndürür.
    """
    try:
        new_key = upgrade_plan(tenant_id=req.tenant_id, plan=req.plan)
        payload = _build_account_payload(tenant_id=new_key.tenant_id)
        payload["message"] = "Tebrikler! Artık buluttasın"
        return payload
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard() -> HTMLResponse:
    """OwnAPI Enterprise SaaS Web Dashboard & Live Playground arayüzünü sunar."""
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>OwnAPI Dashboard bulunamadı (static/index.html)</h1>", status_code=404)
    return HTMLResponse(index_file.read_text(encoding="utf-8"))


@app.get("/api/v1/services")
async def list_services(tenant_id: str = Query(default="default")) -> dict[str, Any]:
    """
    Kayıtlı tüm `registry/` servislerini, şemalarını, `validation_status` (3/3 Auto-Test durumu),
    parametrelerini, TokenStore oturum ve ToS onay durumlarını döndürür.
    """
    client = OwnAPIClient(tenant_id=tenant_id)
    token_store = TokenStore()
    consent_mgr = ConsentManager()
    hitl_mgr = HITLQueueManager()

    base_services = client.list_services()
    enriched: list[dict[str, Any]] = []
    validated_count = 0
    active_sessions = 0

    for svc in base_services:
        s_name = svc["service_name"]
        schema_path = PROJECT_ROOT / "registry" / s_name / "schema.json"
        val_status = "BAŞARILI"
        val_runs = 3
        last_val_at = None
        target_url = ""

        if schema_path.exists():
            try:
                raw_schema = json.loads(schema_path.read_text(encoding="utf-8"))
                val_status = raw_schema.get("validation_status", "BAŞARILI")
                val_runs = raw_schema.get("validation_passed_runs", 3)
                last_val_at = raw_schema.get("last_validated_at") or raw_schema.get("created_at")
                target_url = raw_schema.get("target_url", "")
            except Exception:
                pass

        if val_status == "BAŞARILI":
            validated_count += 1

        sess = token_store.get_session(tenant_id, s_name)
        has_sess = bool(sess and (sess.get("cookie") or sess.get("playwright_cookies")))
        if has_sess:
            active_sessions += 1

        has_consent = consent_mgr.has_consented(tenant_id, s_name)

        enriched.append({
            **svc,
            "target_url": target_url,
            "validation_status": val_status,
            "validation_passed_runs": val_runs,
            "last_validated_at": last_val_at,
            "has_active_session": has_sess,
            "session_updated_at": sess.get("updated_at") or sess.get("healed_at") if sess else None,
            "has_tos_consent": has_consent,
        })

    pending_tasks = hitl_mgr.list_tasks(tenant_id=None, status_filter="PENDING_USER_ACTION")

    auto_repair_file = PROJECT_ROOT / ".sessions" / "auto_repair_report.json"
    latest_repair = None
    if auto_repair_file.exists():
        try:
            rep_data = json.loads(auto_repair_file.read_text(encoding="utf-8"))
            latest_repair = rep_data.get("latest_summary")
        except Exception:
            pass

    return {
        "success": True,
        "tenant_id": tenant_id,
        "total_services": len(enriched),
        "validated_count": validated_count,
        "validation_rate": round((validated_count / len(enriched)) * 100, 1) if enriched else 100.0,
        "active_sessions_count": active_sessions,
        "pending_hitl_count": len(pending_tasks),
        "latest_auto_repair": latest_repair,
        "services": enriched,
    }


@app.post("/api/v1/onboard")
async def onboard_site_endpoint(req: OnboardRequest) -> Any:
    """
    Web Dashboard üzerinden yeni bir siteyi sisteme ekler (`onboard_new_site`):
    1. Paket bazlı maksimum site limitini (`max_sites` / `active_sites`) kontrol eder.
    2. Plaid tarzı yasal onay (User Delegation Consent) kontrolünü yapar ve SHA-256 imzasıyla loglar.
    3. Tarayıcı oturumunu açıp XHR/Fetch trafiğini yakalar (`wait_seconds` veya tarayıcı içi 'Kaydı Bitir' butonuyla).
    4. GPT-4o ile `client.py`, `schema.json` ve `__init__.py` dosyalarını derler.
    5. 3x Auto-Test (Compiler + Validator Loop) doğrulamasını çalıştırıp sonucu döndürür.
    """
    if not req.consent_accepted:
        raise HTTPException(
            status_code=400,
            detail="Plaid tarzı Yasal Onay (User Delegation Disclaimer) kutucuğu onaylanmalıdır.",
        )

    svc_name = req.service_name.strip()
    target_url = req.target_url.strip()
    if not svc_name or not target_url:
        raise HTTPException(status_code=400, detail="Site adı (service_name) ve Hedef URL (target_url) zorunludur.")

    if not target_url.startswith(("http://", "https://")):
        target_url = f"https://{target_url}"

    try:
        register_site_usage(req.tenant_id, svc_name)
    except SiteLimitExceededError as exc:
        acc = _build_account_payload(tenant_id=req.tenant_id)
        return JSONResponse(
            status_code=403,
            content={
                "success": False,
                "service_name": svc_name,
                "target_url": target_url,
                "tenant_id": acc["tenant_id"],
                "plan": acc["plan"],
                "max_sites": acc["max_sites"],
                "active_sites": acc["active_sites"],
                "active_sites_count": acc["active_sites_count"],
                "error": str(exc),
                "detail": str(exc),
                "warning": str(exc),
                "message": str(exc),
            },
        )

    start_t = time.perf_counter()
    try:
        result = await asyncio.to_thread(
            onboard_new_site,
            service_name=svc_name,
            start_url=target_url,
            description=req.description.strip(),
            headless=(req.mode.strip().lower() == "cloud"),
            tenant_id=req.tenant_id,
            mode=req.mode,
            auto_validate=True,
            validation_runs=req.validation_runs,
            interactive_tos=False,
            wait_seconds=req.wait_seconds,
        )
        elapsed_ms = round((time.perf_counter() - start_t) * 1000.0, 2)
        acc = _build_account_payload(tenant_id=req.tenant_id)
        return {
            **result,
            "max_sites": acc["max_sites"],
            "active_sites": acc["active_sites"],
            "active_sites_count": acc["active_sites_count"],
            "elapsed_ms": elapsed_ms,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - start_t) * 1000.0, 2)
        return {
            "success": False,
            "service_name": svc_name,
            "target_url": target_url,
            "elapsed_ms": elapsed_ms,
            "error": str(exc),
        }


def _detect_service_from_prompt(prompt: str, explicit_service: str | None = None) -> str | None:
    """Doğal dil sorusu (`prompt`) içinden kullanılan site/konektör adını tespit eder."""
    if explicit_service and explicit_service.strip():
        return explicit_service.strip().lower()
    lower_p = (prompt or "").lower()
    reg_dir = PROJECT_ROOT / "registry"
    candidates: list[str] = []
    if reg_dir.exists():
        for child in sorted(reg_dir.iterdir()):
            if child.is_dir() and not child.name.startswith("_"):
                candidates.append(child.name.lower())
    for extra in ("thy", "trendyol", "pegasus", "enuygun", "wikipedia", "linkedin_jobs", "biletix", "obilet", "hepsiburada", "amazon"):
        if extra not in candidates:
            candidates.append(extra)
    if "türk hava yolları" in lower_p or "turk hava yollari" in lower_p or "turkish airlines" in lower_p:
        return "thy"
    if "linkedin" in lower_p and "linkedin_jobs" in candidates:
        return "linkedin_jobs"
    for svc in candidates:
        if svc in lower_p:
            return svc
    return None


def _consume_and_resolve_account(
    tenant_id: str = "default",
    api_key: str | None = None,
    requested_mode: str | None = None,
    service_name: str | None = None,
) -> dict[str, Any]:
    """
    `X-API-Key` / `api_key` veya `tenant_id` üzerinden kullanıcıyı bulur,
    varsa `service_name` için paket bazlı site limitini (`register_site_usage`) kontrol eder,
    `core/api_keys.py` (`consume_credit`) ile 1 kredi düşer ve aktif modu ('local' / 'cloud')
    ile güncel kredi/paket/site bilgilerini döndürür.
    """
    auth_id = (api_key or tenant_id or "default").strip() or "default"
    info_before = get_tenant_info(auth_id)
    resolved_tenant = str(info_before.get("tenant_id") or tenant_id or "default")
    if service_name:
        register_site_usage(auth_id, service_name)
    consume_credit(auth_id, amount=1, service=service_name)
    acc = _build_account_payload(tenant_id=resolved_tenant)

    plan_mode = str(acc.get("mode", "local")).strip().lower()
    req_mode_clean = str(requested_mode or "").strip().lower()
    if plan_mode == "cloud" or req_mode_clean == "cloud":
        active_mode = "cloud"
    elif req_mode_clean in ("local", "cloud"):
        active_mode = req_mode_clean
    else:
        active_mode = plan_mode if plan_mode in ("local", "cloud") else "local"

    return {
        "tenant_id": resolved_tenant,
        "api_key": acc.get("api_key"),
        "plan": acc.get("plan", "free"),
        "plan_label": acc.get("plan_label"),
        "mode": active_mode,
        "remaining_credits": int(acc.get("remaining_credits", 0)),
        "used_credits": int(acc.get("used_credits", 0)),
        "credits": int(acc.get("credits", 0)),
        "max_credits": int(acc.get("max_credits", 1000)),
        "max_sites": int(acc.get("max_sites", 2)),
        "active_sites": list(acc.get("active_sites", [])),
        "active_sites_count": int(acc.get("active_sites_count", 0)),
    }


@app.post("/api/v1/call")
async def call_service(
    req: CallRequest,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Any:
    """
    İlgili servisi User-Delegated Browser Middleware üzerinden çalıştırıp JSON yanıtını,
    süresini (ms) ve güncel kredi/paket/mod/site limiti durumunu döndürür.
    """
    try:
        credit_ctx = _consume_and_resolve_account(
            tenant_id=req.tenant_id,
            api_key=x_api_key or req.api_key,
            requested_mode=req.mode,
            service_name=req.service,
        )
    except SiteLimitExceededError as exc:
        auth_id = (x_api_key or req.api_key or req.tenant_id or "default").strip() or "default"
        acc = _build_account_payload(tenant_id=str(get_tenant_info(auth_id).get("tenant_id", req.tenant_id)))
        return JSONResponse(
            status_code=403,
            content={
                "success": False,
                "service": req.service,
                "tenant_id": acc["tenant_id"],
                "plan": acc["plan"],
                "mode": acc["mode"],
                "remaining_credits": acc["remaining_credits"],
                "used_credits": acc["used_credits"],
                "max_sites": acc["max_sites"],
                "active_sites": acc["active_sites"],
                "active_sites_count": acc["active_sites_count"],
                "error": str(exc),
                "detail": str(exc),
                "warning": str(exc),
                "message": str(exc),
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=402, detail=str(exc))

    active_tenant = credit_ctx["tenant_id"]
    active_mode = credit_ctx["mode"]

    client = OwnAPIClient(
        api_key=x_api_key or req.api_key,
        tenant_id=active_tenant,
        mode=active_mode,
        non_interactive=req.non_interactive,
    )
    start_t = time.perf_counter()
    try:
        result = await asyncio.to_thread(
            client.call,
            req.service,
            mode=active_mode,
            non_interactive=req.non_interactive,
            _consume_credit=False,
            **req.params,
        )
        elapsed_ms = round((time.perf_counter() - start_t) * 1000.0, 2)
        return {
            "success": True,
            "service": req.service,
            "tenant_id": active_tenant,
            "plan": credit_ctx["plan"],
            "mode": active_mode,
            "remaining_credits": credit_ctx["remaining_credits"],
            "used_credits": credit_ctx["used_credits"],
            "credits": credit_ctx["credits"],
            "max_credits": credit_ctx["max_credits"],
            "max_sites": credit_ctx["max_sites"],
            "active_sites": credit_ctx["active_sites"],
            "active_sites_count": credit_ctx["active_sites_count"],
            "non_interactive": req.non_interactive,
            "elapsed_ms": elapsed_ms,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": result,
        }
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - start_t) * 1000.0, 2)
        return {
            "success": False,
            "service": req.service,
            "tenant_id": active_tenant,
            "plan": credit_ctx["plan"],
            "mode": active_mode,
            "remaining_credits": credit_ctx["remaining_credits"],
            "used_credits": credit_ctx["used_credits"],
            "credits": credit_ctx["credits"],
            "max_credits": credit_ctx["max_credits"],
            "max_sites": credit_ctx["max_sites"],
            "active_sites": credit_ctx["active_sites"],
            "active_sites_count": credit_ctx["active_sites_count"],
            "elapsed_ms": elapsed_ms,
            "error": str(exc),
        }


@app.post("/api/v1/ask")
async def ask_agent(
    req: AskRequest,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Any:
    """
    Doğal dil ajanını (`OwnAPIClient.ask`) çalıştırıp yanıtı ve güncel kredi/paket/mod/site limiti durumunu döndürür.
    """
    detected_svc = _detect_service_from_prompt(req.prompt, req.service)
    try:
        credit_ctx = _consume_and_resolve_account(
            tenant_id=req.tenant_id,
            api_key=x_api_key or req.api_key,
            requested_mode=req.mode,
            service_name=detected_svc,
        )
    except SiteLimitExceededError as exc:
        auth_id = (x_api_key or req.api_key or req.tenant_id or "default").strip() or "default"
        acc = _build_account_payload(tenant_id=str(get_tenant_info(auth_id).get("tenant_id", req.tenant_id)))
        return JSONResponse(
            status_code=403,
            content={
                "success": False,
                "prompt": req.prompt,
                "service": detected_svc,
                "tenant_id": acc["tenant_id"],
                "plan": acc["plan"],
                "mode": acc["mode"],
                "remaining_credits": acc["remaining_credits"],
                "used_credits": acc["used_credits"],
                "max_sites": acc["max_sites"],
                "active_sites": acc["active_sites"],
                "active_sites_count": acc["active_sites_count"],
                "error": str(exc),
                "detail": str(exc),
                "warning": str(exc),
                "message": str(exc),
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=402, detail=str(exc))

    active_tenant = credit_ctx["tenant_id"]
    active_mode = credit_ctx["mode"]

    client = OwnAPIClient(
        api_key=x_api_key or req.api_key,
        tenant_id=active_tenant,
        mode=active_mode,
    )
    start_t = time.perf_counter()
    try:
        answer = await asyncio.to_thread(client.ask, req.prompt, _consume_credit=False)
        elapsed_ms = round((time.perf_counter() - start_t) * 1000.0, 2)
        return {
            "success": True,
            "prompt": req.prompt,
            "service": detected_svc,
            "tenant_id": active_tenant,
            "plan": credit_ctx["plan"],
            "mode": active_mode,
            "remaining_credits": credit_ctx["remaining_credits"],
            "used_credits": credit_ctx["used_credits"],
            "credits": credit_ctx["credits"],
            "max_credits": credit_ctx["max_credits"],
            "max_sites": credit_ctx["max_sites"],
            "active_sites": credit_ctx["active_sites"],
            "active_sites_count": credit_ctx["active_sites_count"],
            "elapsed_ms": elapsed_ms,
            "answer": answer,
        }
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - start_t) * 1000.0, 2)
        return {
            "success": False,
            "prompt": req.prompt,
            "service": detected_svc,
            "tenant_id": active_tenant,
            "plan": credit_ctx["plan"],
            "mode": active_mode,
            "remaining_credits": credit_ctx["remaining_credits"],
            "used_credits": credit_ctx["used_credits"],
            "credits": credit_ctx["credits"],
            "max_credits": credit_ctx["max_credits"],
            "max_sites": credit_ctx["max_sites"],
            "active_sites": credit_ctx["active_sites"],
            "active_sites_count": credit_ctx["active_sites_count"],
            "elapsed_ms": elapsed_ms,
            "error": str(exc),
        }


@app.post("/api/v1/validate/{service_name}")
async def validate_single_service(
    service_name: str,
    req: ValidateRequest | None = None,
) -> dict[str, Any]:
    """Tek bir servis için 3x Auto-Test (Compiler + Validator Loop) doğrulamasını çalıştırır."""
    cfg = req or ValidateRequest()
    service_dir = PROJECT_ROOT / "registry" / service_name
    client_path = service_dir / "client.py"
    schema_path = service_dir / "schema.json"
    traffic_path = service_dir / "captured_traffic.json"

    if not client_path.exists() or not schema_path.exists():
        raise HTTPException(status_code=404, detail=f"Servis '{service_name}' bulunamadı.")

    raw_schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema_obj = RegistrySchema.model_validate(raw_schema)
    endpoints: list[dict[str, Any]] = []
    if traffic_path.exists():
        try:
            tdata = json.loads(traffic_path.read_text(encoding="utf-8"))
            endpoints = tdata.get("endpoints", [])
        except Exception:
            endpoints = []

    matched_ep = next(
        (e for e in endpoints if e.get("url") == schema_obj.endpoint_url),
        endpoints[0] if endpoints else {},
    )

    report = await asyncio.to_thread(
        validate_and_repair_client,
        service_name=service_name,
        target_url=schema_obj.target_url,
        description=schema_obj.goal,
        client_path=client_path,
        schema_obj=schema_obj,
        matched_endpoint=matched_ep,
        all_endpoints=endpoints,
        validation_runs=cfg.runs,
        max_repair_attempts=3,
        tenant_id=cfg.tenant_id,
        mode=cfg.mode,
    )

    raw_schema["validation_status"] = report.get("validation_status", "BAŞARILI")
    raw_schema["validation_passed_runs"] = report.get("passed_runs", cfg.runs)
    raw_schema["last_validated_at"] = datetime.now(timezone.utc).isoformat()
    schema_path.write_text(json.dumps(raw_schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {
        "success": report.get("validation_status") == "BAŞARILI",
        "service_name": service_name,
        **report,
    }


@app.post("/api/v1/auto-repair")
async def run_auto_repair(req: AutoRepairRequest | None = None) -> dict[str, Any]:
    """
    `AutoRepairEngine.validate_all_services(runs=1, auto_heal=True)` tetikleyip
    toplu sağlık ve onarım raporunu (`.sessions/auto_repair_report.json`) döndürür.
    """
    cfg = req or AutoRepairRequest()
    engine = AutoRepairEngine(tenant_id=cfg.tenant_id, mode=cfg.mode)
    report = await asyncio.to_thread(
        engine.validate_all_services,
        runs=cfg.runs,
        auto_heal=cfg.auto_heal,
        services=cfg.services,
    )
    return {
        "success": True,
        **report,
    }


@app.get("/api/v1/auto-repair/report")
async def get_auto_repair_report() -> dict[str, Any]:
    """Kaydedilmiş son `.sessions/auto_repair_report.json` geçmişini döndürür."""
    report_file = PROJECT_ROOT / ".sessions" / "auto_repair_report.json"
    if not report_file.exists():
        return {"success": True, "total_runs": 0, "latest_summary": None, "history": []}
    try:
        data = json.loads(report_file.read_text(encoding="utf-8"))
        return {"success": True, **data}
    except Exception as exc:
        return {"success": False, "error": str(exc), "history": []}


@app.get("/api/hitl/pending")
@app.get("/api/v1/hitl/pending")
async def get_hitl_pending(
    tenant_id: str | None = Query(default=None, description="Kiracı filtresi (boş ise tüm kiracılar)"),
) -> dict[str, Any]:
    """
    Proje kök dizinindeki `.sessions/hitl_state.json` dosyasından bekleyen (`PAUSED` / `PENDING_USER_ACTION`)
    HITL onay isteklerini diskten okuyarak döndürür. Sayfa yenilendiğinde (F5) veya 2 saniyelik
    polling sırasında bekleyen onayların kaybolmadan anında ekrana gelmesini sağlar.
    """
    mgr = HITLQueueManager()
    all_tasks = mgr.list_tasks(tenant_id=tenant_id, status_filter=None)
    pending_tasks = [t for t in all_tasks if mgr.is_pending_status(t.get("status"))]
    completed_tasks = [t for t in all_tasks if str(t.get("status", "")).upper() == "COMPLETED"]
    return {
        "success": True,
        "state_file": ".sessions/hitl_state.json",
        "total_tasks": len(all_tasks),
        "pending_count": len(pending_tasks),
        "paused_count": len(pending_tasks),
        "completed_count": len(completed_tasks),
        "pending_tasks": list(reversed(pending_tasks)),
        "tasks": list(reversed(all_tasks)),
    }


@app.get("/api/hitl/queue")
@app.get("/api/v1/hitl/queue")
async def get_hitl_queue(
    tenant_id: str | None = Query(default=None, description="Kiracı filtresi (boş ise tüm kiracılar)"),
    status: str | None = Query(default=None, description="PAUSED, PENDING_USER_ACTION veya COMPLETED"),
) -> dict[str, Any]:
    """`HITLQueueManager` (`.sessions/hitl_state.json`) üzerinden bekleyen (`PAUSED`) ve tamamlanan görevleri listeler."""
    mgr = HITLQueueManager()
    tasks = mgr.list_tasks(tenant_id=tenant_id, status_filter=status)
    pending_count = sum(1 for t in tasks if mgr.is_pending_status(t.get("status")))
    completed_count = sum(1 for t in tasks if str(t.get("status", "")).upper() == "COMPLETED")
    return {
        "success": True,
        "state_file": ".sessions/hitl_state.json",
        "total_tasks": len(tasks),
        "pending_count": pending_count,
        "paused_count": pending_count,
        "completed_count": completed_count,
        "tasks": list(reversed(tasks)),
    }


@app.get("/api/hitl/preview/{task_id}")
@app.get("/api/v1/hitl/preview/{task_id}")
async def get_hitl_preview(
    task_id: str,
    tenant_id: str | None = Query(default=None, description="İsteğe bağlı kiracı filtresi"),
) -> dict[str, Any]:
    """
    Aktif veya duraklatılmış (`PAUSED`) HITL görevi için bulut tarayıcı ekran görüntüsünü (`screenshot_b64`)
    ve canlı durum önizlemesini döndürür.
    """
    mgr = HITLQueueManager()
    preview = mgr.get_task_preview(task_id=task_id, tenant_id=tenant_id)
    if not preview:
        raise HTTPException(status_code=404, detail=f"HITL görevi '{task_id}' bulunamadı.")
    return preview


@app.post("/api/hitl/resolve/{task_id}")
@app.post("/api/v1/hitl/resolve/{task_id}")
@app.post("/api/hitl/approve/{task_id}")
@app.post("/api/hitl/resume/{task_id}")
@app.post("/api/v1/hitl/approve/{task_id}")
@app.post("/api/v1/hitl/resume/{task_id}")
async def approve_hitl_task(
    task_id: str,
    req: HITLResolveRequest | None = Body(default=None),
    tenant_id: str | None = Query(default=None),
    otp_code: str | None = Query(default=None, description="Opsiyonel SMS / E-Posta / 2FA doğrulama kodu"),
) -> dict[str, Any]:
    """
    `.sessions/hitl_state.json` içindeki bekleyen (`PAUSED`) HITL görevini onaylar,
    varsa `otp_code` (SMS/E-Posta doğrulama kodu) parametresini açık tarayıcıya iletir
    ve duraklatılmış ajanın kaldığı yerden (`resume_task`) devam etmesini sağlar.
    """
    eff_tenant = (req.tenant_id if req and req.tenant_id else tenant_id) or None
    eff_otp = (req.otp_code if req and req.otp_code is not None else otp_code) or None
    if eff_otp is not None:
        eff_otp = str(eff_otp).strip() or None

    mgr = HITLQueueManager()
    task, _ = mgr.get_task(task_id, tenant_id=eff_tenant)
    if not task:
        raise HTTPException(status_code=404, detail=f"HITL görevi '{task_id}' bulunamadı.")
    try:
        res = await asyncio.to_thread(
            mgr.resume_task,
            task_id=task_id,
            tenant_id=eff_tenant,
            otp_code=eff_otp,
        )
        return {"success": True, **res}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/v1/consent")
async def get_consent_audit(tenant_id: str = Query(default="default")) -> dict[str, Any]:
    """`ConsentManager` üzerinden SHA-256 imzalı yasal kullanıcı delegasyon kayıtlarını döndürür."""
    mgr = ConsentManager()
    records = mgr.get_consents(tenant_id=tenant_id)
    return {
        "success": True,
        "tenant_id": tenant_id,
        "tos_version": TOS_VERSION,
        "disclaimer": USER_DELEGATION_DISCLAIMER,
        "total_records": len(records),
        "consents": list(reversed(records)),
    }


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def run_server(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    """FastAPI sunucusunu Uvicorn ile başlatır."""
    import uvicorn

    uvicorn.run("server:app" if reload else app, host=host, port=port, reload=reload)


if __name__ == "__main__":
    run_server()
