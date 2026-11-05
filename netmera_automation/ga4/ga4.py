"""Gerçek GA4 Data API istemcisi (REST v1beta, yalnız okuma). Standart kütüphane ile çalışır.

Erişim belirteci `token_provider` ile verilir. Hata iletisine ham yanıt, property kimliği veya
belirteç girmez; yalnız Google'ın kısa hata kodu kullanılır.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import ayarlar
from .istemci import ClientError, MESSAGES

API_ROOT = "https://analyticsdata.googleapis.com/v1beta/"
ADMIN_ROOT = "https://analyticsadmin.googleapis.com/v1beta/"  # yalnız mülk listesi (okuma)
SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
RETRY_STATUSES = {500, 503}  # GA4 saatte 10 sunucu hatasına izin verir; yalnız bir kez tekrar
STATUS_CODES = {400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED", 403: "PERMISSION_DENIED", 429: "RESOURCE_EXHAUSTED"}
MAX_BODY = 20 * 1024 * 1024
TIMEOUT = 60


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ClientError("AG_HATASI", "GA4 isteği yönlendirildi; erişim bilgisi başka adrese gönderilmedi.")


def _reason(exc: urllib.error.HTTPError) -> str | None:
    """Hata gövdesinden yalnız kısa kodu çıkarır (ör. SERVICE_DISABLED, PERMISSION_DENIED)."""
    try:
        raw = exc.read(65537)
        error = json.loads(raw).get("error") if len(raw) <= 65536 else None
    except Exception:
        return None
    if not isinstance(error, dict):
        return None
    for detail in error.get("details") or []:
        if isinstance(detail, dict) and isinstance(detail.get("reason"), str):
            return detail["reason"][:60]
    return error["status"][:60] if isinstance(error.get("status"), str) else None


def _code(status: int, reason: str | None) -> str:
    if reason in MESSAGES:
        return reason
    if status >= 500:
        return "UNAVAILABLE"
    return STATUS_CODES.get(status, f"HTTP_{status}")


class RestClient:
    kaynak = "ga4"

    def __init__(self, settings: dict, *, token_provider=None, opener=None, sleep=time.sleep):
        self.settings = settings
        self.token_provider = token_provider or ayarlar.token_provider(settings)
        self.opener = opener or urllib.request.build_opener(NoRedirect)
        self.sleep = sleep

    def metadata(self, ref: str) -> dict:
        return self._call("GET", f"properties/{ayarlar.property_id(self.settings, ref)}/metadata")

    def run_report(self, ref: str, body: dict) -> dict:
        return self._call("POST", f"properties/{ayarlar.property_id(self.settings, ref)}:runReport", body)

    def check_compatibility(self, ref: str, body: dict) -> dict:
        return self._call("POST", f"properties/{ayarlar.property_id(self.settings, ref)}:checkCompatibility", body)

    def mulkler(self) -> list[dict]:
        """Hesabın erişebildiği GA4 mülkleri (Admin API accountSummaries, salt okuma). En çok 1000."""
        sonuc, sayfa = [], None
        for _ in range(5):
            yol = "accountSummaries?pageSize=200" + (f"&pageToken={urllib.parse.quote(sayfa)}" if sayfa else "")
            veri = self._call("GET", yol, kok=ADMIN_ROOT)
            for hesap in veri.get("accountSummaries") or []:
                for m in hesap.get("propertySummaries") or []:
                    pid = str(m.get("property", "")).rpartition("/")[2]
                    if pid.isdigit():
                        sonuc.append({"hesap": str(hesap.get("displayName") or "")[:120], "ad": str(m.get("displayName") or pid)[:120],
                                      "property_id": pid})
            sayfa = veri.get("nextPageToken")
            if not sayfa:
                break
        return sonuc

    def _call(self, method: str, path: str, body: dict | None = None, kok: str = API_ROOT) -> dict:
        token = self.token_provider()
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        for attempt in (1, 2):
            request = urllib.request.Request(kok + path, data=data, method=method, headers=headers)
            try:
                with self.opener.open(request, timeout=TIMEOUT) as response:
                    raw = response.read(MAX_BODY + 1)
            except urllib.error.HTTPError as exc:
                if exc.code in RETRY_STATUSES and attempt == 1:
                    self.sleep(2)
                    continue
                raise ClientError(_code(exc.code, _reason(exc))) from None
            except (urllib.error.URLError, TimeoutError, OSError):
                raise ClientError("AG_HATASI") from None
            if len(raw) > MAX_BODY:
                raise ClientError("YANIT_GECERSIZ", "GA4 yanıtı çok büyük; işin 'limit' değerini düşür.")
            try:
                parsed = json.loads(raw)
            except ValueError:
                raise ClientError("YANIT_GECERSIZ") from None
            if not isinstance(parsed, dict):
                raise ClientError("YANIT_GECERSIZ")
            return parsed
        raise AssertionError("ulaşılamaz")
