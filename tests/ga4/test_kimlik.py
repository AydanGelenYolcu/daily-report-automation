"""Hizmet hesabı girişi: RSA imzası OpenSSL ile birebir, JWT talepleri, belirteç önbelleği, hatalar, kurulum ve
bağlantı komutları. Google'a bağlanılmaz: token ve GA4 yanıtları sahte; anahtar test sırasında OpenSSL ile üretilir."""
import base64
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
import urllib.error
import urllib.parse

from netmera_automation.ga4 import ayarlar, kimlik
from netmera_automation.ga4.__main__ import main
from netmera_automation.ga4.istemci import ClientError
from netmera_automation.ga4.paths import PathError
from tests.ga4.helpers import TempDataCase

OPENSSL = shutil.which("openssl")
EPOSTA = "ga4-okuyucu@sentetik-proje.iam.gserviceaccount.com"
SAHTE_BELIRTEC = "sentetik-erisim-belirteci-123"


def b64url_coz(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@unittest.skipUnless(OPENSSL, "OpenSSL bulunamadı")
class KimlikTemel(TempDataCase):
    @classmethod
    def setUpClass(cls):
        cls._klasor = tempfile.mkdtemp(prefix="ga4-anahtar-")
        k = Path(cls._klasor)
        subprocess.run([OPENSSL, "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(k / "k8.pem")],
                       check=True, capture_output=True)
        subprocess.run([OPENSSL, "pkey", "-in", str(k / "k8.pem"), "-traditional", "-out", str(k / "k1.pem")], check=True, capture_output=True)
        cls.pem8 = (k / "k8.pem").read_text()
        cls.pem1 = (k / "k1.pem").read_text()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._klasor, ignore_errors=True)

    def anahtar_dosyasi(self, **degisiklik):
        veri = {"type": "service_account", "client_email": EPOSTA, "private_key": self.pem8,
                "token_uri": "https://oauth2.googleapis.com/token", "private_key_id": "sentetik"}
        veri.update(degisiklik)
        yol = self.tmp / "indirilen.json"
        yol.write_text(json.dumps(veri), encoding="utf-8")
        return yol


class Yanit:
    def __init__(self, veri):
        self.veri = veri

    def read(self, limit=-1):
        return self.veri

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class SahteGoogle:
    """Token adresine ve GA4 metadata adresine sahte yanıt verir; istekleri kaydeder."""

    def __init__(self, token_hatasi=None, metadata_hatasi=None):
        self.istekler, self.token_hatasi, self.metadata_hatasi = [], token_hatasi, metadata_hatasi

    def open(self, istek, timeout=None):
        self.istekler.append(istek)
        if istek.full_url.startswith("https://oauth2.googleapis.com/"):
            if self.token_hatasi:
                raise urllib.error.HTTPError(istek.full_url, 400, "x", {}, io.BytesIO(json.dumps({"error": self.token_hatasi}).encode()))
            return Yanit(json.dumps({"access_token": SAHTE_BELIRTEC, "expires_in": 3599, "token_type": "Bearer"}).encode())
        if istek.full_url.endswith("/metadata"):
            if self.metadata_hatasi:
                raise urllib.error.HTTPError(istek.full_url, 403, "x", {}, io.BytesIO(json.dumps({"error": {"status": self.metadata_hatasi}}).encode()))
            return Yanit(json.dumps({"dimensions": [{"apiName": "platform"}, {"apiName": "customEvent:kategori", "customDefinition": True}],
                                     "metrics": [{"apiName": "activeUsers"}, {"apiName": "totalUsers"}]}).encode())
        raise AssertionError(f"beklenmeyen adres {istek.full_url}")


class ImzaTestleri(KimlikTemel):
    def test_imza_openssl_ile_birebir_ayni(self):
        mesaj = b"site-yapi.sentetik.mesaj"
        (self.tmp / "m.bin").write_bytes(mesaj)
        (self.tmp / "k.pem").write_text(self.pem8)
        beklenen = subprocess.run([OPENSSL, "dgst", "-sha256", "-sign", str(self.tmp / "k.pem"), str(self.tmp / "m.bin")],
                                  check=True, capture_output=True).stdout
        for pem in (self.pem8, self.pem1):  # PKCS#8 (Google) ve PKCS#1
            with self.subTest(bicim=pem.splitlines()[0]):
                self.assertEqual(kimlik.rs256_imzala(mesaj, kimlik.rsa_anahtari(pem)), beklenen)

    def test_gecersiz_anahtarlar(self):
        for pem in ["", "merhaba", "-----BEGIN PRIVATE KEY-----\n!!!\n-----END PRIVATE KEY-----", self.pem8.replace("A", "B", 40)]:
            with self.subTest(pem=pem[:30]), self.assertRaises(Exception):
                kimlik.rsa_anahtari(pem)
        kucuk = subprocess.run([OPENSSL, "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:1024"], check=True, capture_output=True).stdout.decode()
        with self.assertRaises(ValueError):
            kimlik.rsa_anahtari(kucuk)

    def test_jwt_talepleri_ve_imza_dogrulamasi(self):
        anahtar = kimlik.rsa_anahtari(self.pem8)
        jwt = kimlik.jwt_olustur({"iss": EPOSTA, "aud": "https://oauth2.googleapis.com/token", "iat": 100, "exp": 3700}, anahtar)
        baslik, govde, imza = jwt.split(".")
        self.assertEqual(json.loads(b64url_coz(baslik)), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(json.loads(b64url_coz(govde))["iss"], EPOSTA)
        (self.tmp / "k.pem").write_text(self.pem8)
        (self.tmp / "m.bin").write_bytes(f"{baslik}.{govde}".encode())
        (self.tmp / "s.bin").write_bytes(b64url_coz(imza))
        subprocess.run([OPENSSL, "pkey", "-in", str(self.tmp / "k.pem"), "-pubout", "-out", str(self.tmp / "pub.pem")], check=True, capture_output=True)
        dogrula = subprocess.run([OPENSSL, "dgst", "-sha256", "-verify", str(self.tmp / "pub.pem"), "-signature", str(self.tmp / "s.bin"),
                                  str(self.tmp / "m.bin")], capture_output=True)
        self.assertIn(b"Verified OK", dogrula.stdout)


class BelirtecTestleri(KimlikTemel):
    def test_belirtec_akisi_ve_onbellek(self):
        google, saat = SahteGoogle(), [1000.0]
        hesap = kimlik.HizmetHesabi(self.anahtar_dosyasi(), opener=google, saat=lambda: saat[0])
        self.assertEqual(hesap.belirtec(), SAHTE_BELIRTEC)
        istek = google.istekler[0]
        self.assertEqual((istek.full_url, istek.get_method()), ("https://oauth2.googleapis.com/token", "POST"))
        form = urllib.parse.parse_qs(istek.data.decode())
        self.assertEqual(form["grant_type"], ["urn:ietf:params:oauth:grant-type:jwt-bearer"])
        talepler = json.loads(b64url_coz(form["assertion"][0].split(".")[1]))
        self.assertEqual(talepler, {"iss": EPOSTA, "scope": "https://www.googleapis.com/auth/analytics.readonly",
                                    "aud": "https://oauth2.googleapis.com/token", "iat": 1000, "exp": 4600})
        saat[0] += 3000
        hesap.belirtec()
        self.assertEqual(len(google.istekler), 1, "süre dolmadan yeni istek yok")
        saat[0] += 500
        hesap.belirtec()
        self.assertEqual(len(google.istekler), 2, "süre dolmaya yakın yenilenir")

    def test_google_hatalari(self):
        for kod, parca in (("invalid_grant", "saati"), ("invalid_client", "tanınmadı")):
            with self.subTest(kod=kod):
                hesap = kimlik.HizmetHesabi(self.anahtar_dosyasi(), opener=SahteGoogle(token_hatasi=kod))
                with self.assertRaises(ClientError) as yakala:
                    hesap.belirtec()
                self.assertEqual(yakala.exception.code, kod)
                self.assertIn(parca, str(yakala.exception))

    def test_anahtar_dosyasi_denetimi(self):
        hatalilar = [{"type": "authorized_user"}, {"client_email": "biri@gmail.com"},
                     {"token_uri": "https://kotu.example.com/token"}, {"private_key": "bozuk"}]
        for degisiklik in hatalilar:
            with self.subTest(degisiklik=degisiklik), self.assertRaises(ClientError) as yakala:
                kimlik.anahtar_dosyasi_oku(self.anahtar_dosyasi(**degisiklik))
            self.assertEqual(yakala.exception.code, "KIMLIK_DOSYASI")
        with self.assertRaises(ClientError):
            kimlik.anahtar_dosyasi_oku(self.tmp / "yok.json")
        with self.assertRaises(PathError):
            kimlik.HizmetHesabi(self.tmp / "OneDrive" / "anahtar.json")
        self.assertEqual(kimlik.anahtar_dosyasi_oku(self.anahtar_dosyasi())["eposta"], EPOSTA)


class KurulumTestleri(KimlikTemel):
    def calistir(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            kod = main(list(argv))
        return kod, out.getvalue(), err.getvalue()

    def test_kurulum_anahtari_kopyalar_ve_ayari_yazar(self):
        kaynak = self.anahtar_dosyasi()
        kod, out, _ = self.calistir("kurulum", "--anahtar", str(kaynak), "--property-id", "123456789")
        self.assertEqual(kod, 0)
        ayar = ayarlar.load()
        self.assertEqual(ayar["propertyler"], {"PROP_PROD": {"property_id": "123456789"}})
        hedef = Path(ayar["kimlik"]["anahtar_dosyasi"])
        self.assertEqual(hedef.name, "ga4-hizmet-hesabi.json")
        self.assertEqual(hedef.read_bytes(), kaynak.read_bytes())
        self.assertIn("İndirilen asıl dosyayı sil", out)
        self.assertNotIn("PRIVATE KEY", out)
        # İkinci property eklenince ilki korunur.
        self.assertEqual(self.calistir("kurulum", "--anahtar", str(kaynak), "--property-ref", "PROP_UAT", "--property-id", "42")[0], 0)
        self.assertEqual(set(ayarlar.load()["propertyler"]), {"PROP_PROD", "PROP_UAT"})

    def test_kurulum_hatalari_ayar_yazmaz(self):
        kod, _, err = self.calistir("kurulum", "--anahtar", str(self.anahtar_dosyasi(type="x")), "--property-id", "1")
        self.assertEqual(kod, 2)
        self.assertIn("hizmet hesabı anahtarı değil", err)
        self.assertFalse(ayarlar.settings_path().exists())
        kod, _, err = self.calistir("kurulum", "--anahtar", str(self.anahtar_dosyasi()), "--property-id", "G-ABC123")
        self.assertEqual(kod, 2)
        self.assertIn("yalnız rakam", err)

    def test_baglanti_veri_cekmeden_dogrular(self):
        self.calistir("kurulum", "--anahtar", str(self.anahtar_dosyasi()), "--property-id", "123456789")
        google = SahteGoogle()
        with mock.patch("urllib.request.build_opener", return_value=google):
            kod, out, _ = self.calistir("baglanti")
        self.assertEqual(kod, 0, out)
        self.assertIn("✓ Google erişim belirteci alındı", out)
        self.assertIn("✓ PROP_PROD: erişim var — 2 boyut, 2 metrik (1 özel tanım)", out)
        self.assertNotIn(SAHTE_BELIRTEC, out)
        self.assertEqual([i.full_url.rsplit("/", 1)[-1] for i in google.istekler], ["token", "metadata"])
        self.assertEqual(google.istekler[1].get_header("Authorization"), f"Bearer {SAHTE_BELIRTEC}")

    def test_baglanti_yetki_yoksa_yol_gosterir(self):
        self.calistir("kurulum", "--anahtar", str(self.anahtar_dosyasi()), "--property-id", "123456789")
        with mock.patch("urllib.request.build_opener", return_value=SahteGoogle(metadata_hatasi="PERMISSION_DENIED")):
            kod, out, _ = self.calistir("baglanti")
        self.assertEqual(kod, 2)
        self.assertIn("Görüntüleyen", out)
        self.assertNotIn("123456789", out)


if __name__ == "__main__":
    unittest.main()
