from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import parse_qs, unquote_plus, urlparse

from rich.console import Console

console = Console(stderr=True, legacy_windows=False)

# Havalimanı kodu <-> Şehir eşleştirmeleri (Autocomplete listesinde hem IATA kodu hem şehir adı eşleşmesi için)
AIRPORT_CITY_ALIASES: dict[str, list[str]] = {
    "IST": ["IST", "İstanbul", "Istanbul", "İstanbul Havalimanı"],
    "SAW": ["SAW", "Sabiha Gökçen", "İstanbul", "Istanbul"],
    "ESB": ["ESB", "Ankara", "Esenboğa", "Esenboga"],
    "ADB": ["ADB", "İzmir", "Izmir", "Adnan Menderes"],
    "AYT": ["AYT", "Antalya"],
    "TZX": ["TZX", "Trabzon"],
    "ADA": ["ADA", "Adana", "Şakirpaşa", "Çukurova"],
    "DLM": ["DLM", "Dalaman", "Muğla"],
    "BJV": ["BJV", "Bodrum", "Milas", "Muğla"],
    "GZT": ["GZT", "Gaziantep"],
    "ASR": ["ASR", "Kayseri"],
    "VAN": ["VAN", "Van"],
    "DIY": ["DIY", "Diyarbakır", "Diyarbakir"],
    "ERZ": ["ERZ", "Erzurum"],
    "SZF": ["SZF", "Samsun"],
    "ECN": ["ECN", "Lefkoşa", "Ercan", "Kıbrıs"],
    "LHR": ["LHR", "London", "Londra", "Heathrow"],
    "CDG": ["CDG", "Paris", "Charles de Gaulle"],
    "FRA": ["FRA", "Frankfurt"],
    "AMS": ["AMS", "Amsterdam"],
    "JFK": ["JFK", "New York"],
    "250_0": ["İstanbul", "IST"],
    "1912_170": ["Ankara", "ESB"],
}

# Semantik alan anahtar kelimeleri (Türkçe + İngilizce, normalize edilmiş küçük harf)
SEMANTIC_FIELD_KEYWORDS: dict[str, list[str]] = {
    "origin": [
        "nereden",
        "kalkis",
        "kalkis yeri",
        "kalkis havalimani",
        "departure port",
        "departure airport",
        "departure city",
        "departureport",
        "originairport",
        "originport",
        "origin",
        "from airport",
        "flying from",
        "from",
        "hareket",
    ],
    "destination": [
        "nereye",
        "varis",
        "varis yeri",
        "varis havalimani",
        "arrival port",
        "arrival airport",
        "arrival city",
        "arrivalport",
        "destinationairport",
        "destinationport",
        "destination",
        "to airport",
        "flying to",
        "to",
        "inis",
    ],
    "departure_date": [
        "gidis tarihi",
        "gidis",
        "kalkis tarihi",
        "hareket tarihi",
        "departure date",
        "departuredate",
        "depart date",
        "outbound date",
        "start date",
        "depart",
        "check-in",
        "checkin",
    ],
    "return_date": [
        "donus tarihi",
        "donus",
        "varis tarihi",
        "return date",
        "returndate",
        "inbound date",
        "end date",
        "return",
        "check-out",
        "checkout",
    ],
    "query": [
        "urun, kategori veya marka ara",
        "arama yap",
        "is ara",
        "kelime veya is unvani",
        "search jobs",
        "keywords",
        "search",
        "ara",
    ],
}

SEARCH_BUTTON_KEYWORDS: list[str] = [
    "ucus ara",
    "ucus bul",
    "ucuslari listele",
    "ucuslari goster",
    "bilet bul",
    "bilet ara",
    "ucak bileti bul",
    "seferleri goster",
    "sefer ara",
    "search flights",
    "find flights",
    "show flights",
    "ara",
    "bul",
    "sorgula",
    "listele",
    "search",
]

NEGATIVE_BUTTON_KEYWORDS: list[str] = [
    "iptal",
    "kapat",
    "temizle",
    "vazgec",
    "geri",
    "onceki",
    "sonraki",
    "cerez",
    "cookie",
    "reddet",
    "kabul et",
    "üye ol",
    "uye ol",
    "giris yap",
    "login",
    "sign in",
    "sign up",
    "close",
    "cancel",
    "clear",
    "reset",
]


def _normalize_tr(text: str) -> str:
    """Türkçe karakterleri ASCII karşılıklarına dönüştürür ve küçük harfe çevirir."""
    if not text:
        return ""
    tr_map = str.maketrans("ıİğĞüÜşŞöÖçÇ", "iigguussoocc")
    cleaned = str(text).translate(tr_map).lower()
    return re.sub(r"\s+", " ", cleaned).strip()


def _expand_aliases(value: str) -> list[str]:
    """Havalimanı kodu veya şehir değeri için arama ve autocomplete eşleşme adaylarını döndürür."""
    clean = str(value or "").strip()
    if not clean:
        return []
    upper = clean.upper()
    aliases = list(AIRPORT_CITY_ALIASES.get(upper, []))
    if clean not in aliases:
        aliases.insert(0, clean)
    return aliases


def _format_date_variants(date_str: str) -> list[str]:
    """
    Farklı sitelerin bekleyebileceği tarih formatı varyasyonlarını üretir:
    (orijinal, DD.MM.YYYY, DD/MM/YYYY, DD-MM-YYYY, YYYY-MM-DD).
    """
    clean = str(date_str or "").strip()
    if not clean:
        return []
    variants: list[str] = [clean]

    # YYYYMMDD (örn. 20270707)
    if len(clean) == 8 and clean.isdigit():
        yyyy, mm, dd = clean[:4], clean[4:6], clean[6:8]
        for v in (f"{dd}.{mm}.{yyyy}", f"{dd}-{mm}-{yyyy}", f"{yyyy}-{mm}-{dd}", f"{dd}/{mm}/{yyyy}"):
            if v not in variants:
                variants.append(v)
        return variants

    for sep in ("-", ".", "/"):
        if sep in clean:
            parts = clean.split(sep)
            if len(parts) == 3:
                if len(parts[0]) == 4:
                    yyyy, mm, dd = parts[0], parts[1].zfill(2), parts[2].zfill(2)
                else:
                    dd, mm, yyyy = parts[0].zfill(2), parts[1].zfill(2), parts[2]
                for v in (
                    f"{dd}.{mm}.{yyyy}",
                    f"{dd}-{mm}-{yyyy}",
                    f"{yyyy}-{mm}-{dd}",
                    f"{dd}/{mm}/{yyyy}",
                ):
                    if v not in variants:
                        variants.append(v)
                break
    return variants


def extract_semantic_form_params(
    json_payload: Any = None,
    params: dict[str, Any] | None = None,
    url: str | None = None,
    target_url: str | None = None,
    explicit_form_params: dict[str, Any] | None = None,
) -> dict[str, str]:
    """
    İstek gövdesi (`json_payload`), sorgu parametreleri (`params`), hedef URL (`url` / `target_url`)
    veya doğrudan verilen sözlükten semantik form alanlarını (`origin`, `destination`,
    `departure_date`, `return_date`, `query`, `location`) otonom olarak çıkarır.
    """
    extracted: dict[str, str] = {}

    if isinstance(explicit_form_params, dict):
        for k, v in explicit_form_params.items():
            if v is not None and str(v).strip():
                extracted[str(k).strip()] = str(v).strip()

    def _scan_obj(obj: Any) -> None:
        if isinstance(obj, str):
            s = obj.strip()
            if s.startswith("{") or s.startswith("["):
                try:
                    _scan_obj(json.loads(s))
                except Exception:
                    pass
            return

        if isinstance(obj, list):
            # Çoklu uçuş segmenti listesi (örn. originDestinationInformationList veya flightSearchList)
            if len(obj) >= 1 and isinstance(obj[0], dict):
                seg0 = obj[0]
                for ok in ("originAirportCode", "origin_airport_code", "departurePort", "departure_port", "origin", "from"):
                    if seg0.get(ok) and not extracted.get("origin"):
                        extracted["origin"] = str(seg0[ok]).strip()
                for dk in ("destinationAirportCode", "destination_airport_code", "arrivalPort", "arrival_port", "destination", "to"):
                    if seg0.get(dk) and not extracted.get("destination"):
                        extracted["destination"] = str(seg0[dk]).strip()
                for dep_k in ("departureDate", "departure_date", "date", "depDate"):
                    if seg0.get(dep_k) and not extracted.get("departure_date"):
                        extracted["departure_date"] = str(seg0[dep_k]).strip()
                for ret_k in ("returnDate", "return_date", "retDate"):
                    if seg0.get(ret_k) and not extracted.get("return_date"):
                        extracted["return_date"] = str(seg0[ret_k]).strip()

            if len(obj) >= 2 and isinstance(obj[1], dict) and not extracted.get("return_date"):
                seg1 = obj[1]
                for dep_k in ("departureDate", "departure_date", "returnDate", "return_date", "date"):
                    if seg1.get(dep_k):
                        extracted["return_date"] = str(seg1[dep_k]).strip()
                        break

            for item in obj:
                _scan_obj(item)
            return

        if isinstance(obj, dict):
            # Önce iç listeleri kontrol et
            for list_key in ("originDestinationInformationList", "flightSearchList", "segments", "legs", "itineraries"):
                if isinstance(obj.get(list_key), list):
                    _scan_obj(obj[list_key])

            for k, v in obj.items():
                if v is None or isinstance(v, (dict, list, bool)):
                    if isinstance(v, (dict, list)):
                        _scan_obj(v)
                    continue
                val_str = str(v).strip()
                if not val_str:
                    continue
                kl = re.sub(r"[_\-\s]", "", str(k).lower())

                if not extracted.get("origin") and kl in (
                    "origin",
                    "originairportcode",
                    "originairport",
                    "origincitycode",
                    "originid",
                    "departureport",
                    "departureairport",
                    "from",
                    "fromport",
                    "fromairport",
                    "kalkis",
                    "nereden",
                ):
                    extracted["origin"] = val_str
                elif not extracted.get("destination") and kl in (
                    "destination",
                    "destinationairportcode",
                    "destinationairport",
                    "destinationcitycode",
                    "destinationid",
                    "arrivalport",
                    "arrivalairport",
                    "to",
                    "toport",
                    "toairport",
                    "varis",
                    "nereye",
                ):
                    extracted["destination"] = val_str
                elif not extracted.get("departure_date") and kl in (
                    "departuredate",
                    "depdate",
                    "gidistarihi",
                    "outbounddate",
                    "startdate",
                ):
                    extracted["departure_date"] = val_str
                elif not extracted.get("return_date") and kl in (
                    "returndate",
                    "retdate",
                    "donustarihi",
                    "inbounddate",
                    "enddate",
                ):
                    extracted["return_date"] = val_str
                elif not extracted.get("query") and kl in (
                    "keywords",
                    "query",
                    "q",
                    "search",
                    "keyword",
                ):
                    extracted["query"] = val_str
                elif not extracted.get("location") and kl in ("location", "city"):
                    extracted["location"] = val_str

    _scan_obj(json_payload)
    _scan_obj(params)

    # URL query parametrelerinden çıkarım
    for u in (target_url, url):
        if not u:
            continue
        try:
            parsed = urlparse(str(u))
            qs = parse_qs(parsed.query)
            flat_qs = {k: unquote_plus(str(v[0])).strip() for k, v in qs.items() if v}
            _scan_obj(flat_qs)
        except Exception:
            pass

    return extracted


class GenericFormAutomator:
    """
    Tüm konektörlerde (`registry/*`) ve `SessionHealer` (`healer.py`) içinde ortak çalışan
    Genel Amaçlı Akıllı Form Otomasyonu (Generic Form Automation) Katmanı:
    1. Sayfa yüklendiğinde havalimanı/şehir (`origin` / `destination`), tarih (`departure` / `return`)
       veya arama (`query`) input'larını `placeholder`, `name`, `id`, `aria-label`, `data-testid` ve `<label>`
       etiketlerinden semantik olarak bulup otonom doldurur.
    2. Autocomplete (şehir/havalimanı öneri listesi) açılırsa ilk eşleşen sonuca otonom tıklar.
    3. Formun arama / uçuş bul butonunu otomatik tespit edip tıklayarak SPA state'ini (frontend oturumunu)
       tam anlamıyla oluşturur.
    """

    def __init__(self, settle_wait_sec: float = 2.5) -> None:
        self.settle_wait_sec = settle_wait_sec

    async def automate_page_form(
        self,
        page: Any,
        form_params: dict[str, str] | None = None,
        json_payload: Any = None,
        params: dict[str, Any] | None = None,
        url: str | None = None,
        target_url: str | None = None,
        submit_form: bool = True,
    ) -> dict[str, Any]:
        """
        Verilen Playwright `page` üzerinde semantik form otomasyonunu yürütür ve
        yapılan işlemlerin özet raporunu döndürür.
        """
        resolved = extract_semantic_form_params(
            json_payload=json_payload,
            params=params,
            url=url,
            target_url=target_url,
            explicit_form_params=form_params,
        )

        report: dict[str, Any] = {
            "executed": False,
            "detected_params": resolved,
            "filled_fields": {},
            "autocomplete_selected": {},
            "submitted": False,
            "submit_button": None,
        }

        if not resolved:
            return report

        has_flight_fields = bool(
            resolved.get("origin")
            or resolved.get("destination")
            or resolved.get("departure_date")
            or resolved.get("return_date")
        )
        has_search_query = bool(resolved.get("query"))

        if not has_flight_fields and not has_search_query:
            return report

        try:
            # Çerez / bildirim banner'ı formu kapatıyorsa sessizce kapatmayı dene
            await self._dismiss_blocking_banners(page)

            # 1. Origin (Nereden / Kalkış Havalimanı veya Şehir)
            if resolved.get("origin"):
                orig_val = resolved["origin"]
                orig_ok, orig_ac = await self._fill_location_field(
                    page=page,
                    role_key="origin",
                    value=orig_val,
                )
                if orig_ok:
                    report["executed"] = True
                    report["filled_fields"]["origin"] = orig_val
                    report["autocomplete_selected"]["origin"] = orig_ac

            # 2. Destination (Nereye / Varış Havalimanı veya Şehir)
            if resolved.get("destination"):
                dest_val = resolved["destination"]
                dest_ok, dest_ac = await self._fill_location_field(
                    page=page,
                    role_key="destination",
                    value=dest_val,
                )
                if dest_ok:
                    report["executed"] = True
                    report["filled_fields"]["destination"] = dest_val
                    report["autocomplete_selected"]["destination"] = dest_ac

            # 3. Departure Date (Gidiş Tarihi)
            if resolved.get("departure_date"):
                dep_val = resolved["departure_date"]
                dep_ok = await self._fill_date_field(
                    page=page,
                    role_key="departure_date",
                    date_str=dep_val,
                )
                if dep_ok:
                    report["executed"] = True
                    report["filled_fields"]["departure_date"] = dep_val

            # 4. Return Date (Dönüş Tarihi)
            if resolved.get("return_date"):
                ret_val = resolved["return_date"]
                ret_ok = await self._fill_date_field(
                    page=page,
                    role_key="return_date",
                    date_str=ret_val,
                )
                if ret_ok:
                    report["executed"] = True
                    report["filled_fields"]["return_date"] = ret_val

            # 5. Genel Arama Input'u (Eğer origin/destination yoksa ama query varsa)
            if not has_flight_fields and has_search_query:
                q_val = resolved["query"]
                q_ok, q_ac = await self._fill_location_field(
                    page=page,
                    role_key="query",
                    value=q_val,
                )
                if q_ok:
                    report["executed"] = True
                    report["filled_fields"]["query"] = q_val
                    report["autocomplete_selected"]["query"] = q_ac

            # 6. Arama / Uçuş Bul Butonunu Otomatik Tespit Et ve Tıkla
            if submit_form and report["filled_fields"]:
                btn_info = await self._click_search_submit_button(page)
                if btn_info.get("clicked"):
                    report["submitted"] = True
                    report["submit_button"] = btn_info.get("label")
                    console.print(
                        f"[bold green]🤖 [Generic Form Automation] Form dolduruldu "
                        f"({report['filled_fields']}) ve '{btn_info.get('label')}' butonuna tıklandı![/bold green]"
                    )
                    await asyncio.sleep(self.settle_wait_sec)
                else:
                    console.print(
                        f"[cyan]🤖 [Generic Form Automation] Alanlar dolduruldu: {report['filled_fields']}[/cyan]"
                    )
                    await asyncio.sleep(1.0)
        except Exception as exc:
            console.print(f"[dim]Generic Form Automation bildirimi: {exc}[/dim]")

        return report

    async def _dismiss_blocking_banners(self, page: Any) -> None:
        """Form elemanlarının tıklanmasını engelleyebilecek çerez/modal pencerelerini sessizce kapatır."""
        try:
            await page.evaluate(
                """() => {
                    const selectors = [
                        '#onetrust-accept-btn-handler',
                        '[id*="cookie" i] button[id*="accept" i]',
                        '[class*="cookie" i] button[class*="accept" i]',
                        'button[data-testid*="cookie-accept" i]'
                    ];
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el && el.offsetParent !== null) {
                            el.click();
                            break;
                        }
                    }
                }"""
            )
        except Exception:
            pass

    async def _locate_semantic_element(self, page: Any, role_key: str) -> str | None:
        """
        Sayfadaki görünür input/combobox elemanlarını `placeholder`, `name`, `id`, `aria-label`,
        `data-testid` ve `<label>` metinlerine göre puanlayarak en uygun elemana geçici bir
        `data-ownapi-field="<role_key>"` özniteliği atar ve CSS seçicisini döndürür.
        """
        keywords = SEMANTIC_FIELD_KEYWORDS.get(role_key, [])
        if not keywords:
            return None

        attr_val = f"ownapi-{role_key}"
        found = await page.evaluate(
            """({ roleKey, keywords, attrVal }) => {
                const norm = (s) => (s || '')
                    .toString()
                    .replace(/[ıİ]/g, 'i')
                    .replace(/[ğĞ]/g, 'g')
                    .replace(/[üÜ]/g, 'u')
                    .replace(/[şŞ]/g, 's')
                    .replace(/[öÖ]/g, 'o')
                    .replace(/[çÇ]/g, 'c')
                    .toLowerCase()
                    .replace(/\\s+/g, ' ')
                    .trim();

                const isVisible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity) === 0) {
                        return false;
                    }
                    const r = el.getBoundingClientRect();
                    return r.width > 8 && r.height > 8;
                };

                // Önceki işareti temizle
                document.querySelectorAll(`[data-ownapi-target="${attrVal}"]`).forEach(
                    el => el.removeAttribute('data-ownapi-target')
                );

                const candidates = Array.from(document.querySelectorAll(
                    'input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]):not([type="submit"]):not([type="button"]), ' +
                    '[role="combobox"], [role="searchbox"], [contenteditable="true"], ' +
                    'button[aria-haspopup="listbox"], button[aria-haspopup="dialog"], ' +
                    '[data-testid*="origin" i], [data-testid*="destination" i], [data-testid*="departure" i], [data-testid*="return" i], ' +
                    '[class*="port-input" i], [class*="airport-input" i]'
                ));

                let bestEl = null;
                let bestScore = 0;

                for (const el of candidates) {
                    if (!isVisible(el)) continue;
                    // Zaten başka bir role atanmışsa atla
                    const existingRole = el.getAttribute('data-ownapi-assigned');
                    if (existingRole && existingRole !== roleKey) continue;

                    const placeholder = norm(el.getAttribute('placeholder'));
                    const name = norm(el.getAttribute('name'));
                    const id = norm(el.getAttribute('id'));
                    const ariaLabel = norm(el.getAttribute('aria-label'));
                    const testId = norm(el.getAttribute('data-testid') || el.getAttribute('data-cy') || el.getAttribute('data-qa'));
                    const title = norm(el.getAttribute('title'));

                    let labelText = '';
                    if (el.id) {
                        const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                        if (lbl) labelText += ' ' + norm(lbl.innerText || lbl.textContent);
                    }
                    const parentLabel = el.closest('label');
                    if (parentLabel) {
                        labelText += ' ' + norm(parentLabel.innerText || parentLabel.textContent);
                    }
                    const fieldWrap = el.closest('[class*="field" i], [class*="input" i], [class*="port" i], [class*="date" i], [class*="search" i]');
                    if (fieldWrap) {
                        const subLbl = fieldWrap.querySelector('label, [class*="label" i], [class*="title" i]');
                        if (subLbl) labelText += ' ' + norm(subLbl.innerText || subLbl.textContent);
                    }

                    const primaryBlob = `${placeholder} ${ariaLabel} ${name} ${id} ${testId} ${title}`;
                    const fullBlob = `${primaryBlob} ${labelText}`;

                    let score = 0;
                    for (let i = 0; i < keywords.length; i++) {
                        const kw = keywords[i];
                        const weight = Math.max(10, 40 - i * 2);
                        if (placeholder === kw || ariaLabel === kw || name === kw || id === kw) {
                            score += weight + 35;
                        } else if (primaryBlob.includes(kw)) {
                            score += weight + 15;
                        } else if (fullBlob.includes(kw)) {
                            score += weight;
                        }
                    }

                    // Input etiketine ek bonus
                    if (el.tagName.toLowerCase() === 'input') {
                        score += 8;
                    }

                    // Karşıt rol kelimeleri geçiyorsa puan kır (örn. origin ararken nereye/destination geçmesi)
                    if (roleKey === 'origin' && (primaryBlob.includes('nereye') || primaryBlob.includes('destination') || primaryBlob.includes('arrival') || primaryBlob.includes('varis'))) {
                        score -= 60;
                    }
                    if (roleKey === 'destination' && (primaryBlob.includes('nereden') || primaryBlob.includes('origin') || primaryBlob.includes('departure') || primaryBlob.includes('kalkis'))) {
                        score -= 60;
                    }
                    if (roleKey === 'departure_date' && (primaryBlob.includes('donus') || primaryBlob.includes('return'))) {
                        score -= 60;
                    }
                    if (roleKey === 'return_date' && (primaryBlob.includes('gidis') || primaryBlob.includes('departure') || primaryBlob.includes('kalkis'))) {
                        score -= 60;
                    }

                    if (score > bestScore) {
                        bestScore = score;
                        bestEl = el;
                    }
                }

                if (bestEl && bestScore >= 12) {
                    bestEl.setAttribute('data-ownapi-target', attrVal);
                    bestEl.setAttribute('data-ownapi-assigned', roleKey);
                    return `[data-ownapi-target="${attrVal}"]`;
                }
                return null;
            }""",
            {"roleKey": role_key, "keywords": keywords, "attrVal": attr_val},
        )
        return found

    async def _fill_location_field(
        self,
        page: Any,
        role_key: str,
        value: str,
    ) -> tuple[bool, bool]:
        """
        Havalimanı/şehir (`origin` / `destination`) veya arama (`query`) input'unu bulur,
        değeri yazar ve Autocomplete listesi açılırsa ilk eşleşen sonuca tıklar.
        Dönüş: `(filled_ok, autocomplete_clicked)`
        """
        selector = await self._locate_semantic_element(page, role_key)
        if not selector:
            return False, False

        aliases = _expand_aliases(value)
        type_value = aliases[0] if aliases else str(value).strip()
        norm_aliases = [_normalize_tr(a) for a in aliases if a]

        try:
            loc = page.locator(selector).first
            await loc.click(timeout=3000)
            await asyncio.sleep(0.2)

            # Eğer tıklanan öğe bir combobox/button ise ve tıklama sonrası aktif bir <input> açıldıysa onu yakala
            active_input_sel = await page.evaluate(
                """(origSel) => {
                    document.querySelectorAll('[data-ownapi-active-input]').forEach(
                        el => el.removeAttribute('data-ownapi-active-input')
                    );
                    const orig = document.querySelector(origSel);
                    if (orig && orig.tagName && orig.tagName.toLowerCase() === 'input') {
                        return origSel;
                    }
                    const active = document.activeElement;
                    if (active && active.tagName && active.tagName.toLowerCase() === 'input') {
                        active.setAttribute('data-ownapi-active-input', '1');
                        return '[data-ownapi-active-input="1"]';
                    }
                    if (orig) {
                        const innerInput = orig.querySelector('input') || document.querySelector('input:focus, [class*="modal" i] input, [class*="dropdown" i] input, [class*="popup" i] input, [class*="search" i] input');
                        if (innerInput) {
                            innerInput.setAttribute('data-ownapi-active-input', '1');
                            return '[data-ownapi-active-input="1"]';
                        }
                    }
                    return origSel;
                }""",
                selector,
            )

            target_loc = page.locator(active_input_sel).first
            try:
                await target_loc.fill("", timeout=2000)
            except Exception:
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Backspace")

            await target_loc.type(type_value, delay=45)
            await asyncio.sleep(0.45)

            # Autocomplete listesi açıldıysa ilk eşleşen sonuca tıkla
            ac_clicked = await self._select_first_autocomplete_match(
                page=page,
                norm_aliases=norm_aliases,
                input_selector=active_input_sel,
            )
            return True, ac_clicked
        except Exception:
            return False, False

    async def _select_first_autocomplete_match(
        self,
        page: Any,
        norm_aliases: list[str],
        input_selector: str,
    ) -> bool:
        """
        Açılan şehir/havalimanı Autocomplete listesinden (`[role="option"]`, `listbox`, `.autocomplete-item` vb.)
        ilk eşleşen görünür öğeyi bulup tıklar.
        """
        try:
            clicked = await page.evaluate(
                """({ aliases, inputSel }) => {
                    const norm = (s) => (s || '')
                        .toString()
                        .replace(/[ıİ]/g, 'i')
                        .replace(/[ğĞ]/g, 'g')
                        .replace(/[üÜ]/g, 'u')
                        .replace(/[şŞ]/g, 's')
                        .replace(/[öÖ]/g, 'o')
                        .replace(/[çÇ]/g, 'c')
                        .toLowerCase()
                        .replace(/\\s+/g, ' ')
                        .trim();

                    const isVisible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity) === 0) {
                            return false;
                        }
                        const r = el.getBoundingClientRect();
                        return r.width > 10 && r.height > 10;
                    };

                    const inputEl = document.querySelector(inputSel);
                    const optionSelectors = [
                        '[role="option"]',
                        '[role="listbox"] li',
                        '[role="listbox"] [role="button"]',
                        '[class*="autocomplete" i] li',
                        '[class*="autocomplete" i] [class*="item" i]',
                        '[class*="suggestion" i] li',
                        '[class*="suggestion" i] [class*="item" i]',
                        '[class*="port-list" i] li',
                        '[class*="airport-list" i] li',
                        '[class*="dropdown-menu" i] li',
                        '[class*="search-result" i] li',
                        '[data-testid*="option" i]',
                        '[data-testid*="suggestion" i]',
                        '[data-testid*="airport" i]',
                        '.ui-menu-item'
                    ];

                    const rawOptions = Array.from(document.querySelectorAll(optionSelectors.join(', ')));
                    const visibleOptions = rawOptions.filter(el => {
                        if (!isVisible(el)) return false;
                        if (inputEl && (el === inputEl || el.contains(inputEl))) return false;
                        const txt = norm(el.innerText || el.textContent);
                        return txt.length >= 2 && txt.length <= 250;
                    });

                    if (visibleOptions.length === 0) {
                        return false;
                    }

                    // 1. Önce kelime sınırı (word-boundary) ile tam kod veya şehir eşleşen ilk seçeneği ara
                    for (const alias of aliases) {
                        if (!alias) continue;
                        const escaped = alias.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
                        const wordRe = new RegExp(`(^|[^a-z0-9])${escaped}([^a-z0-9]|$)`, 'i');
                        for (const opt of visibleOptions) {
                            const txt = norm(opt.innerText || opt.textContent);
                            if (wordRe.test(txt)) {
                                opt.click();
                                return true;
                            }
                        }
                    }

                    // 2. Alt dizge (substring) eşleşmesi ara
                    for (const alias of aliases) {
                        if (!alias) continue;
                        for (const opt of visibleOptions) {
                            const txt = norm(opt.innerText || opt.textContent);
                            if (txt.includes(alias)) {
                                opt.click();
                                return true;
                            }
                        }
                    }

                    // 3. Eşleşme yoksa açılan listedeki ilk geçerli öneriye tıkla
                    visibleOptions[0].click();
                    return true;
                }""",
                {"aliases": norm_aliases, "inputSel": input_selector},
            )
            if clicked:
                await asyncio.sleep(0.25)
                return True

            # DOM üzerinden bulunamadıysa klavye ile ilk öneriyi seçmeyi dene
            await page.keyboard.press("ArrowDown")
            await asyncio.sleep(0.1)
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.2)
            return True
        except Exception:
            return False

    async def _fill_date_field(
        self,
        page: Any,
        role_key: str,
        date_str: str,
    ) -> bool:
        """
        Gidiş (`departure_date`) veya dönüş (`return_date`) tarih alanını semantik olarak bulur,
        hem input değerini (`input`/`change` event'leriyle) ayarlar hem de açık takvim hücresi varsa seçer.
        """
        selector = await self._locate_semantic_element(page, role_key)
        if not selector:
            return False

        variants = _format_date_variants(date_str)
        if not variants:
            return False

        primary_val = variants[0]
        iso_val = next((v for v in variants if len(v) == 10 and v[4] == "-"), primary_val)

        try:
            filled = await page.evaluate(
                """({ sel, primaryVal, isoVal, allVariants }) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    el.click();
                    el.focus();

                    const targetInput = (el.tagName && el.tagName.toLowerCase() === 'input')
                        ? el
                        : (el.querySelector('input') || el);

                    if (targetInput && targetInput.tagName && targetInput.tagName.toLowerCase() === 'input') {
                        const inType = (targetInput.getAttribute('type') || 'text').toLowerCase();
                        const valToSet = inType === 'date' ? isoVal : primaryVal;

                        try {
                            targetInput.removeAttribute('readonly');
                        } catch (_) {}

                        const nativeSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype,
                            'value'
                        )?.set;
                        if (nativeSetter) {
                            nativeSetter.call(targetInput, valToSet);
                        } else {
                            targetInput.value = valToSet;
                        }
                        targetInput.dispatchEvent(new Event('input', { bubbles: true }));
                        targetInput.dispatchEvent(new Event('change', { bubbles: true }));
                        targetInput.dispatchEvent(new Event('blur', { bubbles: true }));
                    }

                    // Takvim popover açıksa eşleşen gün hücresine tıkla
                    for (const v of allVariants) {
                        const cell = document.querySelector(
                            `[data-date="${v}"], [data-day="${v}"], [aria-label*="${v}"], td[title*="${v}"]`
                        );
                        if (cell && cell.offsetParent !== null) {
                            cell.click();
                            break;
                        }
                    }
                    return true;
                }""",
                {
                    "sel": selector,
                    "primaryVal": primary_val,
                    "isoVal": iso_val,
                    "allVariants": variants,
                },
            )
            await asyncio.sleep(0.25)
            return bool(filled)
        except Exception:
            return False

    async def _click_search_submit_button(self, page: Any) -> dict[str, Any]:
        """
        Formun arama / uçuş bul butonunu (`Uçuş Ara`, `Uçuş Bul`, `Bilet Bul`, `Ara`, `Search` vb.)
        otomatik tespit edip tıklayarak SPA state'ini başlatır.
        """
        try:
            result = await page.evaluate(
                """({ posKeywords, negKeywords }) => {
                    const norm = (s) => (s || '')
                        .toString()
                        .replace(/[ıİ]/g, 'i')
                        .replace(/[ğĞ]/g, 'g')
                        .replace(/[üÜ]/g, 'u')
                        .replace(/[şŞ]/g, 's')
                        .replace(/[öÖ]/g, 'o')
                        .replace(/[çÇ]/g, 'c')
                        .toLowerCase()
                        .replace(/\\s+/g, ' ')
                        .trim();

                    const isVisible = (el) => {
                        if (!el || el.disabled) return false;
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity) === 0) {
                            return false;
                        }
                        const r = el.getBoundingClientRect();
                        return r.width > 16 && r.height > 16;
                    };

                    const candidates = Array.from(document.querySelectorAll(
                        'button, input[type="submit"], input[type="button"], [role="button"], a[class*="btn" i], a[class*="button" i], a[class*="search" i]'
                    ));

                    let bestBtn = null;
                    let bestScore = 0;
                    let bestLabel = '';

                    for (const btn of candidates) {
                        if (!isVisible(btn)) continue;

                        const rawText = (btn.innerText || btn.value || btn.getAttribute('aria-label') || btn.getAttribute('title') || '').trim();
                        const txt = norm(rawText);
                        const aria = norm(btn.getAttribute('aria-label'));
                        const testId = norm(btn.getAttribute('data-testid') || btn.getAttribute('data-cy') || btn.getAttribute('id') || btn.getAttribute('name'));
                        const cls = norm(btn.className);
                        const blob = `${txt} ${aria} ${testId} ${cls}`;

                        if (!blob.trim()) continue;
                        if (negKeywords.some(neg => txt === neg || blob.includes(neg))) {
                            continue;
                        }

                        let score = 0;
                        for (let i = 0; i < posKeywords.length; i++) {
                            const kw = posKeywords[i];
                            const weight = Math.max(12, 50 - i * 2);
                            if (txt === kw || aria === kw) {
                                score += weight + 40;
                            } else if (txt.includes(kw) || aria.includes(kw)) {
                                score += weight + 20;
                            } else if (testId.includes(kw) || cls.includes(kw)) {
                                score += weight;
                            }
                        }

                        if ((btn.getAttribute('type') || '').toLowerCase() === 'submit') {
                            score += 25;
                        }

                        if (score > bestScore) {
                            bestScore = score;
                            bestBtn = btn;
                            bestLabel = rawText || btn.getAttribute('aria-label') || btn.getAttribute('id') || 'submit';
                        }
                    }

                    if (bestBtn && bestScore >= 20) {
                        bestBtn.click();
                        return { clicked: true, label: bestLabel.slice(0, 60), score: bestScore };
                    }
                    return { clicked: false, label: null, score: 0 };
                }""",
                {
                    "posKeywords": SEARCH_BUTTON_KEYWORDS,
                    "negKeywords": NEGATIVE_BUTTON_KEYWORDS,
                },
            )
            if isinstance(result, dict) and result.get("clicked"):
                return result
        except Exception:
            pass
        return {"clicked": False, "label": None, "score": 0}


async def run_generic_form_automation(
    page: Any,
    form_params: dict[str, str] | None = None,
    json_payload: Any = None,
    params: dict[str, Any] | None = None,
    url: str | None = None,
    target_url: str | None = None,
    submit_form: bool = True,
    settle_wait_sec: float = 2.5,
) -> dict[str, Any]:
    """Kısayol yardımcı fonksiyon: Verilen Playwright sayfasında GenericFormAutomator çalıştırır."""
    automator = GenericFormAutomator(settle_wait_sec=settle_wait_sec)
    return await automator.automate_page_form(
        page=page,
        form_params=form_params,
        json_payload=json_payload,
        params=params,
        url=url,
        target_url=target_url,
        submit_form=submit_form,
    )
