"""Tek Google ayarı: OAuth istemci JSON'u ve Google girişi GA4 ile Google Sheets için ortaktır (kullanıcı kararı 2026-10-28).

İstemci dosyası GA4 bölümünün dosyasıdır: <veri>\\ga4\\google-istemci.json (en son verilen JSON; varsayılan).
Ortak Google bağlantısı sayfasından seçilen JSON buraya yazılır. Giriş GA4 oturumudur (DPAPI): Analytics okuma +
Google Sheets yazma izinleri birlikte istenir; bu oturum Netmera'nın hücre eşlemeli Sheets yazımında da kullanılır.
Ekip istemcisi (config/google-ekip-istemcisi.json) varsa ve ortak dosya yoksa ortak dosya ondan oluşturulur.
"""
from __future__ import annotations

import json

from .ga4 import ayarlar as GA_SETTINGS, google_hesap as GH
from .ga4.istemci import ClientError
from .ga4.paths import data_root


def client_path():
    return data_root() / GH.ISTEMCI_DOSYASI


def shared_client() -> dict | None:
    """Ortak istemci {client_id, client_secret}; yoksa None. Bozuk dosya ClientError verir."""
    if not client_path().exists():
        return None
    return GH.GoogleHesabi().istemci()


def _use_google_account():
    """GA4 giriş yöntemi seçilmemişse ortak Google hesabı yapılır (hizmet hesabı seçiliyse dokunulmaz)."""
    try:
        current = (GA_SETTINGS._mevcut().get("kimlik") or {}).get("tur")
    except GA_SETTINGS.SettingsError:
        return
    if current is None:
        GA_SETTINGS.kimlik_ayarla({"tur": "google_hesabi", "istemci_dosyasi": str(client_path())})


def save(raw: dict) -> None:
    """Masaüstü OAuth istemcisini ortak dosyaya yazar (öncekinin yerine geçer)."""
    GH.istemci_kaydet(json.dumps(raw))
    _use_google_account()


def ensure_client(team: dict | None) -> None:
    """Ortak dosya yoksa ekip istemcisinden oluşturur."""
    if not client_path().exists() and team:
        save({"installed": {"client_id": team["client_id"], "client_secret": team["client_secret"]}})


def status() -> dict:
    """{client, connected, sheets, email, expired}: ortak istemci ve ortak Google girişinin durumu."""
    d = GH.GoogleHesabi().durum()
    return {"client": bool(d["istemci"]), "connected": bool(d["bagli"]), "sheets": bool(d["sheets"]),
            "email": d.get("eposta"), "expired": bool(d.get("suresi_doldu"))}


def token() -> str:
    """Ortak girişin erişim belirteci (gerekirse yenilenir). Sheets izni yoksa ClientError."""
    hesap = GH.GoogleHesabi()
    if not hesap.sheets_izni():
        raise ClientError("SHEETS_GIRIS", "Ortak Google bağlantısında Google Sheets izni yok: Google bağlantısı sayfasından yeniden bağlan "
                                          "ve izin ekranında Google E-Tablolar kutusunu işaretle.")
    return hesap.belirtec()


def connect(cancel=None, browser=None) -> str | None:
    """Ortak Google girişini açar (GA4 + Sheets izinleri). Bağlanan e-postayı döndürür."""
    hesap = GH.GoogleHesabi(tarayici=browser) if browser else GH.GoogleHesabi()
    email = hesap.baglan(iptal=cancel)
    _use_google_account()
    return email


def logout() -> None:
    GH.GoogleHesabi().cikis()
