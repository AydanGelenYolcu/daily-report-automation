"""Kendi Google hesabınla giriş: OAuth 2.0 masaüstü uygulaması + PKCE, yerel geri dönüş adresi (127.0.0.1).

Akış Netmera Otomasyonu'ndaki Sheets bağlantısıyla aynıdır: normal tarayıcıda Google izin ekranı açılır, şifre uygulamaya
gelmez. Google'ın verdiği yenileme belirteci Windows DPAPI (CurrentUser) ile şifrelenip yalnız bu bilgisayarda saklanır.
İstenen izinler: GA4 salt okuma + hangi hesabın bağlı olduğunu göstermek için e-posta. Hesabın yönetici yetkileri
uygulamaya geçmez; uygulama yalnız istediği kapsamı kullanabilir.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser

from .istemci import ClientError
from .kimlik import SCOPE, YENILEME_PAYI, NoRedirect
from .paths import check_local_path, data_root

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
# GA4 yalnız okuma; Sheets çıktısı için tablolara yazma (kullanıcı kararı 2026-10-25). Eski oturum bir kez yeniden onaylanır.
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
KAPSAMLAR = ("openid", "email", SCOPE, SHEETS_SCOPE)
ISTEMCI_DOSYASI = "google-istemci.json"
OTURUM_DOSYASI = "google-oturum.json"
ENTROPI = b"AnalyticsVeriCekme:google-oturum:v1"
BEKLEME = 180  # sn: tarayıcıda izin ekranının tamamlanması için

HATALAR = {
    "invalid_grant": "Google oturumunun süresi doldu veya izin geri alındı. Bağlantı sayfasından \"Google ile bağlan\" ile yeniden bağlan. "
                     "OAuth uygulaması Test durumundaysa bu 7 günde bir olur; kalıcı kullanım için uygulamayı yayınla (Üretimde).",
    "invalid_client": "OAuth istemcisi tanınmadı (Google Cloud'da silinmiş olabilir). Masaüstü uygulaması JSON dosyasını yeniden indirip yükle.",
    "unauthorized_client": "Bu OAuth istemcisi masaüstü girişi için yetkili değil. Google Cloud'da \"Masaüstü uygulaması\" türünde istemci oluştur.",
    "access_denied": "Google izni verilmedi: izin ekranında iptal edildi ya da hesap bu uygulamaya erişemiyor. Uygulama Test durumundaysa "
                     "hesabın Kitle → Test kullanıcıları listesinde olmalı.",
    "org_internal": "OAuth uygulaması yalnız kendi kuruluşuna açık (Dahili). Google Auth Platform → Kitle bölümünde Harici seçip "
                    "hesabını test kullanıcısı olarak ekle.",
}


def _hata(kod: str | None, varsayilan: str) -> ClientError:
    return ClientError(kod if kod in HATALAR else "UNAUTHENTICATED", HATALAR.get(kod) or varsayilan)


# ---------- Windows DPAPI ----------

def _dpapi(veri: bytes, *, coz: bool = False) -> bytes:
    if os.name != "nt":
        raise ClientError("DPAPI_YOK", "Google oturumu yalnız Windows'ta korumalı saklanabilir.")
    import ctypes
    from ctypes import wintypes as W

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", W.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    kaynak = ctypes.create_string_buffer(veri, len(veri))
    entropi = ctypes.create_string_buffer(ENTROPI, len(ENTROPI))
    src = Blob(len(veri), ctypes.cast(kaynak, ctypes.POINTER(ctypes.c_ubyte)))
    ent = Blob(len(ENTROPI), ctypes.cast(entropi, ctypes.POINTER(ctypes.c_ubyte)))
    cikti = Blob()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    fn = crypt.CryptUnprotectData if coz else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p if coz else W.LPCWSTR, ctypes.POINTER(Blob), ctypes.c_void_p,
                   ctypes.c_void_p, W.DWORD, ctypes.POINTER(Blob)]
    fn.restype = W.BOOL
    try:
        # 1 = CRYPTPROTECT_UI_FORBIDDEN
        if not fn(ctypes.byref(src), None if coz else "Analytics Veri Cekme", ctypes.byref(ent), None, None, 1, ctypes.byref(cikti)):
            raise OSError(ctypes.get_last_error())
        return ctypes.string_at(cikti.pbData, cikti.cbData)
    finally:
        ctypes.memset(kaynak, 0, len(veri))
        if cikti.pbData:
            ctypes.memset(cikti.pbData, 0, cikti.cbData)
            kernel.LocalFree(ctypes.cast(cikti.pbData, ctypes.c_void_p))


# ---------- OAuth istemcisi (Google Cloud → Masaüstü uygulaması JSON'u) ----------

def istemci_dogrula(raw) -> dict:
    try:
        if not isinstance(raw, dict) or "installed" not in raw or "web" in raw:
            raise ValueError
        c = raw["installed"]
        cid, sir = c["client_id"], c["client_secret"]
        if not isinstance(cid, str) or not cid.endswith(".apps.googleusercontent.com") or len(cid) > 512:
            raise ValueError
        if not isinstance(sir, str) or not 1 <= len(sir) <= 1024 or any(ord(ch) < 33 for ch in sir):
            raise ValueError
        if c.get("auth_uri", AUTH_URL) not in (AUTH_URL, "https://accounts.google.com/o/oauth2/auth"):
            raise ValueError
        if c.get("token_uri", TOKEN_URL) not in (TOKEN_URL, "https://accounts.google.com/o/oauth2/token"):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise ClientError("ISTEMCI_GECERSIZ", "Google Cloud'dan indirilen \"Masaüstü uygulaması\" OAuth istemci JSON'unu seç "
                                              "(client_secret_….json). Web istemcisi veya hizmet hesabı dosyası uygun değil.") from None
    return {"client_id": cid, "client_secret": sir}


def istemci_kaydet(icerik: str) -> Path:
    """İstemciyi doğrular ve veri klasörüne yazar. Masaüstü istemci sırrı Google'a göre gizli sayılmaz; hesap belirteci içermez."""
    try:
        raw = json.loads(icerik)
    except ValueError:
        raise ClientError("ISTEMCI_GECERSIZ", "Dosya JSON değil.") from None
    istemci = istemci_dogrula(raw)
    hedef = check_local_path(data_root() / ISTEMCI_DOSYASI)
    hedef.parent.mkdir(parents=True, exist_ok=True)
    hedef.write_text(json.dumps({"installed": {**istemci, "auth_uri": AUTH_URL, "token_uri": TOKEN_URL,
                                               "redirect_uris": ["http://localhost"]}}, indent=2), encoding="utf-8")
    return hedef


def _eposta(id_token) -> str | None:
    """Yalnız gösterim için: token yanıtındaki id_token'ın e-posta alanı (TLS ile doğrudan Google'dan gelir)."""
    try:
        govde = id_token.split(".")[1]
        veri = json.loads(base64.urlsafe_b64decode(govde + "=" * (-len(govde) % 4)))
        e = veri.get("email")
        return e if isinstance(e, str) and "@" in e and len(e) <= 320 else None
    except Exception:
        return None


class GoogleHesabi:
    """Kullanıcının Google hesabıyla erişim belirteci. `belirtec()` gerekirse yeniler."""

    def __init__(self, istemci_yolu=None, oturum_yolu=None, *, opener=None, saat=time.time, tarayici=webbrowser.open):
        self.istemci_yolu = Path(istemci_yolu) if istemci_yolu else data_root() / ISTEMCI_DOSYASI
        self.oturum_yolu = Path(oturum_yolu) if oturum_yolu else data_root() / OTURUM_DOSYASI
        self.opener = opener or urllib.request.build_opener(NoRedirect)
        self.saat = saat
        self.tarayici = tarayici
        self._kilit = threading.RLock()

    # --- kayıtlar ---

    def istemci(self) -> dict:
        try:
            raw = json.loads(self.istemci_yolu.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            raise ClientError("ISTEMCI_YOK", "OAuth istemci dosyası yüklenmedi. Bağlantı sayfasında Masaüstü uygulaması JSON'unu bırak.") from None
        except (OSError, ValueError):
            raise ClientError("ISTEMCI_GECERSIZ", "OAuth istemci dosyası okunamadı; yeniden yükle.") from None
        return istemci_dogrula(raw)

    def oturum(self) -> dict | None:
        if not self.oturum_yolu.exists():
            return None
        try:
            kayit = json.loads(self.oturum_yolu.read_text(encoding="utf-8"))
            if kayit.get("surum") != 1:
                raise ValueError
            return json.loads(_dpapi(base64.b64decode(kayit["korumali"], validate=True), coz=True))
        except ClientError:
            raise
        except Exception:
            raise ClientError("OTURUM_OKUNAMADI", "Google oturumu bu Windows hesabında açılamadı. Bağlantıyı kesip yeniden bağlan.") from None

    def _kaydet(self, oturum: dict):
        blob = _dpapi(json.dumps(oturum).encode("utf-8"))
        hedef = check_local_path(self.oturum_yolu)
        hedef.parent.mkdir(parents=True, exist_ok=True)
        gecici = hedef.with_suffix(".tmp")
        gecici.write_text(json.dumps({"surum": 1, "korumali": base64.b64encode(blob).decode("ascii")}), encoding="utf-8")
        os.replace(gecici, hedef)

    def durum(self) -> dict:
        istemci_var = self.istemci_yolu.exists()
        try:
            o = self.oturum()
        except ClientError:
            o = {"suresi_doldu": True}
        o = o or {}
        return {"istemci": istemci_var, "bagli": bool(o.get("refresh_token")), "suresi_doldu": bool(o.get("suresi_doldu")),
                "eposta": o.get("eposta"), "sheets": SHEETS_SCOPE in (o.get("kapsam") or [])}

    def sheets_izni(self) -> bool:
        """Eski (Sheets eklenmeden önceki) oturumda kapsam kaydı yoktur: izin yok sayılır, kullanıcı yeniden bağlanır."""
        try:
            return SHEETS_SCOPE in ((self.oturum() or {}).get("kapsam") or [])
        except ClientError:
            return False

    def bilgi(self) -> dict:
        return {"eposta": (self.oturum() or {}).get("eposta") or "Google hesabı"}

    # --- Google uç noktaları ---

    def _post(self, url: str, form: dict) -> dict:
        istek = urllib.request.Request(url, data=urlencode(form).encode("ascii"), method="POST",
                                       headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        try:
            with self.opener.open(istek, timeout=30) as yanit:
                veri = json.loads(yanit.read(65536) or b"{}")
        except urllib.error.HTTPError as exc:
            kod = None
            try:
                kod = json.loads(exc.read(65536)).get("error")
            except Exception:
                pass
            raise _hata(kod if isinstance(kod, str) else None, "Google girişi başarısız.") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ClientError("AG_HATASI", "Google giriş adresine (oauth2.googleapis.com) bağlanılamadı.") from None
        except ValueError:
            raise ClientError("YANIT_GECERSIZ", "Google giriş yanıtı okunamadı.") from None
        if not isinstance(veri, dict):
            raise ClientError("YANIT_GECERSIZ", "Google giriş yanıtı okunamadı.")
        return veri

    def _kabul(self, istemci: dict, yanit: dict, onceki: dict | None = None) -> dict:
        erisim, yenileme = yanit.get("access_token"), yanit.get("refresh_token") or (onceki or {}).get("refresh_token")
        sure = yanit.get("expires_in")
        if not isinstance(erisim, str) or not erisim or not isinstance(yenileme, str) or not yenileme \
                or type(sure) is not int or not 0 < sure <= 86400:
            raise ClientError("YANIT_GECERSIZ", "Google çevrimdışı erişim izni vermedi; yeniden bağlan.")
        kapsam = yanit.get("scope", " ".join(KAPSAMLAR))
        if not isinstance(kapsam, str) or SCOPE not in kapsam.split():
            raise ClientError("KAPSAM_YOK", "Google Analytics okuma izni verilmedi. Yeniden bağlan ve izin ekranında "
                                            "Google Analytics kutusunu işaretle.")
        verilen = sorted(set(kapsam.split()) | (set((onceki or {}).get("kapsam") or []) if "scope" not in yanit else set()))
        return {"client_id": istemci["client_id"], "refresh_token": yenileme, "access_token": erisim,
                "expires_at": self.saat() + sure, "eposta": _eposta(yanit.get("id_token")) or (onceki or {}).get("eposta"),
                "kapsam": verilen}

    def belirtec(self) -> str:
        with self._kilit:
            istemci = self.istemci()
            o = self.oturum()
            if not o or not o.get("refresh_token"):
                if o and o.get("suresi_doldu"):
                    raise _hata("invalid_grant", "")
                raise ClientError("GOOGLE_BAGLI_DEGIL", "Google hesabı bağlı değil. Bağlantı sayfasından \"Google ile bağlan\".")
            if o.get("client_id") != istemci["client_id"]:
                raise ClientError("GOOGLE_BAGLI_DEGIL", "OAuth istemcisi değişti; Google hesabını yeniden bağla.")
            if o.get("access_token") and o.get("expires_at", 0) > self.saat() + YENILEME_PAYI:
                return o["access_token"]
            try:
                yanit = self._post(TOKEN_URL, {"grant_type": "refresh_token", "refresh_token": o["refresh_token"],
                                               "client_id": istemci["client_id"], "client_secret": istemci["client_secret"]})
            except ClientError as exc:
                if exc.code == "invalid_grant":
                    # Geçersiz belirteci tutmak "bağlı" görüntüsü verir: oturum düşürülür, hesap adı korunur.
                    self._kaydet({"client_id": istemci["client_id"], "suresi_doldu": True, "eposta": o.get("eposta")})
                raise
            yeni = self._kabul(istemci, yanit, o)
            self._kaydet(yeni)
            return yeni["access_token"]

    def baglan(self, *, bekleme: int = BEKLEME, iptal: threading.Event | None = None) -> str | None:
        """Tarayıcıda izin ekranını açar, geri dönüşü 127.0.0.1'de bekler, belirteci şifreli kaydeder. Bağlanan e-postayı döndürür."""
        istemci = self.istemci()
        dogrulayici = secrets.token_urlsafe(64)
        meydan = base64.urlsafe_b64encode(hashlib.sha256(dogrulayici.encode()).digest()).decode().rstrip("=")
        durum_anahtari = secrets.token_urlsafe(32)
        sonuc = {}

        class GeriDonus(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                p = urlsplit(self.path)
                q = parse_qs(p.query)
                gecerli = (p.path == "/oauth2callback" and self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"
                           and len(q.get("state", [])) == 1 and hmac.compare_digest(q["state"][0], durum_anahtari))
                if not gecerli:
                    self.send_error(400)
                    return
                if sonuc:
                    self.send_error(409)
                    return
                if q.get("error"):
                    sonuc["hata"] = q["error"][0][:80]  # yalnız genel hata kodu; URL saklanmaz
                elif len(q.get("code", [])) == 1:
                    sonuc["kod"] = q["code"][0]
                else:
                    self.send_error(400)
                    return
                ok = "kod" in sonuc
                baslik = "Google yanıtı alındı" if ok else "Google izni tamamlanmadı"
                metin = ("Bu sekmeyi kapatıp Analytics Veri Çekme'ye dön; bağlantı sonucu Bağlantı sayfasında görünür." if ok
                         else "Bu sekmeyi kapatıp Analytics Veri Çekme'ye dön; ne yapman gerektiği orada yazıyor.")
                govde = ("<!doctype html><meta charset='utf-8'><title>" + baslik + "</title>"
                         "<body style=\"font:16px/1.5 'Segoe UI',system-ui,sans-serif;display:grid;place-items:center;min-height:90vh;margin:0;"
                         "background:#f6f7fb;color:#1f2430\"><div style='max-width:440px;padding:28px;border-radius:14px;background:#fff;"
                         "box-shadow:0 0 0 1px #e3e6ee,0 12px 32px -12px rgba(30,40,80,.18);text-align:center'>"
                         "<div style='font-size:36px'>" + ("✓" if ok else "!") + "</div><h1 style='font-size:19px;margin:8px 0'>" + baslik +
                         "</h1><p style='color:#5b6273;margin:0'>" + metin + "</p></div>").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Length", str(len(govde)))
                self.end_headers()
                self.wfile.write(govde)

        sunucu = HTTPServer(("127.0.0.1", 0), GeriDonus)
        sunucu.timeout = 0.5
        donus = f"http://127.0.0.1:{sunucu.server_port}/oauth2callback"
        adres = AUTH_URL + "?" + urlencode({
            "client_id": istemci["client_id"], "redirect_uri": donus, "response_type": "code", "scope": " ".join(KAPSAMLAR),
            "state": durum_anahtari, "code_challenge": meydan, "code_challenge_method": "S256",
            "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"})
        try:
            if not self.tarayici(adres):
                raise ClientError("TARAYICI", "Google izin ekranı açılamadı. Varsayılan tarayıcı ayarını kontrol et.")
            son = time.monotonic() + bekleme
            while not sonuc:
                if iptal is not None and iptal.is_set():
                    raise ClientError("IPTAL", "Google bağlantısı iptal edildi.")
                if time.monotonic() > son:
                    raise ClientError("ZAMAN_ASIMI", "Google girişi zamanında tamamlanmadı; yeniden dene.")
                sunucu.handle_request()
        finally:
            sunucu.server_close()
        if sonuc.get("hata"):
            raise _hata(sonuc["hata"], "Google izni tamamlanmadı; yeniden dene.")
        try:
            yanit = self._post(TOKEN_URL, {"grant_type": "authorization_code", "code": sonuc["kod"], "client_id": istemci["client_id"],
                                           "client_secret": istemci["client_secret"], "redirect_uri": donus, "code_verifier": dogrulayici})
        except ClientError as exc:
            if exc.code == "invalid_grant":
                raise ClientError("invalid_grant", "Google onay kodu kullanılamadı (süresi geçmiş olabilir); yeniden bağlan.") from None
            raise
        with self._kilit:
            oturum = self._kabul(istemci, yanit)
            self._kaydet(oturum)
        return oturum.get("eposta")

    def cikis(self):
        """Google'daki izni geri almayı dener ve yerel oturumu siler."""
        try:
            o = self.oturum()
        except ClientError:
            o = None
        if o and o.get("refresh_token"):
            try:
                self._post(REVOKE_URL, {"token": o["refresh_token"]})
            except ClientError:
                pass  # ağ yoksa da yerel oturum silinir
        self.oturum_yolu.unlink(missing_ok=True)
