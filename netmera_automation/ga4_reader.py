"""GA4 işlerini Netmera çalışmasında okur: tarayıcı açılmaz, GA4 Data API (veya Demo'da sentetik kaynak) kullanılır.

Analytics Veri Çekme'nin `rapor.run_job` akışı aynen kullanılır (eşik/örnekleme/(other) uyarıları, 0 yazmama).
Her metrik bir sütundur: kırılımsız işte tek satırın değeri, kırılımlı işte GA4'ün kendi toplamı. Tablonun tamamı
`tables` içinde saklanır; arayüz sonuç tablosu ve dışa aktarma için kullanır.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .panel import PanelError
from .paths import TIMEZONE


class Ga4Reader:
    def __init__(self, profile: dict, log=print, client=None):
        self.profile, self.log = profile, log
        self._client, self._error = client, None
        self.tables: list[dict] = []

    def client(self):
        if self._client is None and self._error is None:
            from .ga4.ayarlar import SettingsError
            from .ga4.istemci import ClientError
            try:
                if self.profile["source"] == "demo":
                    from .ga4.sentetik import SyntheticClient
                    self._client = SyntheticClient()
                else:
                    from .ga4 import ayarlar
                    from .ga4.ga4 import RestClient
                    self._client = RestClient(ayarlar.load())
            except (SettingsError, ClientError) as exc:
                self._error = str(exc).splitlines()[0]
        if self._client is None:
            raise PanelError("GA4_NOT_CONNECTED", detail=self._error)
        return self._client

    def fetch(self, job: dict, first: date, last: date):
        from .ga4 import rapor
        today = datetime.now(ZoneInfo(TIMEZONE)).date()
        ga4 = {"ad": job["label"], "etkin": True, **job["ga4"]}
        if (first, last) != rapor.resolve_dates(ga4["tarih"], today):
            # Çalıştır'da seçilen tarih / günlere bölme / kaçan gün: işin tarihi bu aralıkla değişir
            ga4 = {**ga4, "tarih": {"tur": "aralik", "baslangic": first.isoformat(), "bitis": last.isoformat()}}
        result = rapor.run_job(ga4, self.client(), today=today)
        result["job_id"] = job["id"]
        self.tables.append(result)
        if result["durum"] == "hata":
            raise PanelError("GA4_ERROR", detail=result["hata"])
        if not result["boyutlar"]:
            values = result["satirlar"][0]["metrikler"]
        elif result["toplam"]:
            values = result["toplam"]
        else:
            raise PanelError("GA4_ERROR", detail="kırılımlı işte GA4 toplamı alınamadı")
        notes = [rapor.WARNINGS[w] for w in result["uyarilar"] if w in rapor.WARNINGS]
        if result["boyutlar"]:
            notes.insert(0, "GA4 toplamı (kırılım: " + ", ".join(result["boyutlar"]) + ")")
        return {m: values[m] for m in job["columns"]}, " · ".join(notes) or None
