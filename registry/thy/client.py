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


def _extract_amount_and_currency(obj: Any) -> tuple[float | None, str]:
    """Herhangi bir fiyat/tutar nesnesinden sayısal tutar ve para birimini derinlemesine ayıklar."""
    if obj is None:
        return None, ""
    if isinstance(obj, (int, float)):
        return float(obj), "TRY"
    if isinstance(obj, dict):
        curr = (
            obj.get("currency")
            or obj.get("currencyCode")
            or obj.get("currency_code")
            or obj.get("code")
            or ""
        )
        amt = None
        for k in (
            "amount", "totalAmount", "price", "totalPrice", "value",
            "fare", "totalFare", "baseFare", "startingPrice", "starting_price",
            "amountInPreferredCurrency", "fareAmount", "cashAmount"
        ):
            v = obj.get(k)
            if v is not None:
                if isinstance(v, (int, float)):
                    amt = float(v)
                    break
                elif isinstance(v, str):
                    try:
                        amt = float(v.replace(".", "").replace(",", ".").strip()) if ("," in v and "." in v) else float(v.replace(",", ".").strip())
                        break
                    except ValueError:
                        pass
                elif isinstance(v, dict):
                    sub_amt, sub_curr = _extract_amount_and_currency(v)
                    if sub_amt is not None:
                        amt = sub_amt
                        curr = curr or sub_curr
                        break
        return amt, str(curr or "TRY")
    return None, ""


def parse_thy_availability(data: dict[str, Any]) -> dict[str, Any]:
    """
    THY /api/v1/availability yanıtını derinlemesine analiz eder.
    Gidiş ve dönüş seferlerine ait uçuş numaraları, saatler, paketler (EcoFly, ExtraFly vb.)
    ve fiyatları (amount, currency) ayrıştırılmış bir sözlük olarak döndürür.
    """
    if not isinstance(data, dict):
        return {"success": False, "error": "Geçersiz yanıt formatı", "segments": [], "discovered_prices": []}

    root = data.get("data") if isinstance(data.get("data"), dict) else data

    # 1. Rota listesini bul (originDestinationInformationList veya alternatifleri)
    od_list: list[dict[str, Any]] = []
    if isinstance(root, dict):
        for k in (
            "originDestinationInformationList",
            "flightAvailabilityList",
            "journeys",
            "boundList",
            "itineraries",
            "originDestinationOptionList",
            "flightList",
            "flightOptionList",
            "flights",
            "options",
        ):
            val = root.get(k)
            if isinstance(val, list) and val:
                # Eğer val doğrudan uçuş seçenekleri dizisiyse:
                if val and isinstance(val[0], dict) and ("flightSegmentList" in val[0] or "fareCategory" in val[0] or "segments" in val[0]):
                    od_list = [{"originDestinationOptionList": val, "originAirportCode": "IST", "destinationAirportCode": "ESB"}]
                else:
                    od_list = val
                break
    elif isinstance(root, list) and root:
        if isinstance(root[0], dict) and ("originDestinationOptionList" in root[0] or "flightOptionList" in root[0]):
            od_list = root
        elif isinstance(root[0], dict) and ("flightSegmentList" in root[0] or "fareCategory" in root[0] or "segments" in root[0]):
            od_list = [{"originDestinationOptionList": root, "originAirportCode": "IST", "destinationAirportCode": "ESB"}]

    # Eğer availabilityInfoResponse içinde uçuşlar varsa
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

    segments_out: list[dict[str, Any]] = []

    for idx, od in enumerate(od_list):
        if not isinstance(od, dict):
            continue
        direction = "GİDİŞ" if idx == 0 else ("DÖNÜŞ" if idx == 1 else f"SEGMENT_{idx+1}")
        orig = od.get("originAirportCode") or od.get("originCityCode") or od.get("origin") or ""
        dest = od.get("destinationAirportCode") or od.get("destinationCityCode") or od.get("destination") or ""
        date_val = od.get("departureDate") or od.get("date") or ""

        # Başlangıç fiyatları (önce segmentten, yoksa root seviyesinden)
        econ_amt, econ_curr = _extract_amount_and_currency(od.get("economyStartingPrice") or root.get("economyStartingPrice"))
        bus_amt, bus_curr = _extract_amount_and_currency(od.get("businessStartingPrice") or root.get("businessStartingPrice"))

        flights_out: list[dict[str, Any]] = []
        options = (
            od.get("originDestinationOptionList")
            or od.get("flightOptionList")
            or od.get("flightList")
            or od.get("options")
            or od.get("flights")
            or []
        )
        if isinstance(options, list):
            for f_opt in options:
                if not isinstance(f_opt, dict):
                    continue

                # Uçuş segment detayları
                seg_list = f_opt.get("flightSegmentList") or f_opt.get("segments") or []
                flight_no = ""
                dep_time = ""
                arr_time = ""
                dur_str = ""
                is_direct = True

                if isinstance(seg_list, list) and seg_list:
                    is_direct = len(seg_list) == 1
                    first_seg = seg_list[0] if isinstance(seg_list[0], dict) else {}
                    last_seg = seg_list[-1] if isinstance(seg_list[-1], dict) else {}

                    # Uçuş kodu
                    fn = first_seg.get("flightNumber") or first_seg.get("carrierFlightNumber") or ""
                    carrier = first_seg.get("operatingAirline", {}).get("code") or first_seg.get("carrierCode") or "TK"
                    flight_no = f"{carrier} {fn}".strip() if fn else ""

                    dep_time = str(first_seg.get("departureDateTime") or first_seg.get("departureDate") or "")
                    arr_time = str(last_seg.get("arrivalDateTime") or last_seg.get("arrivalDate") or "")
                    dur_str = str(f_opt.get("duration") or first_seg.get("duration") or first_seg.get("flightDuration") or "")

                # Paket ve ücret detayları
                fares_out: list[dict[str, Any]] = []

                # 1. fareCategory altındaki paketler (captured_traffic.json şeması)
                fare_category = f_opt.get("fareCategory")
                if isinstance(fare_category, dict):
                    for cabin_name, cabin_obj in fare_category.items():
                        if isinstance(cabin_obj, dict):
                            booking_list = cabin_obj.get("bookingPriceInfoList") or []
                            if isinstance(booking_list, list):
                                for b_info in booking_list:
                                    if not isinstance(b_info, dict):
                                        continue
                                    pkg_name = (
                                        b_info.get("brandName")
                                        or b_info.get("brandKey")
                                        or b_info.get("brandCode")
                                        or cabin_name
                                    )
                                    pkg_amt, pkg_curr = _extract_amount_and_currency(
                                        b_info.get("startingPrice") or b_info.get("price") or b_info
                                    )
                                    seat_count = b_info.get("availableSeatCount") or b_info.get("seatCount") or b_info.get("seats")
                                    if pkg_amt is not None:
                                        clean_pkg_name = str(pkg_name).replace("brand.", "").capitalize()
                                        fares_out.append({
                                            "package_name": clean_pkg_name,
                                            "cabin_class": cabin_name,
                                            "amount": pkg_amt,
                                            "currency": pkg_curr,
                                            "available_seats": seat_count,
                                        })

                # 2. Alternatif grup listeleri (eski formatlar)
                subgroups = (
                    f_opt.get("fareSubGroupList")
                    or f_opt.get("subGroupList")
                    or f_opt.get("fares")
                    or f_opt.get("fareCategories")
                    or f_opt.get("brandList")
                    or []
                )
                if isinstance(subgroups, list):
                    for sub in subgroups:
                        if not isinstance(sub, dict):
                            continue
                        pkg_name = (
                            sub.get("subGroupName")
                            or sub.get("brandName")
                            or sub.get("brandCode")
                            or sub.get("cabinClass")
                            or sub.get("fareFamilyCode")
                            or "Standart"
                        )
                        cabin = sub.get("cabinClass") or sub.get("cabin") or ""
                        pkg_amt, pkg_curr = _extract_amount_and_currency(
                            sub.get("price") or sub.get("totalPrice") or sub.get("fare") or sub
                        )
                        seat_count = sub.get("seatCount") or sub.get("availableSeats")

                        if pkg_amt is not None:
                            fares_out.append({
                                "package_name": pkg_name,
                                "cabin_class": cabin,
                                "amount": pkg_amt,
                                "currency": pkg_curr,
                                "available_seats": seat_count,
                            })

                # Eğer f_opt doğrudan bir price içeriyorsa ve fares_out boşsa
                opt_amt, opt_curr = _extract_amount_and_currency(
                    f_opt.get("price") or f_opt.get("cheapestPrice") or f_opt.get("totalPrice")
                )
                if not fares_out and opt_amt is not None:
                    fares_out.append({
                        "package_name": "Standart",
                        "cabin_class": "ECONOMY",
                        "amount": opt_amt,
                        "currency": opt_curr,
                        "available_seats": None,
                    })

                flights_out.append({
                    "flight_number": flight_no or "TK Uçuşu",
                    "departure": dep_time,
                    "arrival": arr_time,
                    "duration": dur_str,
                    "is_direct": is_direct,
                    "packages": fares_out,
                })

        # Min fiyat hesapla
        all_amts = [
            f["amount"]
            for fl in flights_out
            for f in fl.get("packages", [])
            if f.get("amount") is not None
        ]
        if econ_amt is not None:
            all_amts.append(econ_amt)
        if bus_amt is not None:
            all_amts.append(bus_amt)
        min_price = min(all_amts) if all_amts else None
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
            "currency": curr_code,
            "economy_starting_price": econ_amt,
            "business_starting_price": bus_amt,
            "flights": flights_out,
        })

    # Derinlemesine fiyat araması (fallback / ekstra doğrulama)
    all_discovered_prices: list[dict[str, Any]] = []

    def _deep_search(node: Any, path: str = "") -> None:
        if isinstance(node, dict):
            if any(k in node for k in ("amount", "totalAmount", "totalPrice", "fare")):
                a, c = _extract_amount_and_currency(node)
                if a is not None and a > 0:
                    all_discovered_prices.append({
                        "path": path,
                        "amount": a,
                        "currency": c,
                    })
            for k, v in node.items():
                _deep_search(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list):
            for i, elem in enumerate(node):
                _deep_search(elem, f"{path}[{i}]")

    _deep_search(root)

    # API success: True döndürdüğünde ama data içinde showBanner veya uçuşsuz sonuç geldiğinde gelen tüm veriyi derinlemesine yazdır
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
        "success": bool(segments_out or all_discovered_prices),
        "segments": segments_out,
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
