import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import httpx
from healer import SessionHealer
import time

class BaseOwnClient:
    def __init__(self, base_url: str):
        self.base_url = base_url

class UcakBiletiClient(BaseOwnClient):
    def __init__(self, base_url: str = "https://www.obilet.com"):
        super().__init__(base_url)

    def search_flight_offers(self, origin_id: str = "250_0", destination_id: str = "1912_170", departure_date: str = "20270707", return_date: str = "20270714", tenant_id: str = "default", mode: str = "local") -> dict:
        """
        İki şehir ve tarih arasında uçak bileti seferlerini ve fiyatlarını sorgular.

        :param origin_id: Kalkış şehrinin ve havaalanının ID'si.
        :param destination_id: Varış şehrinin ve havaalanının ID'si.
        :param departure_date: Kalkış tarihi (YYYYMMDD formatında).
        :param return_date: Dönüş tarihi (YYYYMMDD formatında).
        :return: Uçuş tekliflerinin JSON yanıtı.
        """
        url = f"{self.base_url}/json/ucuslar/async/{origin_id}-{destination_id}/{departure_date}-{return_date}/2a-1c-1s/economy"
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "origin": "https://www.obilet.com",
            "referer": "https://www.obilet.com/ucuslar/250_0-1912_170/20270707-20270714/2a-1c-1s/economy/all"
        }
        healer = SessionHealer()
        headers, params = healer.apply_to_request(headers, {})

        with httpx.Client(timeout=15.0) as client:
            response = client.get(url, headers=headers)

            if response.status_code in (401, 403, 429):
                healer.heal()
                headers, params = healer.apply_to_request(headers, {})
                response = client.get(url, headers=headers)

            response.raise_for_status()
            return response.json()

if __name__ == "__main__":
    client = UcakBiletiClient()
    start_time = time.perf_counter()
    result = client.search_flight_offers()
    end_time = time.perf_counter()
    duration = (end_time - start_time) * 1000
    print(f"Sorgu süresi: {duration:.2f} ms")
    print("Uçuş teklifleri:", result.get("data", {}).get("result", {}))
