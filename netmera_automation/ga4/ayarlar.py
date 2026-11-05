"""Yerel GA4 ayarları: property takma adı → gerçek property kimliği ve kimlik doğrulama yöntemi.

Dosya `%LOCALAPPDATA%\\AnalyticsVeriCekme\\ayarlar.json` içindedir; iş dosyasına, koda ve OneDrive'a girmez.
Elle yazılmaz: arayüzün Bağlantı sayfası ya da `python -m analytics_cekme kurulum …` yazar.

Kimlik yöntemleri:
  google_hesabi  — kendi Google hesabınla giriş (OAuth masaüstü istemcisi; oturum DPAPI ile şifreli, ayrı dosyada)
  hizmet_hesabi  — Google Cloud hizmet hesabı JSON anahtarı
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil

from .isler import PROPERTY_REF
from .istemci import ClientError
from .paths import check_local_path, data_root

FILE_NAME = "ayarlar.json"
KEY_NAME = "ga4-hizmet-hesabi.json"
EXAMPLE = '{"kimlik": {"tur": "google_hesabi", "istemci_dosyasi": "<…\\\\google-istemci.json>"}, "propertyler": {"PROP_PROD": {"property_id": "123456789"}}}'
PROPERTY_ID = re.compile(r"^\d{1,20}$")
KIMLIK_ALANLARI = {"hizmet_hesabi": "anahtar_dosyasi", "google_hesabi": "istemci_dosyasi"}


class SettingsError(ValueError):
    """Ayar dosyası yok veya geçersiz."""


def settings_path() -> Path:
    return data_root() / FILE_NAME


def _dogrula(raw, path) -> dict:
    if not isinstance(raw, dict) or not set(raw) <= {"propertyler", "kimlik"} or not isinstance(raw.get("propertyler"), dict):
        raise SettingsError(f"Ayar dosyası 'propertyler' nesnesi ve isteğe bağlı 'kimlik' içermeli: {path}\nÖrnek: {EXAMPLE}")
    for ref, entry in raw["propertyler"].items():
        if not PROPERTY_REF.match(ref):
            raise SettingsError(f"Ayar dosyasında geçersiz takma ad '{ref}' (ör. PROP_PROD).")
        if not isinstance(entry, dict) or not set(entry) <= {"property_id", "aciklama"}:
            raise SettingsError(f"'{ref}' yalnız 'property_id' ve isteğe bağlı 'aciklama' içerebilir.")
        if not isinstance(entry.get("property_id"), str) or not PROPERTY_ID.match(entry["property_id"]):
            raise SettingsError(f"'{ref}' için property_id yalnız rakamlardan oluşan metin olmalı (GA4 → Yönetici → Mülk ayrıntıları).")
        if "aciklama" in entry and (not isinstance(entry["aciklama"], str) or len(entry["aciklama"]) > 200):
            raise SettingsError(f"'{ref}' açıklaması en çok 200 karakterlik metin olmalı.")
    kimlik = raw.get("kimlik")
    if kimlik is not None:
        alan = KIMLIK_ALANLARI.get(kimlik.get("tur")) if isinstance(kimlik, dict) else None
        if not alan or set(kimlik) != {"tur", alan} or not isinstance(kimlik[alan], str):
            raise SettingsError('\'kimlik\' şu biçimlerden biri olmalı: {"tur": "google_hesabi", "istemci_dosyasi": "<yol>"} '
                                'veya {"tur": "hizmet_hesabi", "anahtar_dosyasi": "<yol>"}')
    return raw


def load() -> dict:
    path = settings_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise SettingsError(f"GA4 ayar dosyası yok: {path}\nArayüzde Bağlantı sayfasından Google hesabını bağla "
                            "(ya da: python -m analytics_cekme kurulum --anahtar <hizmet hesabı JSON> --property-id <mülk kimliği>).") from None
    except (OSError, ValueError) as exc:
        raise SettingsError(f"GA4 ayar dosyası okunamadı ({type(exc).__name__}): {path}") from None
    return _dogrula(raw, path)


def _mevcut() -> dict:
    path = settings_path()
    try:
        return _dogrula(json.loads(path.read_text(encoding="utf-8-sig")), path)
    except FileNotFoundError:
        return {"propertyler": {}}
    except (OSError, ValueError, SettingsError):
        raise SettingsError(f"Mevcut ayar dosyası bozuk; elle düzelt ya da sil: {path}") from None


def _yaz(ayar: dict) -> Path:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_dogrula(ayar, path), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _mulk_dogrula(ref: str, pid: str):
    if not PROPERTY_REF.match(ref or ""):
        raise SettingsError("Takma ad büyük harf, rakam ve alt çizgiden oluşmalı (ör. PROP_PROD).")
    if not PROPERTY_ID.match(pid or ""):
        raise SettingsError("Mülk kimliği yalnız rakamlardan oluşur (GA4 → Yönetici → Mülk ayrıntıları; G- ile başlayan ölçüm kimliği değil).")


def mulk_kaydet(ref: str, pid: str, aciklama: str | None = None) -> Path:
    """Takma adı bir GA4 mülküne bağlar; diğer mülkler ve kimlik ayarı korunur."""
    _mulk_dogrula(ref, pid)
    ayar = _mevcut()
    ayar["propertyler"][ref] = {"property_id": pid, **({"aciklama": aciklama[:200]} if aciklama else {})}
    return _yaz(ayar)


def kimlik_ayarla(kimlik: dict | None) -> Path:
    ayar = _mevcut()
    if kimlik is None:
        ayar.pop("kimlik", None)
    else:
        ayar["kimlik"] = kimlik
    return _yaz(ayar)


def google_istemci_kaydet(icerik: str) -> Path:
    """OAuth masaüstü istemcisini kaydeder ve kimlik yöntemini Google hesabı yapar (mülkler korunur)."""
    from .google_hesap import istemci_kaydet
    hedef = istemci_kaydet(icerik)
    kimlik_ayarla({"tur": "google_hesabi", "istemci_dosyasi": str(hedef)})
    return hedef


def kurulum(anahtar_kaynagi, ref: str, pid: str) -> tuple[Path, Path, dict]:
    """Hizmet hesabı: anahtarı veri klasörüne kopyalar ve ayar dosyasını yazar. Var olan diğer mülk tanımları korunur.

    Dönüş: (ayar dosyası, kopyalanan anahtar, anahtar bilgisi). Asıl indirilen dosyayı kullanıcı siler.
    """
    from .kimlik import anahtar_dosyasi_oku  # kimlik → ayarlar döngüsel içe aktarımını önler

    _mulk_dogrula(ref, pid)
    kaynak = Path(anahtar_kaynagi)
    bilgi = anahtar_dosyasi_oku(kaynak)  # biçimi kopyalamadan önce doğrula
    ayar = _mevcut()
    hedef_klasor = data_root() / "anahtar"
    hedef_klasor.mkdir(parents=True, exist_ok=True)
    hedef = check_local_path(hedef_klasor / KEY_NAME)
    if kaynak.resolve() != hedef:
        shutil.copyfile(kaynak, hedef)
    ayar["propertyler"][ref] = {"property_id": pid}
    ayar["kimlik"] = {"tur": "hizmet_hesabi", "anahtar_dosyasi": str(hedef)}
    return _yaz(ayar), hedef, bilgi


def property_id(settings: dict, ref: str) -> str:
    entry = settings["propertyler"].get(ref)
    if entry is None:
        raise ClientError("AYAR_EKSIK", f"'{ref}' takma adı bir GA4 mülküne bağlı değil. Bağlantı sayfasından mülk seç ({settings_path()}).")
    return entry["property_id"]


def token_provider(settings: dict, opener=None):
    """Ayardaki kimlik yöntemine göre erişim belirteci üreten fonksiyon (`__self__` hesap nesnesidir)."""
    kimlik = settings.get("kimlik")
    if not kimlik:
        def missing() -> str:
            raise ClientError("KIMLIK_SECILMEDI")
        return missing
    if kimlik["tur"] == "google_hesabi":
        from .google_hesap import GoogleHesabi
        return GoogleHesabi(kimlik["istemci_dosyasi"], opener=opener).belirtec
    from .kimlik import HizmetHesabi
    return HizmetHesabi(kimlik["anahtar_dosyasi"], opener=opener).belirtec
