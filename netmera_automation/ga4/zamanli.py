"""Sheets'e yazım çalışması: bir projenin Google Sheets hedefi olan işlerini GA4'ten çekip projenin tablosuna yazar.

Windows görevi (`python -m analytics_cekme zamanli --proje <kimlik>`) ve arayüzdeki "Şimdi yaz" aynı yolu kullanır.
Projede aynı anda tek yazım çalışır (kilit dosyası). Son çalışmanın özeti projenin klasörüne yazılır: iş adı, durum,
kaç satır güncellendi/eklendi. Metrik değeri, belirteç veya property kimliği özete girmez. Sentetik sayılar Sheets'e gitmez.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import os
import time

from . import ayarlar, projeler, rapor, sheets
from .disari import aralik_etiketi, kayitlar
from .istemci import ClientError

KILIT = "zamanli.kilit"
SON = "zamanli-son.json"
KILIT_OMRU = 3600  # sn: çöken çalışmanın bıraktığı kilit bu süreden sonra yok sayılır


def yazim_isi(job: dict, guncelle_gun: int) -> dict:
    """Sheets'e yazarken çekilecek sorgu: son N gün yeniden çekilir ki dünün geçici sayısı birkaç gün içinde kesinleşsin.

    "Dün" (zaman kırılımsız ya da günlük) → son N gün gün gün. Günlük kırılımlı son K gün (K < N) → son N gün.
    Ay/hafta/yıl kırılımı ve çok günlük toplamlar olduğu gibi çekilir; dönem anahtarı aynı kaldığı için satır yine güncellenir.
    """
    zaman, tarih = job.get("zaman"), job["tarih"]
    if guncelle_gun > 1 and tarih["tur"] == "dun" and zaman in (None, "gun"):
        return {**job, "zaman": "gun", "tarih": {"tur": "son_n_gun", "n": guncelle_gun}}
    if zaman == "gun" and tarih["tur"] == "son_n_gun" and tarih["n"] < guncelle_gun:
        return {**job, "tarih": {"tur": "son_n_gun", "n": guncelle_gun}}
    return job


@contextmanager
def kilit(klasor, bekle: float = 0, uyku=time.sleep):
    """Projede tek yazım. `bekle` sn kadar sırada bekler (zamanlanmış görevler aynı anda başlarsa)."""
    yol = klasor / KILIT
    yol.parent.mkdir(parents=True, exist_ok=True)
    son = time.monotonic() + bekle
    while True:
        try:
            os.close(os.open(yol, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            try:
                eski = time.time() - yol.stat().st_mtime > KILIT_OMRU
            except OSError:
                eski = True
            if eski:
                yol.unlink(missing_ok=True)
                continue
            if time.monotonic() >= son:
                raise ClientError("MESGUL", "Bu projede başka bir Sheets yazımı sürüyor (zamanlanmış görev ya da arayüz). "
                                            "Biraz sonra yeniden dene.") from None
            uyku(2)
    try:
        yield
    finally:
        yol.unlink(missing_ok=True)


def calistir(jobs: list[dict], client, sheets_istemci, *, proje: dict | None = None, now: datetime | None = None) -> dict:
    if client.kaynak != "ga4":
        raise ClientError("SENTETIK", "Sentetik sayılar Google Sheets'e yazılmaz; yalnız GA4 sonuçları yazılır.")
    now = now or datetime.now().astimezone()
    today, damga = now.date(), now.isoformat(timespec="minutes")
    sonuclar = []
    for job in jobs:
        if job.get("sheets") is None:
            continue
        kayit = {"is": job["ad"], "tablo": None, "sekme": job["sheets"].get("sekme") or job["ad"], "url": None,
                 "uyarilar": [], "mesaj": None, "guncellenen": 0, "eklenen": 0}
        try:
            hedef = projeler.hedef(job, proje or {})
        except ClientError as exc:
            sonuclar.append({**kayit, "durum": "hata", "mesaj": str(exc)})
            continue
        kayit.update(tablo=hedef["tablo"], sekme=hedef["sekme"], url=sheets.tablo_url(hedef["tablo"]))
        sorgu = yazim_isi(job, hedef["guncelle_gun"])
        r = rapor.run_job(sorgu, client, today=today)
        if r["durum"] == "hata":
            sonuclar.append({**kayit, "durum": "hata", "mesaj": r["hata"]})
            continue
        uyarilar = list(r["uyarilar"])
        toplamlar = {}
        if hedef["toplam"] and sorgu["boyutlar"]:
            if sorgu.get("zaman"):
                # GA4 toplamı tüm aralık içindir; her dönemin toplamı kırılımsız ayrı sorguyla alınır
                rt = rapor.run_job({**sorgu, "boyutlar": [], "kayit_yoksa_sifir": False}, client, today=today)
                if rt["durum"] == "hata":
                    uyarilar.append("toplam_alinamadi")
                else:
                    toplamlar = {k["donem"]: k["metrikler"] for k in kayitlar(rt, toplam=False)}
            elif r["toplam"]:
                toplamlar = {aralik_etiketi(r): r["toplam"]}
        try:
            basliklar, anahtarlar, izinli = sheets.sutunlar(sorgu)  # işin sütun şablonu uygulanır
            yazildi = sheets.yaz(sheets_istemci, hedef["tablo"], hedef["sekme"], basliklar, anahtarlar,
                                 sheets.satirlar(sorgu, r, toplamlar, damga, basliklar), izinli)
            sonuclar.append({**kayit, **yazildi, "durum": "uyari" if uyarilar else "tamam", "uyarilar": uyarilar})
        except ClientError as exc:
            sonuclar.append({**kayit, "durum": "hata", "mesaj": str(exc)})
    return {"zaman": damga, "isler": sonuclar, **{d: sum(s["durum"] == d for s in sonuclar) for d in ("tamam", "uyari", "hata")}}


def google_hesabi(opener=None):
    """Sheets için Google hesabı girişi (Sheets izniyle). Hizmet hesabı Sheets'e yazmaz."""
    from .google_hesap import GoogleHesabi
    ayar = ayarlar.load()
    kimlik = ayar.get("kimlik") or {}
    if kimlik.get("tur") != "google_hesabi":
        raise ClientError("SHEETS_GIRIS", "Google Sheets için Bağlantı sayfasından Google hesabınla bağlan.")
    hesap = GoogleHesabi(kimlik["istemci_dosyasi"], opener=opener)
    if not hesap.sheets_izni():
        raise ClientError("SHEETS_ACCESS_TOKEN_SCOPE_INSUFFICIENT", sheets.MESAJLAR["ACCESS_TOKEN_SCOPE_INSUFFICIENT"])
    return ayar, hesap


def sheets_istemcisi(opener=None) -> sheets.SheetsIstemci:
    _, hesap = google_hesabi(opener)
    return sheets.SheetsIstemci(hesap.belirtec, opener=opener)


def hazir_istemciler(opener=None):
    """GA4 ve Sheets istemcileri aynı Google girişiyle."""
    from .ga4 import RestClient
    ayar, hesap = google_hesabi(opener)
    return RestClient(ayar, token_provider=hesap.belirtec, opener=opener), sheets.SheetsIstemci(hesap.belirtec, opener=opener)


def son_ozet(pid: str) -> dict | None:
    try:
        return json.loads((projeler.klasor(pid) / SON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def kayitli_calistir(pid: str, jobs: list[dict] | None = None, *, tetikleyen: str, istemciler=None, bekle: float = 0) -> dict:
    """Kilit altında çalıştırır ve özeti projeye kaydeder. Giriş/izin hatası da özete yazılır (zamanlanmış görevde görünür olsun)."""
    from . import store
    proje = projeler.oku(pid)
    klasor = projeler.klasor(pid)
    if jobs is None:
        jobs = [j for j in store.load(pid) if j.get("sheets") is not None]  # Sheets listesinde işaretli olanlar ("etkin"den bağımsız)
    with kilit(klasor, bekle=bekle):
        try:
            client, sheets_istemci = istemciler or hazir_istemciler()
            ozet = calistir(jobs, client, sheets_istemci, proje=proje)
        except (ClientError, ayarlar.SettingsError) as exc:
            mesaj = ("GA4 bağlantısı kurulmadı: Bağlantı sayfasından Google hesabınla bağlan ve mülk seç."
                     if isinstance(exc, ayarlar.SettingsError) else str(exc).splitlines()[0])
            ozet = {"zaman": datetime.now().astimezone().isoformat(timespec="minutes"), "isler": [], "tamam": 0, "uyari": 0, "hata": 1,
                    "genel_hata": mesaj}
        ozet.update(tetikleyen=tetikleyen, proje=pid)
        (klasor / SON).write_text(json.dumps(ozet, ensure_ascii=False, indent=1), encoding="utf-8")
        return ozet
