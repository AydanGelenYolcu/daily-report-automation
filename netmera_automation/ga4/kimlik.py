"""Google hizmet hesabı girişi (OAuth 2.0 JWT bearer), yalnız standart kütüphane ile.

Akış: anahtar dosyasındaki RSA anahtarıyla RS256 imzalı bir JWT üretilir, Google'ın token adresine gönderilir,
dönen kısa ömürlü erişim belirteci (1 saat) bellekte tutulur. Anahtar, belirteç ve imza hiçbir yere yazılmaz.
İmza PKCS#1 v1.5 + SHA-256'dır; testlerde OpenSSL'in ürettiği imzayla birebir karşılaştırılır.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

from .istemci import ClientError
from .paths import check_local_path

SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
TOKEN_HOSTS = {"oauth2.googleapis.com"}
RSA_OID = bytes.fromhex("06092a864886f70d010101")  # 1.2.840.113549.1.1.1 rsaEncryption
SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
YENILEME_PAYI = 120  # sn: belirteç süresi dolmadan önce yenilenir


class KeyFileError(ClientError):
    def __init__(self, message: str):
        super().__init__("KIMLIK_DOSYASI", message)


# ---------- RSA (yalnız imzalama) ----------

def _der(data: bytes, i: int) -> tuple[int, bytes, int]:
    if i + 2 > len(data):
        raise ValueError("DER kısa")
    tag, uzunluk = data[i], data[i + 1]
    i += 2
    if uzunluk & 0x80:
        n = uzunluk & 0x7F
        if not 0 < n <= 4:
            raise ValueError("DER uzunluk")
        uzunluk = int.from_bytes(data[i:i + n], "big")
        i += n
    if i + uzunluk > len(data):
        raise ValueError("DER taşma")
    return tag, data[i:i + uzunluk], i + uzunluk


def _ogeler(icerik: bytes) -> list[tuple[int, bytes]]:
    sonuc, i = [], 0
    while i < len(icerik):
        tag, deger, i = _der(icerik, i)
        sonuc.append((tag, deger))
    return sonuc


def rsa_anahtari(pem: str) -> dict:
    """PKCS#8 ("BEGIN PRIVATE KEY", Google'ın biçimi) veya PKCS#1 ("BEGIN RSA PRIVATE KEY") RSA anahtarı."""
    satirlar = [s.strip() for s in pem.strip().splitlines()]
    if not satirlar or not satirlar[0].startswith("-----BEGIN") or "PRIVATE KEY" not in satirlar[0]:
        raise ValueError("PEM değil")
    der = base64.b64decode("".join(s for s in satirlar if not s.startswith("-----")), validate=True)
    tag, govde, _ = _der(der, 0)
    ogeler = _ogeler(govde)
    if "BEGIN PRIVATE KEY" in satirlar[0]:
        if len(ogeler) < 3 or ogeler[1][0] != 0x30 or RSA_OID not in ogeler[1][1] or ogeler[2][0] != 0x04:
            raise ValueError("RSA anahtarı değil")
        _, govde, _ = _der(ogeler[2][1], 0)
        ogeler = _ogeler(govde)
    sayilar = [int.from_bytes(v, "big") for t, v in ogeler if t == 0x02]
    if len(sayilar) < 9:
        raise ValueError("RSA alanları eksik")
    _, n, e, d, p, q, dp, dq, qinv = sayilar[:9]
    if n.bit_length() < 2048 or p * q != n:
        raise ValueError("RSA anahtarı geçersiz")
    return {"n": n, "e": e, "d": d, "p": p, "q": q, "dp": dp, "dq": dq, "qinv": qinv}


def rs256_imzala(mesaj: bytes, anahtar: dict) -> bytes:
    """RSASSA-PKCS1-v1_5 / SHA-256 (RFC 8017 §8.2). Sonuç açık anahtarla doğrulanır; hatalı imza dönmez."""
    n = anahtar["n"]
    k = (n.bit_length() + 7) // 8
    t = SHA256_DIGEST_INFO + hashlib.sha256(mesaj).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    m = int.from_bytes(em, "big")
    m1, m2 = pow(m, anahtar["dp"], anahtar["p"]), pow(m, anahtar["dq"], anahtar["q"])  # CRT
    s = m2 + ((anahtar["qinv"] * (m1 - m2)) % anahtar["p"]) * anahtar["q"]
    if pow(s, anahtar["e"], n) != m:
        raise ValueError("İmza doğrulanamadı")
    return s.to_bytes(k, "big")


def _b64url(veri: bytes) -> str:
    return base64.urlsafe_b64encode(veri).rstrip(b"=").decode("ascii")


def jwt_olustur(talepler: dict, anahtar: dict) -> str:
    baslik = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    govde = _b64url(json.dumps(talepler, separators=(",", ":")).encode())
    imza = _b64url(rs256_imzala(f"{baslik}.{govde}".encode("ascii"), anahtar))
    return f"{baslik}.{govde}.{imza}"


# ---------- Anahtar dosyası ----------

def anahtar_dosyasi_oku(yol) -> dict:
    """Google Cloud'dan indirilen hizmet hesabı JSON'u: client_email, private_key, token_uri."""
    yol = Path(yol)
    try:
        veri = json.loads(yol.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise KeyFileError(f"Hizmet hesabı anahtar dosyası bulunamadı: {yol}") from None
    except (OSError, ValueError):
        raise KeyFileError(f"Hizmet hesabı anahtar dosyası okunamadı (JSON değil): {yol}") from None
    if not isinstance(veri, dict) or veri.get("type") != "service_account":
        raise KeyFileError("Bu dosya hizmet hesabı anahtarı değil (\"type\": \"service_account\" bekleniyor). "
                           "Cloud Console → Hizmet hesapları → Anahtarlar → JSON anahtarı indir.")
    eposta, token_uri = veri.get("client_email"), veri.get("token_uri", "https://oauth2.googleapis.com/token")
    if not isinstance(eposta, str) or not eposta.endswith(".gserviceaccount.com"):
        raise KeyFileError("Anahtar dosyasında geçerli bir hizmet hesabı e-postası yok.")
    parca = urllib.parse.urlsplit(token_uri if isinstance(token_uri, str) else "")
    if parca.scheme != "https" or parca.hostname not in TOKEN_HOSTS:
        raise KeyFileError("Anahtar dosyasındaki token adresi Google'a ait değil; dosya kullanılmadı.")
    try:
        anahtar = rsa_anahtari(veri.get("private_key") or "")
    except (ValueError, TypeError):
        raise KeyFileError("Anahtar dosyasındaki özel anahtar okunamadı; Cloud Console'dan yeni JSON anahtarı indir.") from None
    return {"eposta": eposta, "token_uri": token_uri, "anahtar": anahtar}


def eposta_goster(eposta: str) -> str:
    """Ekranda tam e-posta yerine ad + proje: ga4-okuyucu@…(proje).iam.gserviceaccount.com"""
    ad, _, alan = eposta.partition("@")
    return f"{ad}@{alan}"


# ---------- Erişim belirteci ----------

HATALAR = {
    "invalid_grant": "Google anahtarı reddetti: anahtar silinmiş/devre dışı olabilir ya da bilgisayarın saati yanlış "
                     "(Windows → Tarih ve saat → Şimdi eşitle).",
    "invalid_client": "Hizmet hesabı tanınmadı (silinmiş olabilir). Cloud Console'da hesabı ve anahtarı kontrol et.",
    "unauthorized_client": "Hizmet hesabının bu işlem için yetkisi yok. Anahtarı yeniden oluşturmayı dene.",
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ClientError("AG_HATASI", "Google giriş isteği yönlendirildi; anahtar başka adrese gönderilmedi.")


class HizmetHesabi:
    """`belirtec()` çağrıldıkça geçerli bir erişim belirteci döndürür (gerekirse yeniler)."""

    def __init__(self, anahtar_yolu, *, opener=None, saat=time.time):
        self.yol = check_local_path(Path(anahtar_yolu))
        self.opener = opener or urllib.request.build_opener(NoRedirect)
        self.saat = saat
        self._bilgi = None
        self._belirtec, self._bitis = None, 0.0

    def bilgi(self) -> dict:
        if self._bilgi is None:
            self._bilgi = anahtar_dosyasi_oku(self.yol)
        return self._bilgi

    def belirtec(self) -> str:
        simdi = self.saat()
        if self._belirtec and simdi < self._bitis - YENILEME_PAYI:
            return self._belirtec
        bilgi = self.bilgi()
        iat = int(simdi)
        assertion = jwt_olustur({"iss": bilgi["eposta"], "scope": SCOPE, "aud": bilgi["token_uri"],
                                 "iat": iat, "exp": iat + 3600}, bilgi["anahtar"])
        govde = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                        "assertion": assertion}).encode("ascii")
        istek = urllib.request.Request(bilgi["token_uri"], data=govde, method="POST",
                                       headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        try:
            with self.opener.open(istek, timeout=30) as yanit:
                veri = json.loads(yanit.read(65536))
        except urllib.error.HTTPError as exc:
            kod = None
            try:
                kod = json.loads(exc.read(65536)).get("error")
            except Exception:
                pass
            raise ClientError(kod if kod in HATALAR else "UNAUTHENTICATED", HATALAR.get(kod)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ClientError("AG_HATASI", "Google giriş adresine (oauth2.googleapis.com) bağlanılamadı.") from None
        except ValueError:
            raise ClientError("YANIT_GECERSIZ", "Google giriş yanıtı okunamadı.") from None
        belirtec = veri.get("access_token") if isinstance(veri, dict) else None
        sure = veri.get("expires_in", 3600) if isinstance(veri, dict) else 3600
        if not isinstance(belirtec, str) or not belirtec:
            raise ClientError("YANIT_GECERSIZ", "Google erişim belirteci vermedi.")
        self._belirtec, self._bitis = belirtec, simdi + (sure if isinstance(sure, (int, float)) else 3600)
        return belirtec
