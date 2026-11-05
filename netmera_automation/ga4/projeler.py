"""GA4 bölümünün projesi = Netmera projesi (birleşik uygulama, kullanıcı kararı 2026-10-28).

Analytics Veri Çekme'deki proje alanları Netmera profilinde `ga4` anahtarında durur: açıklama, GA4 Sheets
ayarları (projenin tablosu, son N gün, toplam satırı) ve çekilmiş hazır rapor paketleri. Proje adı Netmera
projesinin adıdır. Zamanlama Netmera'nın Windows görevidir (GA4 Sheets yazımı aynı çalışmada yapılır);
bu yüzden buradaki `zamanlama` her zaman None'dır. Fonksiyon adları ayrı uygulamadakiyle aynıdır, böylece
paketler, zamanli, disari ve sunucu uçları değişmeden çalışır.
"""
from __future__ import annotations

from pathlib import Path
import re

from .isler import JobError, TABLO_ID, tablo_kimligi
from .istemci import ClientError

KIMLIK = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
PAKET_KEYS = {"kimlik", "ad", "kaynak", "tarih", "cekildi", "haric", "surum"}
SHEETS_KEYS = {"tablo", "guncelle_gun", "toplam"}
VARSAYILAN_SHEETS = {"tablo": None, "guncelle_gun": 3, "toplam": True}
VARSAYILAN_AD = "Genel"


class ProjeHatasi(ValueError):
    """Proje bulunamadı ya da ayarı geçersiz."""


def _netmera():
    from .. import store
    return store


def kok() -> Path:
    return _netmera().profiles_root()


def klasor(pid: str) -> Path:
    """Projenin GA4 çalışma klasörü (son Sheets yazımının özeti, yazım kilidi): <profil>\\ga4\\."""
    if not isinstance(pid, str) or not KIMLIK.match(pid):
        raise ProjeHatasi("Proje kimliği geçersiz.")
    folder = _netmera().profile_dir(pid)
    if not (folder / "profil.json").is_file():
        raise ProjeHatasi("Proje bulunamadı; sayfayı yenile (silinmiş olabilir).")
    (folder / "ga4").mkdir(exist_ok=True)
    return folder / "ga4"


def _sheets_dogrula(raw) -> dict:
    if raw is None:
        return dict(VARSAYILAN_SHEETS)
    if not isinstance(raw, dict) or not set(raw) <= SHEETS_KEYS:
        raise ProjeHatasi("Proje Sheets ayarı yalnız tablo, guncelle_gun ve toplam içerebilir.")
    tablo = raw.get("tablo")
    if tablo in (None, ""):
        tablo = None
    else:
        try:
            tablo = tablo_kimligi(tablo)
        except JobError as exc:
            raise ProjeHatasi(str(exc)) from None
    gun = raw.get("guncelle_gun", 3)
    if not isinstance(gun, int) or isinstance(gun, bool) or not 0 <= gun <= 31:
        raise ProjeHatasi("Son kaç günün güncelleneceği 0–31 arası tam sayı olmalı.")
    toplam = raw.get("toplam", True)
    if not isinstance(toplam, bool):
        raise ProjeHatasi("Toplam satırı ayarı true/false olmalı.")
    return {"tablo": tablo, "guncelle_gun": gun, "toplam": toplam}


def _paketler_dogrula(raw) -> list[dict]:
    """Projede çekilmiş hazır rapor paketleri: kimlik, ad, kaynak (kutuphane/tarama), ortak tarih, haric (silinen raporlar)."""
    from .isler import PAKET_KIMLIK, normalize_period
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 100:
        raise ProjeHatasi("Proje paket listesi bozuk.")
    out, gorulen = [], set()
    for p in raw:
        if not isinstance(p, dict) or not set(p) <= PAKET_KEYS or not PAKET_KIMLIK.match(str(p.get("kimlik", ""))) or p["kimlik"] in gorulen:
            raise ProjeHatasi("Proje paket kaydı bozuk.")
        gorulen.add(p["kimlik"])
        haric = p.get("haric") or []
        if not isinstance(haric, list) or not all(isinstance(h, str) and len(h) <= 200 for h in haric):
            raise ProjeHatasi("Proje paket kaydı bozuk (haric).")
        try:
            tarih = normalize_period(p["tarih"], "paket tarihi") if p.get("tarih") else None
        except JobError as exc:
            raise ProjeHatasi(str(exc)) from None
        out.append({"kimlik": p["kimlik"], "ad": str(p.get("ad") or p["kimlik"])[:120],
                    "kaynak": "tarama" if p["kimlik"].startswith("tarama:") else "kutuphane", "tarih": tarih,
                    "cekildi": str(p.get("cekildi") or "")[:40], "haric": sorted(set(haric)), "surum": int(p.get("surum") or 0)})
    return out


def ga4_ayari(raw) -> dict:
    """Netmera profilindeki `ga4` anahtarı: {aciklama, sheets, paketler}. Netmera profil doğrulaması çağırır."""
    raw = raw or {}
    if not isinstance(raw, dict) or not set(raw) <= {"aciklama", "sheets", "paketler"}:
        raise ProjeHatasi("Projenin GA4 ayarı bozuk.")
    aciklama = raw.get("aciklama")
    return {"aciklama": str(aciklama).strip()[:300] if aciklama else "", "sheets": _sheets_dogrula(raw.get("sheets")),
            "paketler": _paketler_dogrula(raw.get("paketler"))}


def _proje(profile: dict) -> dict:
    ayar = ga4_ayari(profile.get("ga4"))
    return {"id": profile["id"], "ad": profile["name"], "aciklama": ayar["aciklama"], "sheets": ayar["sheets"],
            "zamanlama": None, "olusturuldu": "", "paketler": ayar["paketler"]}


def _yukle(pid: str) -> dict:
    if not isinstance(pid, str) or not KIMLIK.match(pid):
        raise ProjeHatasi("Proje kimliği geçersiz.")
    from ..jobs import JobError as NetmeraJobError
    try:
        return _netmera().load_profile(pid)
    except NetmeraJobError as exc:
        raise ProjeHatasi(str(exc) if "bulunamadı" not in str(exc) else "Proje bulunamadı; sayfayı yenile (silinmiş olabilir).") from None


def oku(pid: str) -> dict:
    return _proje(_yukle(pid))


def listele() -> list[dict]:
    return [_proje(p) for p in _netmera().list_profiles()]


def varsayilan() -> str:
    projeler = listele()
    if not projeler:
        _netmera().ensure_defaults()
        projeler = listele()
    return projeler[0]["id"]


def olustur(ad: str, aciklama: str | None = None) -> dict:
    ad = str(ad or "").strip()
    if not ad or len(ad) > 60:
        raise ProjeHatasi("Proje adı 1–60 karakter olmalı.")
    if any(p["ad"].casefold() == ad.casefold() for p in listele()):
        raise ProjeHatasi(f"'{ad}' adlı bir proje zaten var.")
    profile = _netmera().create_profile(ad)
    if aciklama:
        return guncelle(profile["id"], {"aciklama": aciklama})
    return _proje(profile)


def guncelle(pid: str, degisiklik: dict) -> dict:
    """Ad Netmera projesinin adıdır; açıklama, Sheets ve paketler profilin `ga4` anahtarına yazılır. Zamanlama yok sayılır
    (birleşik uygulamada zamanlama Netmera'nın Zamanlama sayfasındadır)."""
    profile = _yukle(pid)
    ayar = ga4_ayari(profile.get("ga4"))
    if "ad" in degisiklik:
        yeni = str(degisiklik["ad"] or "").strip()
        if not yeni or len(yeni) > 60:
            raise ProjeHatasi("Proje adı 1–60 karakter olmalı.")
        if any(p["id"] != pid and p["ad"].casefold() == yeni.casefold() for p in listele()):
            raise ProjeHatasi(f"'{yeni}' adlı bir proje zaten var.")
        profile["name"] = yeni
    if "aciklama" in degisiklik:
        ayar["aciklama"] = str(degisiklik["aciklama"] or "").strip()[:300]
    if "sheets" in degisiklik:
        ayar["sheets"] = _sheets_dogrula({**ayar["sheets"], **(degisiklik["sheets"] or {})})
    if "paketler" in degisiklik:
        ayar["paketler"] = _paketler_dogrula(degisiklik["paketler"])
    profile["ga4"] = ayar
    from ..jobs import JobError as NetmeraJobError
    try:
        return _proje(_netmera().save_profile(profile))
    except NetmeraJobError as exc:
        raise ProjeHatasi(str(exc)) from None


def sil(pid: str, *, gorev_kaldir=None) -> None:
    """Netmera projesini siler (işler, ayarlar, Windows görevi). Google tablosuna dokunulmaz. Son proje silinmez."""
    profile = _yukle(pid)
    if len(listele()) <= 1:
        raise ProjeHatasi("En az bir proje kalmalı; önce yeni bir proje oluştur.")
    from .. import scheduler
    (gorev_kaldir or scheduler.remove)(profile)
    _netmera().delete_profile(pid)


def hedef(job: dict, proje: dict) -> dict | None:
    """İşin Sheets hedefini projenin varsayılanlarıyla tamamlar. İşte Sheets kapalıysa None."""
    ayar = job.get("sheets")
    if ayar is None:  # {} = Sheets açık, tüm ayarlar projeden
        return None
    varsayilan = (proje or {}).get("sheets") or VARSAYILAN_SHEETS
    tablo = ayar.get("tablo") or varsayilan.get("tablo")
    if not tablo or not TABLO_ID.match(tablo):
        raise ClientError("SHEETS_TABLO_YOK", "Projenin Google tablosu seçilmedi: Sheets → GA4 Google Sheets ayarları'ndan tablo seç.")
    return {"tablo": tablo, "sekme": ayar.get("sekme") or job["ad"][:100],
            "guncelle_gun": ayar["guncelle_gun"] if ayar.get("guncelle_gun") is not None else varsayilan.get("guncelle_gun", 3),
            "toplam": ayar["toplam"] if ayar.get("toplam") is not None else varsayilan.get("toplam", True)}
