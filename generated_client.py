from __future__ import annotations

import json
import pathlib
import sys
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from healer import SessionHealer, update_payload_deep


def search_flights(departure_date: str = '02-03-2027', return_date: str = '09-03-2027', origin_airport_code: str = 'IST', destination_airport_code: str = 'ESB') -> dict[str, Any]:
    """
    Kullanıcının hedefi İstanbul'dan Ankara'ya uçuş seferlerini ve fiyatlarını sorgulamak olduğundan, uçuş müsaitlik ve fiyat bilgilerini içeren '/api/v1/availability' uç noktası en uygun seçimdir. Bu uç nokta, uçuş bilgilerini almak için gerekli olan tüm parametreleri içermektedir.
    Hedef Endpoint: POST https://www.turkishairlines.com/api/v1/availability
    """
    params: dict[str, Any] = {}
    headers: dict[str, str] = {'accept': 'application/json', 'content-type': 'application/json', 'cookie': '_pxvid=c185282e-d5e2-11f1-86f2-308a27c6d3c4; __pxvid=c1d25fc6-d5e2-11f1-abd9-0e7280d394fb; _pk_id.1.d2a8=4f25b97ed45b528c.1793530544.; _fbp=fb.1.1793530544915.313242626834724538; _tt_enable_cookie=1; _ttp=01M6BC0HR4Z3YPGVDT35KNA93E_.tt.1.1793530545925; QuantumMetricUserID=e6900c8c506a489e07af4399ff7be96e; dtCookie=v_4_srv_31_sn_89C5C4E44BA084D39D641470FF8642BB_perc_100000_ol_0_mul_1_app-3A4d1273d1581bf374_0_rcs-3Acss_0; 56428a0cbc0b407645f50388c7919354=393e859695b0ea637541fb372eac029e; geo_country=TR; akamai_country=TR; managebookingversion=4.0; checkinversion=4.4; digitalprofile=4.0; AKA_A2=A; JSESSIONID=0000WTXahgQz2xEVNYDsEzda_Qt:1gaogr5m2; TAFSessionId=tridion_caa9bb4d-504e-4889-811e-e2cd04407f86; TAFTrackingId=tridion_e1ed68e8-d4a5-414b-b39e-9722baabce6b; TurkishAirlines_P03NSC=!WUuGdsdF7zVzfTVO8LDknmlRSN7vi+fUg/f4dDV3u9IDVRVkRf4meBoN0VNvZkBUyLRh4ZK6/50PORM=; ak_bmsc=9CE23E458FB07AD0572D0E36D3837414~000000000000000000000000000000~YAAQh8ETAj5sn3OhAQAAiKnUlwG8BYvKAfTflIsYExzOJGyS8rxWw7geKchCWOiuYtJWmt/cIJ4XX68oedR7aN9/31q4sj9SkbnvNOI5mjFSFEhkusAc7JTppR/9Icdwnm/7CHU0nIiW5i7hTf7hda2jab4EwvUcE3GyEGLyPLcsjv/ttZp8ESkUkE7B1+VB6KCVBrVuQRQi7c8gDr/E8eRtP92jIu9LlIDdIgKbnMS0hVGoZkdbFMC48KQEgdI9OhMI3EOucg8h9/UgiDjvQ+3kSMKhT649CtVdAXad36IU/QlYJVTZDMGnaUfNJWnfxMQsUxPBNKVI50avgK6VnaMFTWFnAqYW3/EB/b2xD9P5Bq+//nvlGwzVIdE8fcCkbjkBkyhWTU3RhTZgmAiwHEeoed5itZDxTXXEEuW6qTf7hsm0NFzJuzPumiPbbotyb745x8N5eYIg1LEi2taZGylvD7XwXYHttQ==; Locale=tr-tr; pfx_postclick=gelirortaklari; _pk_ses.1.d2a8=1; managebookingversiontwo=4.0; msr-db-checkin=B; QuantumMetricSessionID=d59823da34f440278e5a51cb30b1a404; x-current-page-url=%2Ftr-tr; _px2=eyJ1IjoiMTZmMGJhMzAtZDYwZC0xMWYxLWE0YjUtYWQ5ZGIyNTgzMzJmIiwidiI6ImMxODUyODJlLWQ1ZTItMTFmMS04NmYyLTMwOGEyN2M2ZDNjNCIsInQiOjE3OTM1NDkwMTYxMTksImgiOiJmZTQ0ZGU5OGU4ZjJiYjNhMDU2ZTRmODI3NzAxMWQ4OWIyYzFlMDAwMTQ1ZWMwZTY1MjAzMDBlNTE1YzAzYzNlIn0=; g_state={"i_l":0,"i_ll":1793548722427,"i_b":"b2x/mFnsQxwaCx+4wj8/FC1wU+Wua9XuuUiAEAclHZU","i_e":{"enable_itp_optimization":24},"i_et":1793548722427}; pxcts=ueyH3qlbysYwclAygCNUzkHffhzUyoh8gh234PFUsHM=:KWhzpbAem8Y8w5rJ91lgewScmnc-B3YyRJmluAI9joWwSNDzIG8HX/n76XeuiBUSPzfV/hdbmgrXNlAD5aRYxZ7O6/4A7AeWSLXaz36Z-Mzwf26EWVx5lFfRaP0BD3X17Anqf-ZmqjaF1jknXt4Ne7W8dheTEQNrgB0PfXF1A-c5ixiWtf5p-iegfnkar9J-; COOKIE_WARNING_VIEWED=true; COOKIE_FUNCTIONAL=false; COOKIE_ADVERTISEMENTS=false; COOKIE_ANALYTICS=false; COOKIE_GOOGLE_ENHANCED_CONVERSIONS=false; COOKIE_WARNING_REJECTED=true; COOKIE_WARNING_ACCEPTED=false; _pxhd=e4825f1628b051fd1feba5cfc360548d4aa24f6b8863fb44ae6d7371231d266a:c185282e-d5e2-11f1-86f2-308a27c6d3c4; bm_sv=FEEF67B9CE83B84DAF94A0AD702AFE15~YAAQh8ETAiafn3OhAQAAFknWlwGw4YcQXUwEsiYpNuyxYuuIlsduA7mSVKCbtz1PPytGFr6mt/z0kc3S/2Itn+05udaB3rwHLRWbi5I+MYB8QblALttIyhJGzNeJIXwuYxPUZLDgkuuMFCQ2zaoIhaEvmsaIL7lLOv/HeyXvZsb3sVx6NIxJGKp6BJC5p3yVB/3877Kx7uPqLLEuvtLSD0q2MAMIpi/yFd2qce4jX056LXIkPIJJV9LE+GFd4f5XRywrtKEpzlWs5g==~1; RT="z=1&dm=turkishairlines.com&si=9c853d09-9499-485f-a164-1be39d060fef&ss=mvy0k8za&sl=4&tt=ugq&bcn=%2F%2Facebe075.akstat.io%2F&ld=2d5w"; ttcsid_COT8NMRC77UF1T0I4U80=1793548667487::nLh_mhAZYVnuhlCprrdU.2.1793548768398.1; ttcsid=1793548667489::8UQpaISmlCd1AO-KZ5Vq.2.1793548768398.0::1.100430.98818::97150.13.1047.571::98763.11.0', 'origin': 'https://www.turkishairlines.com', 'referer': 'https://www.turkishairlines.com/tr-tr/ucak-bileti/rezervasyon/', 'sec-ch-ua': '"Not:A-Brand";v="8", "Chromium";v="156", "Google Chrome";v="156"', 'sec-ch-ua-mobile': '?0', 'sec-ch-ua-platform': '"Windows"', 'sec-fetch-dest': 'empty', 'sec-fetch-mode': 'cors', 'sec-fetch-site': 'same-origin', 'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36'}
    payload: Any = {'selectedBookerSearch': 'R', 'selectedCabinClass': 'ECONOMY', 'moduleType': 'TICKETING', 'passengerTypeList': [{'quantity': 1, 'code': 'ADULT'}], 'originDestinationInformationList': [{'originAirportCode': 'IST', 'originCountryCode': 'TR', 'originMultiPort': True, 'originDomestic': True, 'destinationAirportCode': 'ESB', 'destinationCountryCode': 'TR', 'destinationMultiPort': False, 'destinationDomestic': True, 'departureDate': '02-03-2027', 'originCityCode': 'IST', 'destinationCityCode': 'ANK', 'originCity': 'İstanbul', 'destinationCity': 'Ankara'}, {'originAirportCode': 'ESB', 'originCountryCode': 'TR', 'originMultiPort': False, 'originDomestic': True, 'destinationAirportCode': 'IST', 'destinationCountryCode': 'TR', 'destinationMultiPort': True, 'destinationDomestic': True, 'departureDate': '09-03-2027', 'originCityCode': 'ANK', 'destinationCityCode': 'IST', 'originCity': 'Ankara', 'destinationCity': 'İstanbul'}], 'savedDate': '2026-11-01T15:59:24.857Z', 'preselectedOptionDetails': []}
    # Query parametresi güncellemesi yok
    payload = update_payload_deep(payload, {'departureDate': departure_date, 'departureDate': return_date, 'originAirportCode': origin_airport_code, 'destinationAirportCode': destination_airport_code})

    healer = SessionHealer()
    # Dışarıdan doğrudan HTTP atmak yerine tarayıcı oturumu üzerinden
    # tarayıcının kendi JavaScript ortamında isteği çalıştır (WAF / Akamai bypass)
    return healer.execute_in_browser(
        method='POST',
        url='https://www.turkishairlines.com/api/v1/availability',
        headers=headers,
        params=params,
        json_payload=payload if True else None,
    )


if __name__ == "__main__":
    start_ns = time.perf_counter()
    data = search_flights()
    elapsed_ms = (time.perf_counter() - start_ns) * 1000

    print(f"[Shadow-API] Yanıt Süresi: {elapsed_ms:.2f} ms")
    if isinstance(data, dict):
        if "hits" in data and isinstance(data["hits"], list):
            hits = data["hits"]
            print(f"[Shadow-API] Toplam Sonuç (nbHits): {data.get('nbHits', len(hits))} | Dönen: {len(hits)}")
            for idx, item in enumerate(hits[:5], start=1):
                title = item.get("title") or item.get("story_title") or item.get("name") or str(item)[:80]
                url = item.get("url") or item.get("story_url") or ""
                print(f"  {idx}. {title} {f'({url})' if url else ''}")
        elif "pages" in data and isinstance(data["pages"], list):
            pages = data["pages"]
            print(f"[Shadow-API] Bulunan Sayfa Sayısı: {len(pages)}")
            for idx, item in enumerate(pages[:5], start=1):
                title = item.get("title") or item.get("key") or str(item)[:80]
                desc = item.get("description") or ""
                key = item.get("key") or ""
                wiki_url = f"https://en.wikipedia.org/wiki/{key}" if key else ""
                print(f"  {idx}. {title} — {desc} {f'({wiki_url})' if wiki_url else ''}")
        else:
            print(f"[Shadow-API] Üst Düzey Anahtarlar: {list(data.keys())}")
            preview = json.dumps(data, ensure_ascii=False, indent=2)[:600]
            print(preview)
    else:
        print(str(data)[:600])

