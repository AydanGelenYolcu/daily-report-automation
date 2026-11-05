"""Yerel arayüz: yalnız 127.0.0.1'de çalışır, her açılışta yeni gizli oturum anahtarı kullanır.

Tarayıcıdaki sayfa (ui/index.html) bu sunucunun JSON API'sini çağırır. Anahtar dosyası tarayıcıdan bu yerel
sunucuya gelir ve OneDrive dışındaki veri klasörüne yazılır; hiçbir yere gönderilmez.
"""
from __future__ import annotations

import csv
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import secrets
import socket
import threading
import time
import urllib.request
from urllib.parse import parse_qs, urlparse
import webbrowser

from . import VERSION, adlar as ad_sozlugu, ayarlar, cikti, disari, isler, meta, paketler, projeler, rapor, store, zamanli, zamanlayici
from .ayarlar import SettingsError
from .istemci import ClientError
from .paths import PathError, data_root

UI_FILE = Path(__file__).resolve().parent / "ui" / "index.html"
USER_ERRORS = (isler.JobError, SettingsError, PathError, ClientError, cikti.OutputError, disari.ExportError,
               zamanlayici.ZamanlamaHatasi, projeler.ProjeHatasi)
BAGLANTI_DOSYASI = "baglanti-durumu.json"
GUNLUK_DOSYASI = "arayuz-gunluk.txt"  # yalnız olay ve hata türü; belirteç, adres, sayı yazılmaz
MAX_GOVDE = 2_000_000
MAX_ANAHTAR = 64_000


def _gunluk(olay: str):
    """Arayüz neden durdu / giriş nerede kaldı sorusu için kısa iz. Konsola yazılmaz (QuickEdit seçimi sunucuyu dondurur)."""
    try:
        yol = data_root() / GUNLUK_DOSYASI
        if yol.exists() and yol.stat().st_size > 200_000:
            yol.write_text("", encoding="utf-8")
        with yol.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {olay}\n")
    except OSError:
        pass


def _oku_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _yaz_json(p: Path, veri):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(veri, ensure_ascii=False), encoding="utf-8")


class App:
    def __init__(self, port: int):
        self.port = port
        self.token = secrets.token_urlsafe(24)
        self.son_ozet = None
        self._ga4 = None
        self.tarayici = webbrowser.open  # Google izin ekranı (testlerde değiştirilir)
        self._iptal = None
        self._giris_adresi = None  # bekleyen Google izin adresi: sekme açılmadıysa arayüz bağlantı olarak gösterir
        self._giris_kilidi = threading.Lock()

    # ---------- yardımcılar ----------

    def istemci(self, kaynak: str):
        if kaynak == "ga4":
            if self._ga4 is None:
                from .ga4 import RestClient
                self._ga4 = RestClient(ayarlar.load())
            return self._ga4
        from .sentetik import SyntheticClient
        return SyntheticClient()

    def meta_veya_yok(self, kaynak: str):
        """Hazır rapor uygunluğu için metadata; bağlantı yoksa kütüphane yine gösterilir, uygunluk 'bilinmiyor' olur."""
        try:
            return meta.fetch(self.istemci(kaynak), "PROP_PROD"), None
        except (ClientError, SettingsError) as exc:
            return None, str(exc)

    def google(self):
        from .google_hesap import GoogleHesabi
        return GoogleHesabi(tarayici=self.tarayici)

    def _degisti(self):
        """Kimlik veya mülk değişince eski istemci ve eski test sonucu kullanılmaz."""
        self._ga4 = None
        (data_root() / BAGLANTI_DOSYASI).unlink(missing_ok=True)

    def baglanti_ozeti(self) -> dict:
        try:
            ayar = ayarlar.load()
        except SettingsError:
            ayar = None
        kimlik = (ayar or {}).get("kimlik") or {}
        yontem = kimlik.get("tur")
        g = self.google().durum()
        hesap, hazir = None, False
        if yontem == "google_hesabi":
            hesap, hazir = g["eposta"], g["bagli"]
        elif yontem == "hizmet_hesabi":
            try:
                from .kimlik import anahtar_dosyasi_oku, eposta_goster
                hesap, hazir = eposta_goster(anahtar_dosyasi_oku(kimlik["anahtar_dosyasi"])["eposta"]), True
            except ClientError:
                hesap = None
        propertyler = list(ayar["propertyler"]) if ayar else []
        return {"yontem": yontem, "kimlik_hazir": hazir, "kurulu": bool(hazir and propertyler), "hesap": hesap, "google": g,
                "propertyler": propertyler, "hizmet_hesabi": hesap if yontem == "hizmet_hesabi" else None,
                "son_test": _oku_json(data_root() / BAGLANTI_DOSYASI)}

    def son_calisma(self):
        o = self.son_ozet
        if not o:
            return None
        return {"zaman": o["zaman"], "kaynak": o["kaynak"], "tamam": o["tamam"], "uyari": o["uyari"], "hata": o["hata"],
                "is_sayisi": len(o["sonuclar"]), "proje": o.get("proje")}

    @staticmethod
    def proje_ozeti(p: dict) -> dict:
        try:
            jobs = store.load(p["id"])
        except isler.JobError:
            jobs = []
        return {**p, "is_sayisi": len(jobs), "etkin_is": sum(j["etkin"] for j in jobs),
                "sheets_is": sum(1 for j in jobs if j.get("sheets") is not None)}  # etkin olup olmadığından bağımsız

    # ---------- uç noktalar ----------

    def handle(self, method: str, route: str, body: dict, query: dict):
        kaynak = (body.get("kaynak") if method == "POST" else (query.get("kaynak") or ["sentetik"])[0]) or "sentetik"
        if kaynak not in ("sentetik", "ga4"):
            raise ClientError("KAYNAK", "Kaynak 'sentetik' veya 'ga4' olmalı.")

        if (method, route) == ("GET", "/api/ping"):
            return {"ok": True}
        istenen_proje = (body.get("proje") if method == "POST" else (query.get("proje") or [None])[0]) or None
        pid = istenen_proje if istenen_proje and any(p["id"] == istenen_proje for p in projeler.listele()) else projeler.varsayilan()

        if (method, route) == ("GET", "/api/durum"):
            jobs = store.load(pid)
            return {"surum": VERSION, "veri_klasoru": str(data_root()), "is_dosyasi": str(store.path(pid)),
                    "proje": self.proje_ozeti(projeler.oku(pid)), "projeler": [{"id": p["id"], "ad": p["ad"]} for p in projeler.listele()],
                    "is_sayisi": len(jobs), "etkin_is": sum(j["etkin"] for j in jobs),
                    "baglanti": self.baglanti_ozeti(), "son_calisma": self.son_calisma()}

        # ---------- projeler ----------
        if (method, route) == ("GET", "/api/projeler"):
            return {"projeler": [self.proje_ozeti(p) for p in projeler.listele()]}
        if (method, route) == ("POST", "/api/projeler/olustur"):
            return {"proje": self.proje_ozeti(projeler.olustur(body.get("ad"), body.get("aciklama")))}
        if (method, route) == ("POST", "/api/projeler/guncelle"):
            degisiklik = {k: body[k] for k in ("ad", "aciklama", "sheets") if k in body}
            return {"proje": self.proje_ozeti(projeler.guncelle(pid, degisiklik))}
        if (method, route) == ("POST", "/api/projeler/sil"):
            if not istenen_proje:
                raise projeler.ProjeHatasi("Silinecek proje seçilmedi.")
            projeler.sil(istenen_proje)
            if self.son_ozet and self.son_ozet.get("proje") == istenen_proje:
                self.son_ozet = None
            return {"projeler": [self.proje_ozeti(p) for p in projeler.listele()], "varsayilan": projeler.varsayilan()}

        if (method, route) == ("GET", "/api/isler"):
            return {"isler": store.load(pid), "paketler": paketler.ozet(pid)}
        if (method, route) == ("POST", "/api/isler/kaydet"):
            jobs = store.upsert(body.get("is") or {}, body.get("eski_ad"), proje=pid)
            return {"isler": jobs}
        if (method, route) == ("POST", "/api/isler/sil"):
            silinen = next((j for j in store.load(pid) if isler.ad_anahtari(j["ad"]) == isler.ad_anahtari(str(body.get("ad", "")))), None)
            jobs = store.delete(body.get("ad", ""), proje=pid)
            if silinen:
                paketler.is_silindi(pid, silinen)  # eşitleme bu raporu pakete geri getirmez
            return {"isler": jobs}
        if (method, route) == ("POST", "/api/isler/etkin"):
            istenen = {isler.ad_anahtari(str(a)) for a in (body.get("adlar") or [body.get("ad", "")])}
            jobs = store.load(pid)
            for j in jobs:
                if isler.ad_anahtari(j["ad"]) in istenen:
                    j["etkin"] = bool(body.get("etkin"))
            return {"isler": store.save(jobs, pid)}
        if (method, route) == ("POST", "/api/isler/ice-aktar"):
            sonuc = store.import_jobs(str(body.get("icerik", "")), proje=pid)
            return {**sonuc, "isler": store.load(pid)}
        if (method, route) == ("GET", "/api/isler/taslak"):
            return store.export_jobs(pid)
        if (method, route) == ("POST", "/api/isler/sablon"):
            adlar = body.get("adlar")
            if not isinstance(adlar, list) or not adlar:
                raise isler.JobError("Şablonun kaydedileceği iş seçilmedi.")
            duzen = str(body.get("duzen") or "")
            sablon = None
            if body.get("sablon") is not None:
                sablon = isler.normalize_sablon({duzen: body["sablon"]}, "Şablon").get(duzen) if duzen in isler.SABLON_DUZENLER else None
            return {"isler": store.sablon_kaydet([str(a) for a in adlar], duzen, sablon, pid)}
        if (method, route) == ("POST", "/api/isler/sheets"):
            ayar = body.get("sheets")
            if ayar is not None:
                ayar = isler.normalize_sheets(ayar, f"'{body.get('ad', '')}'")
            return {"isler": store.sheets_ayarla(str(body.get("ad", "")), ayar, pid)}

        if (method, route) == ("GET", "/api/meta"):
            ref = (query.get("property") or ["PROP_PROD"])[0]
            veri = meta.fetch(self.istemci(kaynak), ref, refresh=(query.get("yenile") or [""])[0] == "1")
            ogrenilmis = ad_sozlugu.ogrenilen()
            # GA4 Türkçe adları: rapor adı önde, Keşfet adı ve diğerleri arama için (metadata yalnız İngilizce ad verir)
            liste = lambda d: sorted(({"api": k, **v, **ad_sozlugu.bilgi(k, v.get("ad"), ozel=bool(v.get("ozel")), ogrenilmis=ogrenilmis)}
                                      for k, v in d.items()), key=lambda x: (x.get("kategori") or "~", x["ad"].casefold()))
            return {"kaynak": kaynak, "property": ref, "metrikler": liste(veri["metrikler"]), "boyutlar": liste(veri["boyutlar"])}

        # ---------- hazır raporlar ve paketler ----------
        if (method, route) == ("GET", "/api/hazir-raporlar"):
            veri, meta_hatasi = self.meta_veya_yok(kaynak)
            return {"agac": paketler.agac(veri, store.load(pid)), "paketler": paketler.ozet(pid), "meta_hatasi": meta_hatasi}
        if (method, route) == ("POST", "/api/paketler/cek"):
            secim = body.get("secim")
            if not isinstance(secim, list) or not secim or not all(isinstance(s, dict) for s in secim):
                raise isler.JobError("Çekilecek rapor grubu seçilmedi.")
            veri, _ = self.meta_veya_yok(kaynak)
            sonuc = paketler.cek(pid, secim, tarih=body.get("tarih"), etkin=bool(body.get("etkin", True)),
                                 eksik_onay=bool(body.get("eksik_onay")), meta=veri)
            return {**sonuc, "isler": store.load(pid), "paketler": paketler.ozet(pid)}
        if (method, route) == ("POST", "/api/paketler/esitle"):
            if body.get('cached_only'):
                # Gezinti sırasında Google'a gidilmez; süresi dolan 24 saatlik metadata
                # pazartesi açılışında uzun ağ denemelerine neden olabiliyordu.
                cached = _oku_json(data_root() / 'meta' / 'PROP_PROD.json') or {}
                veri = cached.get('meta') if meta.gecerli_mi(cached.get('meta')) else None
                if kaynak == 'ga4' and veri is None:
                    return {'isler': store.load(pid), 'paketler': paketler.ozet(pid),
                            'eklenen': [], 'isaretlenen': [], 'onay_bekleyen': []}
            else:
                veri, _ = self.meta_veya_yok(kaynak)
            return {**paketler.esitle(pid, veri), "isler": store.load(pid), "paketler": paketler.ozet(pid)}
        if (method, route) == ("POST", "/api/paketler/sil"):
            sonuc = paketler.paket_sil(pid, str(body.get("kimlik", "")))
            return {**sonuc, "isler": store.load(pid), "paketler": paketler.ozet(pid)}

        if (method, route) == ("POST", "/api/dogrula"):
            try:
                job = isler.normalize_job(body.get("is") or {})
            except isler.JobError as exc:
                return {"hatalar": [str(exc)]}
            try:
                client = self.istemci(kaynak)
                hatalar = meta.validate(job, client, meta.fetch(client, job["property_ref"]))
            except (ClientError, SettingsError) as exc:
                return {"hatalar": [str(exc)], "baglanti_hatasi": True}
            return {"hatalar": hatalar}

        if (method, route) == ("POST", "/api/calistir"):
            jobs = store.load(pid)
            adlar = body.get("adlar")
            if adlar:
                istenen = {isler.ad_anahtari(a) for a in adlar}
                secili = [j for j in jobs if isler.ad_anahtari(j["ad"]) in istenen]
            else:
                secili = [j for j in jobs if j["etkin"]]
            if not secili:
                raise isler.JobError("Çalıştırılacak iş yok: iş seç ya da bir işi etkinleştir.")
            basla = time.monotonic()
            ozet = rapor.run_jobs(secili, self.istemci(kaynak))
            ozet["sure_ms"] = round((time.monotonic() - basla) * 1000)
            ozet["proje"] = pid
            self.son_ozet = ozet
            return ozet

        if (method, route) == ("GET", "/api/csv"):
            if not self.son_ozet:
                raise ClientError("SONUC_YOK", "Önce bir çalıştırma yap.")
            tampon = io.StringIO()
            yazici = csv.DictWriter(tampon, cikti.COLUMNS, delimiter=";")
            yazici.writeheader()
            yazici.writerows(cikti.rows(self.son_ozet))
            ad = f"ga4-sonuc-{self.son_ozet['kaynak']}-{datetime.now():%Y-%m-%d-%H%M}.csv"
            return {"ad": ad, "icerik": "﻿" + tampon.getvalue()}

        if (method, route) in (("POST", "/api/disari-aktar"), ("POST", "/api/disari-aktar/onizleme")):
            if not self.son_ozet:
                raise ClientError("SONUC_YOK", "Önce bir çalıştırma yap.")
            adlar = body.get("adlar")
            # Sütun şablonları: işlerde kayıtlı olan + önizlemede henüz kaydedilmemiş düzenlemeler (istek önce gelir)
            sonuc_projesi = self.son_ozet.get("proje") or pid
            try:
                sablonlar = {j["ad"]: j.get("sablon") for j in store.load(sonuc_projesi)}
            except (isler.JobError, projeler.ProjeHatasi):
                sablonlar = {}
            for ad, sablon in (body.get("sablonlar") or {}).items() if isinstance(body.get("sablonlar"), dict) else []:
                sablonlar[str(ad)] = isler.normalize_sablon(sablon, f"'{ad}'") if sablon else None
            sayfalar = disari.sayfalar(self.son_ozet, duzen=str(body.get("duzen") or body.get("bicim") or "tablo"),
                                       bolme=str(body.get("bolme", "tek")), toplam=body.get("toplam", True) is True,
                                       adlar=adlar if isinstance(adlar, list) else None,
                                       etiketler=body.get("etiketler") if isinstance(body.get("etiketler"), dict) else None,
                                       sablonlar=sablonlar)
            if route.endswith("/onizleme"):
                return disari.onizleme(sayfalar)
            hedef = str(body.get("hedef") or "xlsx")
            kaynak = self.son_ozet["kaynak"]
            proje_adi = projeler.oku(self.son_ozet.get("proje") or pid)["ad"] if (self.son_ozet.get("proje") or pid) else ""
            on_ek = f"ga4-{kaynak}-{disari._dosya_adi(proje_adi)}-{datetime.now():%Y-%m-%d-%H%M}"
            if hedef == "xlsx":
                return disari.xlsx_dosyasi(sayfalar, on_ek)
            if hedef == "csv":
                return disari.csv_dosyasi(sayfalar, on_ek)
            if hedef == "gsheets":
                if kaynak != "ga4":
                    raise disari.ExportError("Sentetik sayılar Google Sheets'e gönderilmez; Excel veya CSV seç.")
                baslik = f"Analytics dışa aktarma — {proje_adi} — {datetime.now():%Y-%m-%d %H:%M}"
                return {"tur": "gsheets", **zamanli.sheets_istemcisi().sayfali_tablo(baslik, sayfalar)}
            raise disari.ExportError("Hedef 'xlsx', 'csv' ya da 'gsheets' olmalı.")

        # ---------- Google Sheets ve zamanlama ----------
        if (method, route) == ("GET", "/api/sheets/bilgi"):
            tablo = isler.tablo_kimligi((query.get("tablo") or [""])[0])
            return zamanli.sheets_istemcisi().bilgi(tablo)
        if (method, route) == ("POST", "/api/sheets/olustur"):
            baslik = str(body.get("baslik") or "").strip() or f"Analytics Veri Çekme — {projeler.oku(pid)['ad']}"
            return zamanli.sheets_istemcisi().olustur(baslik)
        if (method, route) == ("POST", "/api/sheets/yaz"):
            adlar = body.get("adlar")
            istenen = {isler.ad_anahtari(a) for a in adlar} if isinstance(adlar, list) and adlar else None
            secili = [j for j in store.load(pid) if j.get("sheets") is not None and (isler.ad_anahtari(j["ad"]) in istenen if istenen else True)]
            if not secili:
                raise isler.JobError("Bu projede Google Sheets'e yazılan iş yok: Sheets ve zamanlama sayfasındaki listeden yazılacak işleri işaretle.")
            return zamanli.kayitli_calistir(pid, secili, tetikleyen="arayuz")
        if (method, route) == ("GET", "/api/zamanlama"):
            proje = projeler.oku(pid)
            isler_ = []
            for j in store.load(pid):  # projenin bütün işleri: sayfada hangilerinin yazılacağı işaretlenir
                if j.get("sheets") is not None:
                    try:
                        h = projeler.hedef(j, proje)
                    except ClientError:
                        h = {"tablo": None, "sekme": j["sheets"].get("sekme") or j["ad"], "guncelle_gun": None, "toplam": None}
                else:
                    h = {"tablo": None, "sekme": None, "guncelle_gun": None, "toplam": None}
                isler_.append({"ad": j["ad"], "etkin": j["etkin"], "yaz": j.get("sheets") is not None, "ayar": j.get("sheets"),
                               "aciklama": isler.describe(j), **h})
            return {"proje": self.proje_ozeti(proje), "ayar": proje["zamanlama"] or zamanlayici.VARSAYILAN_AYAR,
                    "gorev": zamanlayici.durum(pid), "son": zamanli.son_ozet(pid), "isler": isler_}
        if (method, route) == ("POST", "/api/zamanlama/kur"):
            ayar = zamanlayici.ayar_dogrula({"saat": body.get("saat"), "gunler": body.get("gunler")})
            proje = projeler.oku(pid)
            gorev = zamanlayici.kur(pid, ayar, proje_adi=proje["ad"])
            projeler.guncelle(pid, {"zamanlama": ayar})
            return {"gorev": gorev, "ayar": ayar}
        if (method, route) == ("POST", "/api/zamanlama/kaldir"):
            return {"gorev": zamanlayici.kaldir(pid)}
        if (method, route) == ("POST", "/api/zamanlama/calistir"):
            return {"gorev": zamanlayici.simdi_calistir(pid)}

        if (method, route) == ("POST", "/api/kurulum"):
            icerik = str(body.get("anahtar", ""))
            if not icerik or len(icerik) > MAX_ANAHTAR:
                raise SettingsError("Anahtar dosyası boş veya çok büyük.")
            klasor = data_root() / "anahtar"
            klasor.mkdir(parents=True, exist_ok=True)
            gecici = klasor / f".yukleme-{secrets.token_hex(6)}.json"
            try:
                gecici.write_text(icerik, encoding="utf-8")
                _, _, bilgi = ayarlar.kurulum(gecici, str(body.get("property_ref") or "PROP_PROD"), str(body.get("property_id", "")).strip())
            finally:
                gecici.unlink(missing_ok=True)
            self._degisti()
            from .kimlik import eposta_goster
            return {"hizmet_hesabi": eposta_goster(bilgi["eposta"]), "baglanti": self.baglanti_ozeti()}

        if (method, route) == ("POST", "/api/google/istemci"):
            icerik = str(body.get("icerik", ""))
            if not icerik or len(icerik) > MAX_ANAHTAR:
                raise SettingsError("İstemci dosyası boş veya çok büyük.")
            ayarlar.google_istemci_kaydet(icerik)
            self._degisti()
            return {"baglanti": self.baglanti_ozeti()}
        if (method, route) == ("POST", "/api/google/baglan"):
            if not self._giris_kilidi.acquire(blocking=False):
                # Yarım kalan giriş (sekme kapandı, görünmedi) yeni tıklamayı 3 dk bekletmesin: eskisi bırakılır.
                if self._iptal is not None:
                    self._iptal.set()
                if not self._giris_kilidi.acquire(timeout=5):
                    raise ClientError("MESGUL", "Önceki Google girişi kapanamadı; birkaç saniye sonra yeniden dene.")
            try:
                self._iptal = threading.Event()

                def ac(adres):
                    self._giris_adresi = adres
                    try:
                        self.tarayici(adres)
                    except Exception:
                        pass  # açılamazsa arayüzdeki bağlantıyla devam edilir
                    return True

                from .google_hesap import GoogleHesabi
                hesap = GoogleHesabi(tarayici=ac)
                _gunluk("google girişi başladı")
                try:
                    eposta = hesap.baglan(iptal=self._iptal)
                except ClientError as exc:
                    _gunluk(f"google girişi bitmedi: {exc.code}")
                    raise
                _gunluk("google girişi tamamlandı")
            finally:
                self._iptal = self._giris_adresi = None
                self._giris_kilidi.release()
            if (ayarlar._mevcut().get("kimlik") or {}).get("tur") != "google_hesabi":
                ayarlar.kimlik_ayarla({"tur": "google_hesabi", "istemci_dosyasi": str(hesap.istemci_yolu)})
            self._degisti()
            return {"eposta": eposta, "baglanti": self.baglanti_ozeti()}
        if (method, route) == ("GET", "/api/google/adres"):
            return {"adres": self._giris_adresi}
        if (method, route) == ("POST", "/api/google/iptal"):
            if self._iptal is not None:
                self._iptal.set()
            return {"ok": True}
        if (method, route) == ("POST", "/api/google/cikis"):
            self.google().cikis()
            self._degisti()
            return {"baglanti": self.baglanti_ozeti()}
        if (method, route) == ("GET", "/api/google/mulkler"):
            from .ga4 import RestClient
            try:
                return {"mulkler": RestClient(ayarlar.load()).mulkler()}
            except ClientError as exc:
                if exc.code == "SERVICE_DISABLED":
                    raise ClientError("SERVICE_DISABLED", "Mülk listesi için Google Cloud'da \"Google Analytics Admin API\"yi etkinleştir "
                                                          "ya da mülk kimliğini elle gir.") from None
                raise
        if (method, route) == ("POST", "/api/mulk"):
            ayarlar.mulk_kaydet(str(body.get("property_ref") or "PROP_PROD").strip().upper(), str(body.get("property_id", "")).strip(),
                                str(body["aciklama"]).strip() if body.get("aciklama") else None)
            self._degisti()
            return {"baglanti": self.baglanti_ozeti()}

        if (method, route) == ("POST", "/api/baglanti"):
            return self.baglanti_testi()

        raise ClientError("BULUNAMADI", "Bilinmeyen istek.")

    def baglanti_testi(self) -> dict:
        """Veri çekmeden: ayar → anahtar → belirteç → her property'nin alan listesi."""
        adimlar = []
        bitir = lambda: self._test_kaydet(adimlar)
        try:
            ayar = ayarlar.load()
            adimlar.append({"ad": "Ayar dosyası", "ok": True, "mesaj": str(ayarlar.settings_path())})
        except SettingsError as exc:
            adimlar.append({"ad": "Ayar dosyası", "ok": False, "mesaj": str(exc).splitlines()[0]})
            return bitir()
        kimlik = ayar.get("kimlik")
        if not kimlik:
            adimlar.append({"ad": "Giriş yöntemi", "ok": False, "mesaj": "Google hesabı bağlanmadı."})
            return bitir()
        from .ga4 import RestClient
        self._ga4 = client = RestClient(ayar)
        etiket = "Google hesabı" if kimlik["tur"] == "google_hesabi" else "Hizmet hesabı"
        try:
            client.token_provider()
            adimlar.append({"ad": etiket, "ok": True, "mesaj": client.token_provider.__self__.bilgi()["eposta"]})
            adimlar.append({"ad": "Google girişi", "ok": True, "mesaj": "Erişim belirteci alındı (yalnız GA4 okuma)."})
        except (ClientError, PathError) as exc:
            adimlar.append({"ad": "Google girişi", "ok": False, "mesaj": str(exc)})
            return bitir()
        if not ayar["propertyler"]:
            adimlar.append({"ad": "Mülk", "ok": False, "mesaj": "Henüz mülk seçilmedi."})
            return bitir()
        for ref in ayar["propertyler"]:
            try:
                veri = client.metadata(ref)
                b, m = veri.get("dimensions") or [], veri.get("metrics") or []
                ozel = sum(1 for x in b + m if x.get("customDefinition"))
                adimlar.append({"ad": f"Mülk {ref}", "ok": True, "mesaj": f"Erişim var — {len(b)} boyut, {len(m)} metrik ({ozel} özel tanım)."})
            except ClientError as exc:
                adimlar.append({"ad": f"Mülk {ref}", "ok": False, "mesaj": str(exc)})
        return bitir()

    def _test_kaydet(self, adimlar: list) -> dict:
        sonuc = {"zaman": datetime.now().astimezone().isoformat(timespec="seconds"), "basarili": all(a["ok"] for a in adimlar)}
        try:
            _yaz_json(data_root() / BAGLANTI_DOSYASI, sonuc)
        except OSError:
            pass
        return {**sonuc, "adimlar": adimlar, "baglanti": self.baglanti_ozeti()}

    # ---------- HTTP ----------

    def make_handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code: int, payload, content_type="application/json; charset=utf-8"):
                data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                if content_type.startswith("text/html"):
                    self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; "
                                     "script-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
                self.send_header("Content-Length", str(len(data)))
                try:
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def _dispatch(self, method: str):
                if self.headers.get("Host") not in {f"127.0.0.1:{app.port}", f"localhost:{app.port}"}:
                    return self._send(403, {"error": "Yalnız yerel erişim."})
                url = urlparse(self.path)
                if method == "GET" and url.path in ("/", "/index.html"):
                    if not secrets.compare_digest(parse_qs(url.query).get("t", [""])[0], app.token):
                        return self._send(403, "Bu adresi 'python -m analytics_cekme arayuz' ile açın.".encode(), "text/plain; charset=utf-8")
                    html = UI_FILE.read_text(encoding="utf-8").replace("__TOKEN__", app.token).replace("__SURUM__", VERSION)
                    return self._send(200, html.encode(), "text/html; charset=utf-8")
                if not url.path.startswith("/api/"):
                    return self._send(404, {"error": "Bulunamadı"})
                if not secrets.compare_digest(self.headers.get("X-Token", ""), app.token):
                    return self._send(403, {"error": "Oturum anahtarı geçersiz; arayüzü yeniden açın."})
                body = {}
                if method == "POST":
                    if not self.headers.get("Content-Type", "").startswith("application/json"):
                        return self._send(415, {"error": "JSON bekleniyor."})
                    try:
                        length = int(self.headers.get("Content-Length") or 0)
                    except ValueError:
                        return self._send(400, {"error": "Geçersiz istek uzunluğu."})
                    if length < 0 or self.headers.get("Transfer-Encoding"):
                        return self._send(400, {"error": "Geçersiz istek uzunluğu veya aktarım biçimi."})
                    if length > MAX_GOVDE:
                        return self._send(413, {"error": "İstek çok büyük."})
                    try:
                        body = json.loads(self.rfile.read(length) or b"{}")
                    except ValueError:
                        return self._send(400, {"error": "Geçersiz JSON."})
                    if not isinstance(body, dict):
                        return self._send(400, {"error": "Geçersiz istek."})
                try:
                    return self._send(200, app.handle(method, url.path, body, parse_qs(url.query)))
                except USER_ERRORS as exc:
                    return self._send(400, {"error": str(exc), "kod": getattr(exc, "code", None)})
                except Exception as exc:  # arayüz boş kalmasın, sebep görünsün
                    tb = exc.__traceback__
                    while tb and tb.tb_next:
                        tb = tb.tb_next
                    _gunluk(f"beklenmeyen hata {type(exc).__name__} {url.path} "
                            f"{Path(tb.tb_frame.f_code.co_filename).name if tb else '?'}:{tb.tb_lineno if tb else '?'}")
                    return self._send(500, {"error": f"Beklenmeyen hata: {type(exc).__name__}"})

            def do_GET(self):
                self._dispatch("GET")

            def do_POST(self):
                self._dispatch("POST")

        return Handler


def _free_port(preferred: int = 8790) -> int:
    for port in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise OSError("Boş port bulunamadı.")


def _existing_instance() -> str | None:
    info = _oku_json(data_root() / "arayuz.json")
    if not isinstance(info, dict):
        return None
    try:
        istek = urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/ping", headers={"X-Token": info["token"]})
        with urllib.request.urlopen(istek, timeout=1.5) as yanit:
            if yanit.status == 200:
                return f"http://127.0.0.1:{info['port']}/?t={info['token']}"
    except (OSError, KeyError, ValueError):
        return None
    return None


def serve(open_browser: bool = True, port: int | None = None) -> int:
    url = _existing_instance()
    if url:
        print("Arayüz zaten açık; tarayıcıda gösteriliyor.")
        if open_browser:
            webbrowser.open(url)
        return 0
    app = App(port or _free_port())
    httpd = ThreadingHTTPServer(("127.0.0.1", app.port), app.make_handler())
    url = f"http://127.0.0.1:{app.port}/?t={app.token}"
    _yaz_json(data_root() / "arayuz.json", {"port": app.port, "token": app.token, "pid": os.getpid()})
    print(f"Analytics Veri Çekme {VERSION} arayüzü açık: {url}")
    print("Bu pencereyi kapatırsanız arayüz kapanır.")
    _gunluk(f"arayüz açıldı {VERSION} port {app.port}")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _gunluk("arayüz kapandı (Ctrl+C)")  # pencere X ile kapatılırsa bu satır yazılamaz
        httpd.server_close()
        (data_root() / "arayuz.json").unlink(missing_ok=True)
    return 0
