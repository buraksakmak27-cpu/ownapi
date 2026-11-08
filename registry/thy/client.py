from __future__ import annotations

import copy
import json
import pathlib
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from healer import SessionHealer, update_payload_deep
from core.token_store import TokenStore
from core.base_client import BaseShadowClient


def _normalize_thy_date(date_str: str) -> str:
    """Tarih dizgesini THY'nin güncel API şemasının beklediği DD-MM-YYYY formatına dönüştürür."""
    clean = str(date_str).strip()
    for sep in ("-", ".", "/"):
        if sep in clean:
            parts = clean.split(sep)
            if len(parts) == 3:
                # YYYY-MM-DD -> DD-MM-YYYY
                if len(parts[0]) == 4:
                    return f"{parts[2].zfill(2)}-{parts[1].zfill(2)}-{parts[0]}"
                # DD-MM-YYYY
                return f"{parts[0].zfill(2)}-{parts[1].zfill(2)}-{parts[2]}"
    return clean


THY_HOME_URL = "https://www.turkishairlines.com/tr-tr/"
THY_AVAILABILITY_URL = "https://www.turkishairlines.com/api/v1/availability"

THY_AIRPORT_META: dict[str, dict[str, Any]] = {
    "IST": {"city_code": "IST", "city": "İstanbul", "country": "TR", "multi_port": True, "domestic": True},
    "SAW": {"city_code": "IST", "city": "İstanbul", "country": "TR", "multi_port": False, "domestic": True},
    "ESB": {"city_code": "ANK", "city": "Ankara", "country": "TR", "multi_port": False, "domestic": True},
    "ADB": {"city_code": "IZM", "city": "İzmir", "country": "TR", "multi_port": False, "domestic": True},
    "AYT": {"city_code": "AYT", "city": "Antalya", "country": "TR", "multi_port": False, "domestic": True},
    "TZX": {"city_code": "TZX", "city": "Trabzon", "country": "TR", "multi_port": False, "domestic": True},
    "ADA": {"city_code": "ADA", "city": "Adana", "country": "TR", "multi_port": False, "domestic": True},
    "DLM": {"city_code": "DLM", "city": "Muğla", "country": "TR", "multi_port": False, "domestic": True},
    "BJV": {"city_code": "BJV", "city": "Muğla", "country": "TR", "multi_port": False, "domestic": True},
    "GZT": {"city_code": "GZT", "city": "Gaziantep", "country": "TR", "multi_port": False, "domestic": True},
    "ASR": {"city_code": "ASR", "city": "Kayseri", "country": "TR", "multi_port": False, "domestic": True},
}


def _airport_info(code: str) -> dict[str, Any]:
    clean = (code or "IST").upper().strip()
    return THY_AIRPORT_META.get(
        clean,
        {"city_code": clean, "city": clean, "country": "TR", "multi_port": clean == "IST", "domestic": True},
    )


def _load_captured_availability_template() -> dict[str, Any]:
    """THY /api/v1/availability POST isteğinin güncel ve doğrulanmış DTO şablonunu döndürür."""
    return {
        "selectedBookerSearch": "R",
        "selectedCabinClass": "ECONOMY",
        "moduleType": "TICKETING",
        "passengerTypeList": [
            {
                "quantity": 1,
                "code": "ADULT",
            }
        ],
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
                "originCity": "İstanbul",
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
                "destinationCity": "İstanbul",
            },
        ],
        "savedDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "preselectedOptionDetails": [],
    }


def build_thy_availability_payload(
    departure_date: str = "02-03-2027",
    return_date: str | None = "09-03-2027",
    origin_airport_code: str = "IST",
    destination_airport_code: str = "ESB",
    cabin_class: str = "ECONOMY",
    adults: int = 1,
) -> dict[str, Any]:
    """
    Parametre eksikliğinden kaynaklı 400/404 hatalarını önlemek için THY'nin güncel
    /api/v1/availability arama şemasına birebir uyumlu JSON gövdesini (body) oluşturur.
    """
    is_round_trip = bool(return_date and str(return_date).strip())
    norm_departure_date = _normalize_thy_date(departure_date)
    orig_code = (origin_airport_code or "IST").upper().strip()
    dest_code = (destination_airport_code or "ESB").upper().strip()
    orig_meta = _airport_info(orig_code)
    dest_meta = _airport_info(dest_code)

    outbound_segment: dict[str, Any] = {
        "originAirportCode": orig_code,
        "originCountryCode": orig_meta["country"],
        "originMultiPort": bool(orig_meta["multi_port"]),
        "originDomestic": bool(orig_meta["domestic"]),
        "destinationAirportCode": dest_code,
        "destinationCountryCode": dest_meta["country"],
        "destinationMultiPort": bool(dest_meta["multi_port"]),
        "destinationDomestic": bool(dest_meta["domestic"]),
        "departureDate": norm_departure_date,
        "originCityCode": orig_meta["city_code"],
        "destinationCityCode": dest_meta["city_code"],
        "originCity": orig_meta["city"],
        "destinationCity": dest_meta["city"],
    }

    segments: list[dict[str, Any]] = [outbound_segment]
    if is_round_trip and return_date:
        inbound_segment: dict[str, Any] = {
            "originAirportCode": dest_code,
            "originCountryCode": dest_meta["country"],
            "originMultiPort": bool(dest_meta["multi_port"]),
            "originDomestic": bool(dest_meta["domestic"]),
            "destinationAirportCode": orig_code,
            "destinationCountryCode": orig_meta["country"],
            "destinationMultiPort": bool(orig_meta["multi_port"]),
            "destinationDomestic": bool(orig_meta["domestic"]),
            "departureDate": _normalize_thy_date(return_date),
            "originCityCode": dest_meta["city_code"],
            "destinationCityCode": orig_meta["city_code"],
            "originCity": dest_meta["city"],
            "destinationCity": orig_meta["city"],
        }
        segments.append(inbound_segment)

    return {
        "selectedBookerSearch": "R" if is_round_trip else "O",
        "selectedCabinClass": (cabin_class or "ECONOMY").upper().strip(),
        "moduleType": "TICKETING",
        "passengerTypeList": [
            {
                "quantity": max(1, int(adults or 1)),
                "code": "ADULT",
            }
        ],
        "originDestinationInformationList": segments,
        "savedDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "preselectedOptionDetails": [],
    }


def _build_validate_payload(p_load: Any) -> dict[str, Any]:
    """/api/v1/availability/validate uç noktası için oturum durumunu başlatan DTO payload'unu üretir."""
    if not isinstance(p_load, dict) or not p_load.get("originDestinationInformationList"):
        p_load = build_thy_availability_payload()
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


def search_flights(
    departure_date: str = '02-03-2027',
    return_date: str | None = '09-03-2027',
    origin_airport_code: str = 'IST',
    destination_airport_code: str = 'ESB',
    cid: str | None = None,
    tenant_id: str = "default",
    mode: str = "local",
    non_interactive: bool | None = None,
) -> dict[str, Any]:
    """
    Türk Hava Yolları (THY) İstanbul - Ankara gidiş-dönüş veya tek yön uçuş seferlerini ve müsaitlik/fiyat bilgilerini
    doğrudan User-Delegated In-Browser Fetch tüneli üzerinden sorgular.
    Önce https://www.turkishairlines.com/tr-tr/ ana sayfasına giderek oturum çerezlerini tazeler,
    ardından Referer ve Origin başlıklarıyla /api/v1/availability POST çağrısını yürütür.
    """
    params: dict[str, Any] = {}
    headers: dict[str, str] = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.turkishairlines.com/",
        "Origin": "https://www.turkishairlines.com",
        "referer": "https://www.turkishairlines.com/",
        "origin": "https://www.turkishairlines.com",
    }

    payload = build_thy_availability_payload(
        departure_date=departure_date,
        return_date=return_date,
        origin_airport_code=origin_airport_code,
        destination_airport_code=destination_airport_code,
    )

    healer = SessionHealer(
        target_url=THY_HOME_URL,
        tenant_id=tenant_id,
        service="thy",
        mode=mode,
        non_interactive=bool(non_interactive) if non_interactive is not None else False,
    )

    # TokenStore (.sessions/{tenant_id}/thy_tokens.json) içindeki gerçek çerezleri ve User-Agent başlığını yükle
    saved_tokens = healer.load_tokens(tenant_id=tenant_id, service="thy")
    real_cookie = saved_tokens.get("cookie") or saved_tokens.get("headers", {}).get("cookie", "")
    real_ua = saved_tokens.get("user-agent") or saved_tokens.get("headers", {}).get("user-agent", "")

    if saved_tokens:
        tok_headers = saved_tokens.get("headers", {})
        if isinstance(tok_headers, dict):
            for k in ("authorization", "x-session-id", "x-session", "tafsessionid", "x-requested-with", "accept-language"):
                if tok_headers.get(k):
                    headers[k] = str(tok_headers[k])

    if real_cookie:
        headers["cookie"] = real_cookie
        headers["Cookie"] = real_cookie
    if real_ua:
        headers["user-agent"] = real_ua

    # Kalıcı profil ve PerimeterX (_px...) çerezlerini istek başlıklarına bağla
    headers, params, payload = healer.apply_to_request(
        headers,
        params,
        payload,
        endpoint_url=THY_AVAILABILITY_URL,
    )

    for k in list(headers.keys()):
        if k.lower().startswith("sec-") or k.lower() == "accept-encoding":
            del headers[k]

    # Dinamik cId parametresini belirle
    if not cid:
        raw_referer = headers.get("referer", "")
        if "cid=" in raw_referer.lower():
            try:
                from urllib.parse import parse_qs, urlparse
                qs = parse_qs(urlparse(raw_referer).query)
                found = qs.get("cId", qs.get("cid", [None]))[0]
                if found:
                    cid = found
            except Exception:
                pass
    if not cid:
        cid = str(uuid.uuid4())

    # Backend DTO JSON şemasını bozmamak için payload gövdesindeki fazla alanları temizle
    ALLOWED_PAYLOAD_KEYS = {
        "selectedBookerSearch",
        "selectedCabinClass",
        "moduleType",
        "passengerTypeList",
        "originDestinationInformationList",
        "savedDate",
        "preselectedOptionDetails",
    }
    for k in list(payload.keys()):
        if k not in ALLOWED_PAYLOAD_KEYS:
            del payload[k]

    # Zorunlu Referer ve Origin başlıklarını ve THY özel başlıklarını sabitle
    route_page = f"/tr-tr/ucak-bileti/rezervasyon/ic-hat-musaitlik/?cId={cid}"
    headers["accept"] = "application/json, text/plain, */*"
    headers["content-type"] = "application/json"
    headers["Referer"] = "https://www.turkishairlines.com/"
    headers["Origin"] = "https://www.turkishairlines.com"
    headers["referer"] = "https://www.turkishairlines.com/"
    headers["origin"] = "https://www.turkishairlines.com"
    headers["x-current-page-url"] = route_page
    headers["x-client-id"] = cid
    headers["x-clientid"] = cid
    headers["x-country"] = "TR"
    headers["x-language"] = "tr"

    # TAF / Session ID başlıkları
    for cookie_part in headers.get("cookie", "").split(";"):
        if "TAFSessionId=" in cookie_part:
            taf_val = cookie_part.split("TAFSessionId=")[1].strip()
            if taf_val:
                headers.setdefault("tafsessionid", taf_val)
                headers.setdefault("x-session-id", taf_val)
            break

    # User-Delegated Browser Middleware:
    # Önce tarayıcıda ana sayfaya (https://www.turkishairlines.com/tr-tr/) gidip çerezleri tazele,
    # ardından fetch tüneli üzerinden /api/v1/availability POST isteğini yürüt.
    raw_response = healer.execute_in_browser(
        method="POST",
        url=THY_AVAILABILITY_URL,
        headers=headers,
        params=params if params else None,
        json_payload=payload,
        target_url=THY_HOME_URL,
        tenant_id=tenant_id,
        mode=mode,
        non_interactive=non_interactive,
    )

    # Hata Detayı: Eğer 400 Bad Request dönerse gönderilen payload ve dönen yanıt detayını yazdır
    if isinstance(raw_response, dict):
        status_code = raw_response.get("status_code")
        if raw_response.get("status") == 400 or status_code == 400:
            print("\n" + "=" * 80)
            print("❌ [Shadow-API] 400 Bad Request Hatası Alındı!")
            print(f"HTTP Status: {status_code}")
            print("Gönderilen Payload (JSON):")
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            print("Sunucu Hata Yanıtı (response.text):")
            print(raw_response.get("text") or raw_response)
            print("=" * 80 + "\n")

        if raw_response.get("success") is True:
            data_obj = raw_response.get("data")
            if isinstance(data_obj, dict):
                print(f"\n[Shadow-API] 🔑 API 'success: True' döndürdü. 'data' anahtarları: {list(data_obj.keys())}")
            elif isinstance(data_obj, list):
                print(f"\n[Shadow-API] 🔑 API 'success: True' döndürdü. 'data' (liste) uzunluğu: {len(data_obj)}")
        raw_response["parsed"] = parse_thy_availability(raw_response)
    return raw_response


_PRICE_KEYS_EXACT = (
    "amount",
    "totalAmount",
    "price",
    "totalPrice",
    "value",
    "fare",
    "totalFare",
    "baseFare",
    "startingPrice",
    "starting_price",
    "amountInPreferredCurrency",
    "fareAmount",
    "cashAmount",
    "netAmount",
    "cheapestPrice",
    "lowestPrice",
    "minPrice",
    "bestFare",
    "economyStartingPrice",
    "businessStartingPrice",
)

_NON_PRICE_KEYS = {
    "decimalplaces",
    "quantity",
    "seatcount",
    "availableseatcount",
    "availableseats",
    "seats",
    "flightpagelimit",
    "minflightcountforsorting",
    "minflightcountfortimerangefiltering",
    "minflightcountforoptionsfiltering",
    "maximumdaysforavailability",
    "datedifference",
    "duration",
    "flightduration",
    "flightnumber",
    "carrierflightnumber",
    "id",
    "index",
    "status",
    "status_code",
    "statuscode",
    "maxage",
    "minage",
    "maxcount",
    "numberofextensions",
    "countdowntime",
    "pagestimeout",
    "prefetcheditemcount",
}

_THY_BRAND_ALIASES: dict[str, str] = {
    "ef": "EcoFly",
    "ec": "EcoFly",
    "eco": "EcoFly",
    "ecofly": "EcoFly",
    "eco_fly": "EcoFly",
    "eco fly": "EcoFly",
    "brand.ecofly": "EcoFly",
    "ecojet": "EcoFly",
    "xf": "ExtraFly",
    "ex": "ExtraFly",
    "fl": "ExtraFly",
    "ef2": "ExtraFly",
    "extrafly": "ExtraFly",
    "extra_fly": "ExtraFly",
    "extra fly": "ExtraFly",
    "brand.extrafly": "ExtraFly",
    "flexfly": "ExtraFly",
    "flex_fly": "ExtraFly",
    "flex fly": "ExtraFly",
    "brand.flexfly": "ExtraFly",
    "extra": "ExtraFly",
    "pf": "PrimeFly",
    "pr": "PrimeFly",
    "primefly": "PrimeFly",
    "prime_fly": "PrimeFly",
    "prime fly": "PrimeFly",
    "brand.primefly": "PrimeFly",
    "prime": "PrimeFly",
    "bu": "Business",
    "bc": "Business",
    "bl": "Business",
    "bus": "Business",
    "business": "Business",
    "businessfly": "Business",
    "business_fly": "Business",
    "business fly": "Business",
    "businessprime": "Business",
    "business_prime": "Business",
    "brand.business": "Business",
}


def _normalize_thy_package_name(
    raw_name: Any,
    cabin_class: str = "",
    idx_in_cabin: int | None = None,
) -> str:
    """
    THY marka/paket kodlarını ve isimlerini ('EF', 'XF', 'FL', 'PF', 'BU', 'brand.ecoFly' vb.)
    standart 'EcoFly', 'ExtraFly', 'PrimeFly' veya 'Business' paket adlarına dönüştürür.
    """
    if isinstance(raw_name, list) and raw_name:
        raw_name = raw_name[0]
    clean = str(raw_name or "").strip()
    low = clean.lower().replace("-", "_").strip()
    if low.startswith("brand."):
        low_no_prefix = low[6:].strip()
    else:
        low_no_prefix = low

    if low in _THY_BRAND_ALIASES:
        return _THY_BRAND_ALIASES[low]
    if low_no_prefix in _THY_BRAND_ALIASES:
        return _THY_BRAND_ALIASES[low_no_prefix]

    for key_sub, canonical in (
        ("ecofly", "EcoFly"),
        ("eco_fly", "EcoFly"),
        ("extrafly", "ExtraFly"),
        ("extra_fly", "ExtraFly"),
        ("flexfly", "ExtraFly"),
        ("primefly", "PrimeFly"),
        ("prime_fly", "PrimeFly"),
        ("business", "Business"),
    ):
        if key_sub in low:
            return canonical

    cabin_up = str(cabin_class or "").upper().strip()
    if cabin_up == "BUSINESS" and (not clean or low in ("business", "standart", "standard", "default")):
        return "Business"

    if cabin_up in ("ECONOMY", "") and (not clean or low in ("economy", "standart", "standard", "default")):
        if idx_in_cabin == 0:
            return "EcoFly"
        if idx_in_cabin == 1:
            return "ExtraFly"
        if idx_in_cabin == 2:
            return "PrimeFly"
        if cabin_up == "ECONOMY" and idx_in_cabin is None:
            return "EcoFly"

    return clean or "EcoFly"


def _parse_numeric_price_str(val_str: str) -> tuple[float | None, str]:
    """Metin formatındaki fiyat dizgesinden ('1.671,71 TRY', '₺1671.71' vb.) tutar ve para birimini ayıklar."""
    import re

    s = str(val_str or "").strip()
    if not s:
        return None, ""
    curr = ""
    s_up = s.upper()
    for code, norm_c in (("TRY", "TRY"), ("TL", "TRY"), ("₺", "TRY"), ("USD", "USD"), ("$", "USD"), ("EUR", "EUR"), ("€", "EUR"), ("GBP", "GBP")):
        if code in s_up or code in s:
            curr = norm_c
            break

    m = re.search(r"[-+]?\d[\d.,]*", s)
    if not m:
        return None, curr
    num_tok = m.group(0)
    try:
        if "," in num_tok and "." in num_tok:
            if num_tok.rfind(",") > num_tok.rfind("."):
                num_tok = num_tok.replace(".", "").replace(",", ".")
            else:
                num_tok = num_tok.replace(",", "")
        elif "," in num_tok:
            parts = num_tok.split(",")
            if len(parts[-1]) == 3 and len(parts) > 1 and all(p.isdigit() for p in parts):
                num_tok = "".join(parts)
            else:
                num_tok = num_tok.replace(",", ".")
        amt = float(num_tok)
        return (amt if amt > 0 else None), curr
    except ValueError:
        return None, curr


def _extract_amount_and_currency(obj: Any, _depth: int = 0) -> tuple[float | None, str]:
    """Herhangi bir fiyat/tutar nesnesinden sayısal tutar ve para birimini derinlemesine ayıklar."""
    if obj is None or _depth > 6:
        return None, ""
    if isinstance(obj, bool):
        return None, ""
    if isinstance(obj, (int, float)):
        val = float(obj)
        return (val if val > 0 else None), "TRY"
    if isinstance(obj, str):
        amt, curr = _parse_numeric_price_str(obj)
        return amt, (curr or "TRY") if amt is not None else ""
    if isinstance(obj, list):
        best_amt: float | None = None
        best_curr = ""
        for item in obj:
            sub_amt, sub_curr = _extract_amount_and_currency(item, _depth + 1)
            if sub_amt is not None and sub_amt > 0:
                if best_amt is None or sub_amt < best_amt:
                    best_amt = sub_amt
                    best_curr = sub_curr or best_curr
        return best_amt, str(best_curr or "TRY") if best_amt is not None else ""
    if isinstance(obj, dict):
        curr_raw = (
            obj.get("currency")
            or obj.get("currencyCode")
            or obj.get("currency_code")
            or obj.get("currencySign")
            or ""
        )
        curr = "TRY" if str(curr_raw).strip() == "₺" else str(curr_raw or "").strip()

        amt: float | None = None
        for k in _PRICE_KEYS_EXACT:
            if k not in obj:
                continue
            v = obj.get(k)
            if v is None or isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                if float(v) > 0:
                    amt = float(v)
                    break
            elif isinstance(v, str):
                p_amt, p_curr = _parse_numeric_price_str(v)
                if p_amt is not None and p_amt > 0:
                    amt = p_amt
                    curr = curr or p_curr
                    break
            elif isinstance(v, (dict, list)):
                sub_amt, sub_curr = _extract_amount_and_currency(v, _depth + 1)
                if sub_amt is not None and sub_amt > 0:
                    amt = sub_amt
                    curr = curr or sub_curr
                    break

        if amt is None:
            for k, v in obj.items():
                k_low = str(k).lower().strip()
                if k_low in _NON_PRICE_KEYS:
                    continue
                if any(pk in k_low for pk in ("price", "amount", "fare", "cost", "total", "tutar", "fiyat", "ecofly", "extrafly", "primefly")):
                    sub_amt, sub_curr = _extract_amount_and_currency(v, _depth + 1)
                    if sub_amt is not None and sub_amt > 0:
                        if amt is None or sub_amt < amt:
                            amt = sub_amt
                            curr = curr or sub_curr

        return amt, str(curr or "TRY") if amt is not None else ""
    return None, ""


def _unwrap_thy_root(data: Any) -> Any:
    """
    Gelen yanıt sözlük, liste veya JSON metni (hatta kesilmiş JSON metni) olsa dahi
    içindeki esas veri kökünü (`root`) güvenli şekilde çözümler.
    """
    import re

    if isinstance(data, str):
        raw_str = data.strip()
        if not raw_str:
            return {}
        try:
            return _unwrap_thy_root(json.loads(raw_str))
        except Exception:
            # Kesilmiş (truncated) JSON dizgesinden anlamlı alanları kurtar
            recovered: dict[str, Any] = {"_raw_truncated_text": raw_str}
            for key_name in ("economyStartingPrice", "businessStartingPrice", "bestFare"):
                m = re.search(rf'"{key_name}"\s*:\s*(\{{[^{{}}]*\{{[^{{}}]*\}}[^{{}}]*\}}|\{{[^{{}}]*\}})', raw_str)
                if m:
                    try:
                        recovered[key_name] = json.loads(m.group(1))
                    except Exception:
                        pass
            return recovered

    if isinstance(data, dict):
        for wrapper_key in ("data", "result", "response", "payload", "body"):
            inner = data.get(wrapper_key)
            if isinstance(inner, (dict, list)) and inner:
                return inner
            if isinstance(inner, str) and inner.strip().startswith(("{", "[")):
                parsed_inner = _unwrap_thy_root(inner)
                if parsed_inner:
                    return parsed_inner

        for text_key in ("text", "response_sample", "raw_text"):
            raw_txt = data.get(text_key)
            if isinstance(raw_txt, str) and raw_txt.strip().startswith(("{", "[")):
                parsed_txt = _unwrap_thy_root(raw_txt)
                if isinstance(parsed_txt, (dict, list)) and parsed_txt:
                    return parsed_txt

    return data


def _extract_packages_deep(f_opt: Any, default_cabin: str = "ECONOMY") -> list[dict[str, Any]]:
    """
    Bir uçuş seçeneği (veya alt JSON ağacı) içindeki tüm paketleri (EcoFly, ExtraFly, PrimeFly, Business)
    standart anahtarlara bağlı kalmaksızın derinlemesine (deep search) tarayarak ayıklar.
    """
    fares_out: list[dict[str, Any]] = []
    seen_pkg_keys: set[tuple[str, float]] = set()

    def _add_fare(
        raw_pkg_name: Any,
        cabin_name: str,
        amt: float | None,
        curr: str,
        seats: Any = None,
        idx_in_cabin: int | None = None,
    ) -> None:
        if amt is None or amt <= 0:
            return
        norm_name = _normalize_thy_package_name(raw_pkg_name, cabin_class=cabin_name, idx_in_cabin=idx_in_cabin)
        norm_cabin = str(cabin_name or ("BUSINESS" if norm_name == "Business" else default_cabin)).upper().strip() or "ECONOMY"
        dedup_key = (norm_name, round(float(amt), 2))
        if dedup_key in seen_pkg_keys:
            return
        seen_pkg_keys.add(dedup_key)
        fares_out.append({
            "package_name": norm_name,
            "cabin_class": norm_cabin,
            "amount": round(float(amt), 2),
            "currency": str(curr or "TRY"),
            "available_seats": seats,
        })

    if not isinstance(f_opt, (dict, list)):
        return fares_out

    # 1. Standart fareCategory yapısı
    if isinstance(f_opt, dict):
        fare_category = f_opt.get("fareCategory") or f_opt.get("fareCategories") or f_opt.get("cabins")
        if isinstance(fare_category, dict):
            for cabin_name, cabin_obj in fare_category.items():
                if isinstance(cabin_obj, dict):
                    b_list = (
                        cabin_obj.get("bookingPriceInfoList")
                        or cabin_obj.get("brandList")
                        or cabin_obj.get("packages")
                        or cabin_obj.get("fares")
                        or cabin_obj.get("subGroupList")
                        or []
                    )
                    if isinstance(b_list, list) and b_list:
                        for b_idx, b_info in enumerate(b_list):
                            if not isinstance(b_info, dict):
                                continue
                            raw_brand = (
                                b_info.get("brandName")
                                or b_info.get("brandKey")
                                or b_info.get("brandCode")
                                or b_info.get("brandCodeList")
                                or b_info.get("fareFamilyCode")
                                or b_info.get("subGroupName")
                                or b_info.get("name")
                                or b_info.get("code")
                            )
                            pkg_amt, pkg_curr = _extract_amount_and_currency(
                                b_info.get("startingPrice")
                                or b_info.get("price")
                                or b_info.get("totalPrice")
                                or b_info.get("fare")
                                or b_info
                            )
                            seat_count = (
                                b_info.get("availableSeatCount")
                                or b_info.get("seatCount")
                                or b_info.get("availableSeats")
                                or b_info.get("seats")
                            )
                            _add_fare(raw_brand, str(cabin_name), pkg_amt, pkg_curr, seat_count, idx_in_cabin=b_idx)
                    else:
                        c_amt, c_curr = _extract_amount_and_currency(cabin_obj)
                        _add_fare(cabin_name, str(cabin_name), c_amt, c_curr, idx_in_cabin=0)

    # 2. Derinlemesine (Deep Search) paket/marka taraması
    def _scan_packages(node: Any, current_cabin: str = default_cabin) -> None:
        if isinstance(node, dict):
            node_cabin = str(
                node.get("cabinClass")
                or node.get("cabin")
                or node.get("cabinType")
                or current_cabin
            ).upper().strip() or default_cabin

            # Sözlük anahtarları doğrudan EcoFly / ExtraFly / PrimeFly / Business veya EF / XF / PF ise
            for k, v in node.items():
                k_str = str(k).strip()
                k_low = k_str.lower().replace("-", "_").replace(" ", "_")
                if k_low in (
                    "ecofly", "eco_fly", "extrafly", "extra_fly", "flexfly", "flex_fly",
                    "primefly", "prime_fly", "business", "businessfly",
                    "ecofly_price", "extrafly_price", "primefly_price", "business_price",
                ) or k_str in ("EF", "XF", "FL", "PF", "BU"):
                    p_amt, p_curr = _extract_amount_and_currency(v)
                    seats = v.get("availableSeatCount") or v.get("seatCount") or v.get("seats") if isinstance(v, dict) else None
                    _add_fare(k_str, node_cabin, p_amt, p_curr, seats)

            # Eğer düğümün kendisi bir brand / paket nesnesiyse
            has_brand_field = any(
                bk in node
                for bk in (
                    "brandCode", "brandName", "brandKey", "brandCodeList",
                    "subGroupName", "fareFamilyCode", "packageName", "package_name",
                    "brand", "tier", "tariffName",
                )
            )
            if has_brand_field:
                raw_b = (
                    node.get("brandName")
                    or node.get("brandKey")
                    or node.get("brandCode")
                    or node.get("brandCodeList")
                    or node.get("subGroupName")
                    or node.get("fareFamilyCode")
                    or node.get("packageName")
                    or node.get("package_name")
                    or node.get("brand")
                    or node.get("tier")
                    or node.get("tariffName")
                )
                p_amt, p_curr = _extract_amount_and_currency(
                    node.get("startingPrice")
                    or node.get("price")
                    or node.get("totalPrice")
                    or node.get("fare")
                    or node
                )
                seats = (
                    node.get("availableSeatCount")
                    or node.get("seatCount")
                    or node.get("availableSeats")
                    or node.get("seats")
                )
                _add_fare(raw_b, node_cabin, p_amt, p_curr, seats)

            # Alt liste anahtarlarında sıralı paket listesi varsa
            for list_key in (
                "bookingPriceInfoList", "fareSubGroupList", "subGroupList",
                "fares", "fareList", "brandList", "brands", "packages",
                "packageList", "tariffs", "bundles", "priceOptions",
            ):
                sub_list = node.get(list_key)
                if isinstance(sub_list, list):
                    for idx_item, sub_item in enumerate(sub_list):
                        if isinstance(sub_item, dict):
                            raw_b = (
                                sub_item.get("brandName")
                                or sub_item.get("brandKey")
                                or sub_item.get("brandCode")
                                or sub_item.get("brandCodeList")
                                or sub_item.get("subGroupName")
                                or sub_item.get("fareFamilyCode")
                                or sub_item.get("packageName")
                                or sub_item.get("package_name")
                                or sub_item.get("name")
                                or sub_item.get("code")
                            )
                            item_cabin = str(sub_item.get("cabinClass") or sub_item.get("cabin") or node_cabin)
                            p_amt, p_curr = _extract_amount_and_currency(
                                sub_item.get("startingPrice")
                                or sub_item.get("price")
                                or sub_item.get("totalPrice")
                                or sub_item.get("fare")
                                or sub_item
                            )
                            seats = (
                                sub_item.get("availableSeatCount")
                                or sub_item.get("seatCount")
                                or sub_item.get("availableSeats")
                                or sub_item.get("seats")
                            )
                            _add_fare(raw_b, item_cabin, p_amt, p_curr, seats, idx_in_cabin=idx_item)

            for k, v in node.items():
                if k in ("flightSegmentList", "segments"):
                    continue
                next_cabin = k if str(k).upper() in ("ECONOMY", "BUSINESS") else node_cabin
                _scan_packages(v, next_cabin)
        elif isinstance(node, list):
            for elem in node:
                _scan_packages(elem, current_cabin)

    _scan_packages(f_opt, default_cabin)

    # Eğer hiç paket bulunamadıysa ve f_opt üzerinde doğrudan bir fiyat varsa
    if not fares_out and isinstance(f_opt, dict):
        opt_amt, opt_curr = _extract_amount_and_currency(
            f_opt.get("price")
            or f_opt.get("cheapestPrice")
            or f_opt.get("totalPrice")
            or f_opt.get("startingPrice")
            or f_opt.get("lowestPrice")
            or f_opt.get("fare")
        )
        if opt_amt is not None and opt_amt > 0:
            _add_fare("EcoFly", default_cabin, opt_amt, opt_curr, idx_in_cabin=0)

    # Paketleri EcoFly -> ExtraFly -> PrimeFly -> Business sırasıyla ve fiyata göre diz
    pkg_order = {"EcoFly": 0, "ExtraFly": 1, "PrimeFly": 2, "Business": 3}
    fares_out.sort(key=lambda x: (pkg_order.get(x["package_name"], 99), x.get("amount") or 0.0))
    return fares_out


def _looks_like_flight_segment_dict(d: dict[str, Any]) -> bool:
    """Bir sözlüğün uçuş segmenti veya uçuş seçeneği olup olmadığını yapısal olarak tespit eder."""
    if not isinstance(d, dict):
        return False
    if any(k in d for k in ("flightSegmentList", "fareCategory", "bookingPriceInfoList", "fareSubGroupList")):
        return True
    has_flight_id = any(k in d for k in ("flightNumber", "carrierFlightNumber", "flightNo", "flight_number", "flightCode", "marketingFlightNumber"))
    has_time = any(k in d for k in ("departureDateTime", "departureTime", "arrivalDateTime", "arrivalTime", "departure", "arrival"))
    has_route = any(k in d for k in ("originAirportCode", "departureAirportCode", "destinationAirportCode", "arrivalAirportCode"))
    has_price_or_pkg = any(k in d for k in ("packages", "fares", "brands", "price", "startingPrice", "EcoFly", "ecofly", "EF"))
    return bool((has_flight_id and (has_time or has_route or has_price_or_pkg)) or (has_time and has_price_or_pkg))


def _parse_single_flight_option(
    f_opt: dict[str, Any],
    fallback_orig: str = "",
    fallback_dest: str = "",
    fallback_packages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Tek bir uçuş seçeneği/segment düğümünden uçuş numarası, saatler, paketler ve en düşük fiyatı çıkarır."""
    seg_list = (
        f_opt.get("flightSegmentList")
        or f_opt.get("segments")
        or f_opt.get("legs")
        or f_opt.get("flightLegs")
        or []
    )
    flight_no = ""
    dep_time = ""
    arr_time = ""
    dur_str = ""
    orig_code = fallback_orig
    dest_code = fallback_dest
    is_direct = True

    if isinstance(seg_list, list) and seg_list and isinstance(seg_list[0], dict):
        is_direct = len(seg_list) == 1
        first_seg = seg_list[0]
        last_seg = seg_list[-1] if isinstance(seg_list[-1], dict) else first_seg

        fn = (
            first_seg.get("flightNumber")
            or first_seg.get("carrierFlightNumber")
            or first_seg.get("marketingFlightNumber")
            or first_seg.get("flightNo")
            or first_seg.get("flight_number")
            or first_seg.get("flightCode")
            or ""
        )
        op_air = first_seg.get("operatingAirline") or first_seg.get("marketingAirline") or {}
        carrier = (
            (op_air.get("code") if isinstance(op_air, dict) else None)
            or first_seg.get("carrierCode")
            or first_seg.get("airlineCode")
            or "TK"
        )
        fn_str = str(fn).strip()
        if fn_str:
            flight_no = fn_str if fn_str.upper().startswith(str(carrier).upper()) else f"{carrier} {fn_str}".strip()

        dep_time = str(
            first_seg.get("departureDateTime")
            or first_seg.get("departureTime")
            or first_seg.get("departureDate")
            or first_seg.get("departure")
            or ""
        )
        arr_time = str(
            last_seg.get("arrivalDateTime")
            or last_seg.get("arrivalTime")
            or last_seg.get("arrivalDate")
            or last_seg.get("arrival")
            or ""
        )
        dur_str = str(
            f_opt.get("duration")
            or first_seg.get("duration")
            or first_seg.get("flightDuration")
            or first_seg.get("journeyDuration")
            or ""
        )
        orig_code = str(
            first_seg.get("originAirportCode")
            or first_seg.get("departureAirportCode")
            or (first_seg.get("departureAirport", {}) or {}).get("code")
            or first_seg.get("origin")
            or orig_code
        )
        dest_code = str(
            last_seg.get("destinationAirportCode")
            or last_seg.get("arrivalAirportCode")
            or (last_seg.get("arrivalAirport", {}) or {}).get("code")
            or last_seg.get("destination")
            or dest_code
        )
    else:
        fn = (
            f_opt.get("flightNumber")
            or f_opt.get("carrierFlightNumber")
            or f_opt.get("marketingFlightNumber")
            or f_opt.get("flightNo")
            or f_opt.get("flight_number")
            or f_opt.get("flightCode")
            or ""
        )
        op_air = f_opt.get("operatingAirline") or f_opt.get("marketingAirline") or {}
        carrier = (
            (op_air.get("code") if isinstance(op_air, dict) else None)
            or f_opt.get("carrierCode")
            or f_opt.get("airlineCode")
            or "TK"
        )
        fn_str = str(fn).strip()
        if fn_str:
            flight_no = fn_str if fn_str.upper().startswith(str(carrier).upper()) else f"{carrier} {fn_str}".strip()

        dep_time = str(
            f_opt.get("departureDateTime")
            or f_opt.get("departureTime")
            or f_opt.get("departureDate")
            or f_opt.get("departure")
            or ""
        )
        arr_time = str(
            f_opt.get("arrivalDateTime")
            or f_opt.get("arrivalTime")
            or f_opt.get("arrivalDate")
            or f_opt.get("arrival")
            or ""
        )
        dur_str = str(f_opt.get("duration") or f_opt.get("flightDuration") or "")
        orig_code = str(
            f_opt.get("originAirportCode")
            or f_opt.get("departureAirportCode")
            or f_opt.get("origin")
            or orig_code
        )
        dest_code = str(
            f_opt.get("destinationAirportCode")
            or f_opt.get("arrivalAirportCode")
            or f_opt.get("destination")
            or dest_code
        )

    fares_out = _extract_packages_deep(f_opt)
    if not fares_out and fallback_packages:
        fares_out = [dict(p) for p in fallback_packages]

    packages_by_name: dict[str, float] = {}
    for p in fares_out:
        pname = p.get("package_name")
        pamt = p.get("amount")
        if pname and pamt is not None:
            if pname not in packages_by_name or pamt < packages_by_name[pname]:
                packages_by_name[pname] = pamt

    flight_amts = [p["amount"] for p in fares_out if p.get("amount") is not None and p["amount"] > 0]
    flight_min_price = min(flight_amts) if flight_amts else None
    flight_curr = fares_out[0]["currency"] if fares_out else "TRY"

    return {
        "flight_number": flight_no or "TK Uçuşu",
        "origin": orig_code,
        "destination": dest_code,
        "departure": dep_time,
        "arrival": arr_time,
        "duration": dur_str,
        "is_direct": is_direct,
        "min_price": flight_min_price,
        "currency": flight_curr,
        "packages": fares_out,
        "packages_by_name": packages_by_name,
    }


def parse_thy_availability(data: dict[str, Any] | Any) -> dict[str, Any]:
    """
    THY /api/v1/availability yanıtını derinlemesine (deep search) analiz eder.
    Standart anahtarlar ('originDestinationInformationList', 'originDestinationOptionList' vb.)
    bulunamasa veya farklı bir derinlikte/isimlendirmede gelse bile tüm JSON ağacını tarayarak:
    - Uçuş segmentlerini ve seferlerini,
    - EcoFly, ExtraFly, PrimeFly ve Business paket fiyatlarını,
    - Tüm keşfedilen fiyatları ('discovered_prices') ve en düşük fiyatı ('min_price' / 'lowest_price')
    kesin olarak yakalayıp döndürür.
    """
    if not isinstance(data, (dict, list, str)):
        return {
            "success": False,
            "error": "Geçersiz yanıt formatı",
            "min_price": None,
            "lowest_price": None,
            "currency": "TRY",
            "packages": {},
            "segments": [],
            "flights": [],
            "discovered_prices": [],
        }

    root = _unwrap_thy_root(data)

    # 1. Tüm JSON ağacında derinlemesine fiyat ve paket taraması (Deep Search)
    all_discovered_prices: list[dict[str, Any]] = []
    seen_price_paths: set[tuple[str, float]] = set()
    global_packages_candidates: list[dict[str, Any]] = _extract_packages_deep(root)

    def _deep_search_prices(node: Any, path: str = "", parent_brand: str = "") -> None:
        if isinstance(node, dict):
            node_brand_raw = (
                node.get("brandName")
                or node.get("brandKey")
                or node.get("brandCode")
                or node.get("brandCodeList")
                or node.get("subGroupName")
                or node.get("fareFamilyCode")
                or node.get("packageName")
                or parent_brand
            )
            # Doğrudan fiyat taşıyan nesne mi?
            if any(k in node for k in ("amount", "totalAmount", "price", "totalPrice", "fare", "startingPrice", "baseFare", "totalFare", "fareAmount", "cashAmount", "lowestPrice", "minPrice")):
                a, c = _extract_amount_and_currency(node)
                if a is not None and a > 0:
                    key_sig = (path, round(float(a), 2))
                    if key_sig not in seen_price_paths:
                        seen_price_paths.add(key_sig)
                        pkg_tag = _normalize_thy_package_name(node_brand_raw) if node_brand_raw else None
                        all_discovered_prices.append({
                            "path": path or "root",
                            "amount": round(float(a), 2),
                            "currency": c or "TRY",
                            "package_name": pkg_tag,
                        })

            for k, v in node.items():
                k_str = str(k)
                k_low = k_str.lower().strip()
                if k_low in _NON_PRICE_KEYS:
                    continue
                child_path = f"{path}.{k_str}" if path else k_str
                inferred_brand = k_str if k_low in ("ecofly", "eco_fly", "extrafly", "extra_fly", "flexfly", "primefly", "prime_fly", "business", "ef", "xf", "fl", "pf", "bu") else (str(node_brand_raw) if node_brand_raw else "")

                if isinstance(v, (int, float)) and not isinstance(v, bool) and float(v) > 0:
                    if any(pk in k_low for pk in ("price", "amount", "fare", "cost", "total", "tutar", "fiyat", "ecofly", "extrafly", "primefly", "business")) or k_str in ("EF", "XF", "FL", "PF", "BU"):
                        key_sig = (child_path, round(float(v), 2))
                        if key_sig not in seen_price_paths:
                            seen_price_paths.add(key_sig)
                            pkg_tag = _normalize_thy_package_name(inferred_brand) if inferred_brand else None
                            all_discovered_prices.append({
                                "path": child_path,
                                "amount": round(float(v), 2),
                                "currency": "TRY",
                                "package_name": pkg_tag,
                            })
                elif isinstance(v, str):
                    if any(pk in k_low for pk in ("price", "amount", "fare", "cost", "total", "tutar", "fiyat", "ecofly", "extrafly", "primefly", "business")) or k_str in ("EF", "XF", "FL", "PF", "BU"):
                        s_amt, s_curr = _parse_numeric_price_str(v)
                        if s_amt is not None and s_amt > 0:
                            key_sig = (child_path, round(float(s_amt), 2))
                            if key_sig not in seen_price_paths:
                                seen_price_paths.add(key_sig)
                                pkg_tag = _normalize_thy_package_name(inferred_brand) if inferred_brand else None
                                all_discovered_prices.append({
                                    "path": child_path,
                                    "amount": round(float(s_amt), 2),
                                    "currency": s_curr or "TRY",
                                    "package_name": pkg_tag,
                                })
                elif isinstance(v, (dict, list)):
                    _deep_search_prices(v, child_path, inferred_brand)
        elif isinstance(node, list):
            for i, elem in enumerate(node):
                _deep_search_prices(elem, f"{path}[{i}]", parent_brand)

    _deep_search_prices(root)
    all_discovered_prices.sort(key=lambda x: x["amount"])

    # 2. Rota / Segment listesini bul (önce standart anahtarlar, yoksa derin tarama)
    od_list: list[dict[str, Any]] = []
    if isinstance(root, dict):
        for k in (
            "originDestinationInformationList",
            "flightAvailabilityList",
            "journeys",
            "boundList",
            "itineraries",
            "sliceList",
            "slices",
            "routes",
            "segments",
            "originDestinationOptionList",
            "flightList",
            "flightOptionList",
            "flights",
            "options",
        ):
            val = root.get(k)
            if isinstance(val, list) and val:
                if isinstance(val[0], dict) and _looks_like_flight_segment_dict(val[0]) and not any(
                    ok in val[0] for ok in ("originDestinationOptionList", "flightOptionList", "flightList", "options", "flights")
                ):
                    od_list = [{"originDestinationOptionList": val, "originAirportCode": "IST", "destinationAirportCode": "ESB"}]
                else:
                    od_list = val
                break

    elif isinstance(root, list) and root:
        if isinstance(root[0], dict) and any(
            ok in root[0] for ok in ("originDestinationOptionList", "flightOptionList", "flightList", "options", "flights")
        ):
            od_list = root
        elif isinstance(root[0], dict) and _looks_like_flight_segment_dict(root[0]):
            od_list = [{"originDestinationOptionList": root, "originAirportCode": "IST", "destinationAirportCode": "ESB"}]

    air = root.get("availabilityInfoResponse") if isinstance(root, dict) else None
    if not od_list and isinstance(air, list) and air:
        for idx, item in enumerate(air):
            if isinstance(item, list) and item:
                od_list.append({
                    "originDestinationOptionList": item,
                    "originAirportCode": "IST" if idx == 0 else "ESB",
                    "destinationAirportCode": "ESB" if idx == 0 else "IST",
                })
            elif isinstance(item, dict):
                od_list.append(item)

    # Eğer standart anahtarlarla od_list bulunamadıysa tüm JSON ağacında derinlemesine OD / uçuş listesi ara
    if not od_list:
        deep_flight_nodes: list[dict[str, Any]] = []
        seen_node_ids: set[int] = set()

        def _deep_find_flights(node: Any) -> None:
            if isinstance(node, dict):
                # Eğer düğüm altında bir uçuş seçeneği listesi varsa
                for k, v in node.items():
                    if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                        if any(_looks_like_flight_segment_dict(x) for x in v):
                            if id(node) not in seen_node_ids:
                                seen_node_ids.add(id(node))
                                od_list.append({
                                    "originAirportCode": node.get("originAirportCode") or node.get("origin") or node.get("departureAirportCode") or "",
                                    "destinationAirportCode": node.get("destinationAirportCode") or node.get("destination") or node.get("arrivalAirportCode") or "",
                                    "departureDate": node.get("departureDate") or node.get("date") or "",
                                    "originDestinationOptionList": v,
                                })
                                return
                if _looks_like_flight_segment_dict(node) and id(node) not in seen_node_ids:
                    seen_node_ids.add(id(node))
                    deep_flight_nodes.append(node)
                    return
                for k, v in node.items():
                    _deep_find_flights(v)
            elif isinstance(node, list):
                for elem in node:
                    _deep_find_flights(elem)

        _deep_find_flights(root)
        if not od_list and deep_flight_nodes:
            od_list = [{
                "originAirportCode": deep_flight_nodes[0].get("originAirportCode") or deep_flight_nodes[0].get("departureAirportCode") or deep_flight_nodes[0].get("origin") or "IST",
                "destinationAirportCode": deep_flight_nodes[0].get("destinationAirportCode") or deep_flight_nodes[0].get("arrivalAirportCode") or deep_flight_nodes[0].get("destination") or "ESB",
                "departureDate": deep_flight_nodes[0].get("departureDate") or deep_flight_nodes[0].get("date") or "",
                "originDestinationOptionList": deep_flight_nodes,
            }]

    root_econ_amt, root_econ_curr = _extract_amount_and_currency(root.get("economyStartingPrice") if isinstance(root, dict) else None)
    root_bus_amt, root_bus_curr = _extract_amount_and_currency(root.get("businessStartingPrice") if isinstance(root, dict) else None)

    segments_out: list[dict[str, Any]] = []

    for idx, od in enumerate(od_list):
        if not isinstance(od, dict):
            continue
        direction = "GİDİŞ" if idx == 0 else ("DÖNÜŞ" if idx == 1 else f"SEGMENT_{idx+1}")
        orig = str(
            od.get("originAirportCode")
            or od.get("departureAirportCode")
            or od.get("originCityCode")
            or od.get("origin")
            or ("IST" if idx == 0 else ("ESB" if idx == 1 else ""))
        )
        dest = str(
            od.get("destinationAirportCode")
            or od.get("arrivalAirportCode")
            or od.get("destinationCityCode")
            or od.get("destination")
            or ("ESB" if idx == 0 else ("IST" if idx == 1 else ""))
        )
        date_val = str(od.get("departureDate") or od.get("arrivalDate") or od.get("date") or "")

        econ_amt, econ_curr = _extract_amount_and_currency(od.get("economyStartingPrice"))
        if econ_amt is None:
            econ_amt, econ_curr = root_econ_amt, root_econ_curr
        bus_amt, bus_curr = _extract_amount_and_currency(od.get("businessStartingPrice"))
        if bus_amt is None:
            bus_amt, bus_curr = root_bus_amt, root_bus_curr

        options = (
            od.get("originDestinationOptionList")
            or od.get("flightOptionList")
            or od.get("flightList")
            or od.get("options")
            or od.get("flights")
            or od.get("itineraries")
            or od.get("items")
            or []
        )

        # Eğer options standart anahtarlarda yoksa od içindeki alt dizileri derinlemesine tara
        if not options:
            for k, v in od.items():
                if isinstance(v, list) and v and isinstance(v[0], dict) and _looks_like_flight_segment_dict(v[0]):
                    options = v
                    break

        # Eğer od'nin kendisi doğrudan bir uçuş nesnesiyse
        if not options and _looks_like_flight_segment_dict(od):
            options = [od]

        od_fallback_packages = _extract_packages_deep(od)
        flights_out: list[dict[str, Any]] = []

        if isinstance(options, list):
            for f_opt in options:
                if not isinstance(f_opt, dict):
                    continue
                parsed_fl = _parse_single_flight_option(
                    f_opt,
                    fallback_orig=orig,
                    fallback_dest=dest,
                    fallback_packages=od_fallback_packages or global_packages_candidates,
                )
                flights_out.append(parsed_fl)

        # Segment bazında paket özetini (EcoFly, ExtraFly, PrimeFly, Business) derle
        seg_pkg_summary: dict[str, dict[str, Any]] = {}
        for fl in flights_out:
            for p in fl.get("packages", []):
                pname = p.get("package_name")
                pamt = p.get("amount")
                pcurr = p.get("currency", "TRY")
                if pname and pamt is not None and pamt > 0:
                    if pname not in seg_pkg_summary or pamt < seg_pkg_summary[pname]["amount"]:
                        seg_pkg_summary[pname] = {"amount": pamt, "currency": pcurr}

        for p in od_fallback_packages:
            pname = p.get("package_name")
            pamt = p.get("amount")
            pcurr = p.get("currency", "TRY")
            if pname and pamt is not None and pamt > 0:
                if pname not in seg_pkg_summary or pamt < seg_pkg_summary[pname]["amount"]:
                    seg_pkg_summary[pname] = {"amount": pamt, "currency": pcurr}

        if econ_amt is not None and econ_amt > 0 and "EcoFly" not in seg_pkg_summary:
            seg_pkg_summary["EcoFly"] = {"amount": round(float(econ_amt), 2), "currency": econ_curr or "TRY"}
        if bus_amt is not None and bus_amt > 0 and "Business" not in seg_pkg_summary:
            seg_pkg_summary["Business"] = {"amount": round(float(bus_amt), 2), "currency": bus_curr or "TRY"}

        # Eğer uçuş listesi kesilmiş/boş kalmış ama başlangıç veya paket fiyatları mevcutsa özet uçuş kaydı ekle
        if not flights_out and seg_pkg_summary:
            synth_pkgs = [
                {
                    "package_name": pk_name,
                    "cabin_class": "BUSINESS" if pk_name == "Business" else "ECONOMY",
                    "amount": pk_info["amount"],
                    "currency": pk_info["currency"],
                    "available_seats": None,
                }
                for pk_name, pk_info in seg_pkg_summary.items()
            ]
            synth_amts = [p["amount"] for p in synth_pkgs if p.get("amount")]
            flights_out.append({
                "flight_number": "TK Uçuşu",
                "origin": orig or "IST",
                "destination": dest or "ESB",
                "departure": date_val,
                "arrival": date_val,
                "duration": "",
                "is_direct": True,
                "min_price": min(synth_amts) if synth_amts else None,
                "currency": synth_pkgs[0]["currency"] if synth_pkgs else "TRY",
                "packages": synth_pkgs,
                "packages_by_name": {p["package_name"]: p["amount"] for p in synth_pkgs},
            })

        all_amts = [
            f["amount"]
            for fl in flights_out
            for f in fl.get("packages", [])
            if f.get("amount") is not None and f["amount"] > 0
        ]
        if econ_amt is not None and econ_amt > 0:
            all_amts.append(econ_amt)
        if bus_amt is not None and bus_amt > 0:
            all_amts.append(bus_amt)
        for pk_info in seg_pkg_summary.values():
            if pk_info.get("amount"):
                all_amts.append(pk_info["amount"])

        min_price = round(min(all_amts), 2) if all_amts else None
        curr_code = (
            econ_curr
            or bus_curr
            or (flights_out[0]["packages"][0]["currency"] if flights_out and flights_out[0].get("packages") else "TRY")
        )

        segments_out.append({
            "direction": direction,
            "origin": orig,
            "destination": dest,
            "date": date_val,
            "min_price": min_price,
            "lowest_price": min_price,
            "currency": curr_code,
            "economy_starting_price": econ_amt,
            "business_starting_price": bus_amt,
            "packages": seg_pkg_summary,
            "flights": flights_out,
        })

    # 3. Eğer od_list hiç bulunamadıysa fakat derin aramada fiyatlar veya paketler yakalandıysa sentetik segment oluştur
    if not segments_out and (global_packages_candidates or all_discovered_prices or root_econ_amt or root_bus_amt):
        synth_pkgs_list: list[dict[str, Any]] = list(global_packages_candidates)
        if not synth_pkgs_list:
            for dp in all_discovered_prices:
                pname = dp.get("package_name") or "EcoFly"
                synth_pkgs_list.append({
                    "package_name": pname,
                    "cabin_class": "BUSINESS" if pname == "Business" else "ECONOMY",
                    "amount": dp["amount"],
                    "currency": dp["currency"],
                    "available_seats": None,
                })

        seg_pkg_map: dict[str, dict[str, Any]] = {}
        for p in synth_pkgs_list:
            pname = p["package_name"]
            pamt = p["amount"]
            if pname not in seg_pkg_map or pamt < seg_pkg_map[pname]["amount"]:
                seg_pkg_map[pname] = {"amount": pamt, "currency": p.get("currency", "TRY")}

        all_candidate_amts = [dp["amount"] for dp in all_discovered_prices if dp.get("amount")] + [
            p["amount"] for p in synth_pkgs_list if p.get("amount")
        ]
        if root_econ_amt:
            all_candidate_amts.append(root_econ_amt)
        if root_bus_amt:
            all_candidate_amts.append(root_bus_amt)
        seg_min = round(min(all_candidate_amts), 2) if all_candidate_amts else None
        seg_curr = (
            all_discovered_prices[0]["currency"]
            if all_discovered_prices
            else (root_econ_curr or root_bus_curr or "TRY")
        )

        segments_out.append({
            "direction": "GİDİŞ",
            "origin": "IST",
            "destination": "ESB",
            "date": "",
            "min_price": seg_min,
            "lowest_price": seg_min,
            "currency": seg_curr,
            "economy_starting_price": root_econ_amt or seg_min,
            "business_starting_price": root_bus_amt,
            "packages": seg_pkg_map,
            "flights": [
                {
                    "flight_number": "TK Uçuşu",
                    "origin": "IST",
                    "destination": "ESB",
                    "departure": "",
                    "arrival": "",
                    "duration": "",
                    "is_direct": True,
                    "min_price": seg_min,
                    "currency": seg_curr,
                    "packages": synth_pkgs_list,
                    "packages_by_name": {k: v["amount"] for k, v in seg_pkg_map.items()},
                }
            ] if synth_pkgs_list else [],
        })

    # 4. Küresel paket özeti (EcoFly, ExtraFly, PrimeFly, Business) ve genel en düşük fiyat (min_price / lowest_price)
    global_packages_summary: dict[str, dict[str, Any]] = {}
    for seg in segments_out:
        for pk_name, pk_val in (seg.get("packages") or {}).items():
            amt = pk_val.get("amount") if isinstance(pk_val, dict) else pk_val
            curr = pk_val.get("currency", seg.get("currency", "TRY")) if isinstance(pk_val, dict) else seg.get("currency", "TRY")
            if amt is not None and amt > 0:
                if pk_name not in global_packages_summary or amt < global_packages_summary[pk_name]["amount"]:
                    global_packages_summary[pk_name] = {"amount": round(float(amt), 2), "currency": curr}

    for p in global_packages_candidates:
        pname = p.get("package_name")
        pamt = p.get("amount")
        pcurr = p.get("currency", "TRY")
        if pname and pamt is not None and pamt > 0:
            if pname not in global_packages_summary or pamt < global_packages_summary[pname]["amount"]:
                global_packages_summary[pname] = {"amount": round(float(pamt), 2), "currency": pcurr}

    for dp in all_discovered_prices:
        pname = dp.get("package_name")
        pamt = dp.get("amount")
        pcurr = dp.get("currency", "TRY")
        if pname and pamt is not None and pamt > 0:
            if pname not in global_packages_summary or pamt < global_packages_summary[pname]["amount"]:
                global_packages_summary[pname] = {"amount": round(float(pamt), 2), "currency": pcurr}

    all_global_amounts: list[float] = [
        seg["min_price"] for seg in segments_out if seg.get("min_price") is not None and seg["min_price"] > 0
    ] + [
        dp["amount"] for dp in all_discovered_prices if dp.get("amount") is not None and dp["amount"] > 0
    ] + [
        pk["amount"] for pk in global_packages_summary.values() if pk.get("amount") is not None and pk["amount"] > 0
    ]

    overall_min_price = round(min(all_global_amounts), 2) if all_global_amounts else None
    overall_currency = (
        segments_out[0].get("currency")
        if segments_out and segments_out[0].get("currency")
        else (all_discovered_prices[0]["currency"] if all_discovered_prices else "TRY")
    )

    if overall_min_price is not None and "EcoFly" not in global_packages_summary:
        global_packages_summary["EcoFly"] = {"amount": overall_min_price, "currency": overall_currency}

    all_flights_flat = [fl for seg in segments_out for fl in seg.get("flights", [])]

    has_show_banner = isinstance(root, dict) and "showBanner" in root
    is_success = isinstance(data, dict) and data.get("success") is True
    if has_show_banner or (is_success and not segments_out and not all_discovered_prices):
        print("\n" + "=" * 80)
        if has_show_banner:
            print("⚠️ [Shadow-API] 'showBanner' içeren yanıt tespit edildi (/api/v1/availability yerine alt servis yanıtı)!")
        else:
            print("⚠️ [Shadow-API] API 'success: True' döndürdü ancak hiçbir uçuş segmenti veya fiyatı ayrıştırılamadı!")
        print("API Yanıtı ('data' / tam JSON gövdesi):")
        try:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception:
            print(str(data)[:2000])
        print("=" * 80 + "\n")

    return {
        "success": bool(segments_out or all_discovered_prices or overall_min_price is not None),
        "min_price": overall_min_price,
        "lowest_price": overall_min_price,
        "currency": overall_currency,
        "packages": global_packages_summary,
        "ecofly_min_price": global_packages_summary.get("EcoFly", {}).get("amount"),
        "extrafly_min_price": global_packages_summary.get("ExtraFly", {}).get("amount"),
        "primefly_min_price": global_packages_summary.get("PrimeFly", {}).get("amount"),
        "business_min_price": global_packages_summary.get("Business", {}).get("amount"),
        "segments": segments_out,
        "flights": all_flights_flat,
        "discovered_prices": all_discovered_prices,
    }


def print_availability_summary(parsed: dict[str, Any], elapsed_ms: float = 0.0) -> None:
    """Ayrıştırılmış uçuş ve fiyat verilerini terminale ve ajana zengin, anlaşılır bir formatta yansıtır."""
    print("\n" + "=" * 80)
    print("✈  TÜRK HAVA YOLLARI (THY) UÇUŞ MÜSAİTLİK VE FİYAT RAPORU")
    if elapsed_ms > 0:
        print(f"⏱  Yanıt Süresi: {elapsed_ms:.2f} ms")
    print("=" * 80)

    segments = parsed.get("segments", [])
    if not segments:
        discovered = parsed.get("discovered_prices", [])
        if discovered:
            print("✔ Uçuş Fiyat Verileri (Derin Tarama ile Bulundu):")
            for item in discovered[:12]:
                print(f"  • {item['path']}: {item['amount']:,.2f} {item['currency']}")
        else:
            print("⚠️ Yapılandırılmış uçuş veya fiyat verisi tespit edilemedi.")
        print("=" * 80 + "\n")
        return

    for seg in segments:
        dir_label = seg.get("direction", "UÇUŞ")
        icon = "🟢" if "GİDİŞ" in dir_label else ("🔵" if "DÖNÜŞ" in dir_label else "✈")
        orig = seg.get("origin", "")
        dest = seg.get("destination", "")
        date_str = seg.get("date", "")
        curr = seg.get("currency", "TRY")
        min_p = seg.get("min_price")

        print(f"\n{icon} [{dir_label}] {orig} ➔ {dest} | Tarih: {date_str}")
        if min_p is not None:
            print(f"   💰 En Düşük Fiyat: {min_p:,.2f} {curr}")
        econ = seg.get("economy_starting_price")
        bus = seg.get("business_starting_price")
        price_tags: list[str] = []
        if econ is not None:
            price_tags.append(f"Economy Başlangıç: {econ:,.2f} {curr}")
        if bus is not None:
            price_tags.append(f"Business Başlangıç: {bus:,.2f} {curr}")
        if price_tags:
            print(f"   ℹ  {' | '.join(price_tags)}")

        flights = seg.get("flights", [])
        if flights:
            print("   " + "-" * 74)
            for f_idx, fl in enumerate(flights, start=1):
                f_no = fl.get("flight_number", "Uçuş")
                raw_dep = fl.get("departure", "")
                raw_arr = fl.get("arrival", "")
                dep = raw_dep[-8:-3] if len(raw_dep) >= 16 else raw_dep
                arr = raw_arr[-8:-3] if len(raw_arr) >= 16 else raw_arr
                dur = fl.get("duration", "")
                mode = "Direkt" if fl.get("is_direct") else "Aktarmalı"
                time_info = f"{dep} ➔ {arr}" if dep and arr else ""
                meta = " | ".join(p for p in (time_info, f"Süre: {dur}" if dur else "", mode) if p)

                print(f"   {f_idx}. {f_no} ({meta})")
                packages = fl.get("packages", [])
                for pkg in packages:
                    p_name = pkg.get("package_name", "Paket")
                    p_amt = pkg.get("amount")
                    p_curr = pkg.get("currency", "TRY")
                    seats = pkg.get("available_seats")
                    seat_info = f" ({seats} koltuk)" if seats else ""
                    if p_amt is not None:
                        print(f"      • {p_name:<16}: {p_amt:>10,.2f} {p_curr}{seat_info}")
            print("   " + "-" * 74)

    print("\n" + "=" * 80 + "\n")


class ThyClient(BaseShadowClient):
    """THY servisi için OwnAPI User-Delegated Browser Middleware istemcisi."""

    def __init__(
        self,
        tenant_id: str = "default",
        token_store: TokenStore | None = None,
        mode: str = "local",
    ) -> None:
        super().__init__(
            service_name="thy",
            base_url=THY_HOME_URL,
            tenant_id=tenant_id,
            token_store=token_store,
            mode=mode,
        )

    def search_flights(
        self,
        departure_date: str = '02-03-2027',
        return_date: str | None = '09-03-2027',
        origin_airport_code: str = 'IST',
        destination_airport_code: str = 'ESB',
        cid: str | None = None,
    ) -> dict[str, Any]:
        return search_flights(
            departure_date=departure_date,
            return_date=return_date,
            origin_airport_code=origin_airport_code,
            destination_airport_code=destination_airport_code,
            cid=cid,
            tenant_id=self.tenant_id,
            mode=self.mode,
            non_interactive=self.non_interactive,
        )

    def search(
        self,
        departure_date: str = '02-03-2027',
        return_date: str | None = '09-03-2027',
        origin_airport_code: str = 'IST',
        destination_airport_code: str = 'ESB',
        cid: str | None = None,
    ) -> dict[str, Any]:
        return self.search_flights(
            departure_date=departure_date,
            return_date=return_date,
            origin_airport_code=origin_airport_code,
            destination_airport_code=destination_airport_code,
            cid=cid,
        )


THYClient = ThyClient


if __name__ == "__main__":
    start_ns = time.perf_counter()
    data = search_flights()
    elapsed_ms = (time.perf_counter() - start_ns) * 1000

    if isinstance(data, dict):
        parsed = data.get("parsed") or parse_thy_availability(data)
        print_availability_summary(parsed, elapsed_ms=elapsed_ms)
        if not parsed.get("segments") and not parsed.get("discovered_prices"):
            print(f"[Shadow-API] Üst Düzey Anahtarlar: {list(data.keys())}")
            if "data" in data and isinstance(data["data"], dict):
                print(f"[Shadow-API] Data Alt Anahtarlar: {list(data['data'].keys())[:10]}")
            print(json.dumps(data, ensure_ascii=False, indent=2)[:800])
    else:
        print(f"[Shadow-API] Yanıt Süresi: {elapsed_ms:.2f} ms")
        print(str(data)[:600])
