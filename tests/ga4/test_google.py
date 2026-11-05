"""Kendi Google hesabıyla giriş: istemci dosyası, DPAPI saklama, PKCE/geri dönüş akışı, belirteç yenileme ve arayüz uç
noktaları. Ağa çıkmaz: Google yanıtları sahte, "tarayıcı" geri dönüş adresini doğrudan çağırır."""
import base64
import io
import json
import os
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlencode, urlsplit

from netmera_automation.ga4 import ayarlar, google_hesap
from netmera_automation.ga4.google_hesap import GoogleHesabi
from netmera_automation.ga4.istemci import ClientError
from tests.ga4.helpers import TempDataCase
from tests.ga4.test_kimlik import Yanit
from tests.ga4.test_server import SunucuTemel

EPOSTA = "ornek.kullanici@example.com"
ISTEMCI = json.dumps({"installed": {"client_id": "000-sentetik.apps.googleusercontent.com", "client_secret": "SENTETIK-SIR",
                                    "project_id": "sentetik", "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                                    "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": ["http://localhost"]}})
KIMLIK = "id." + base64.urlsafe_b64encode(json.dumps({"email": EPOSTA}).encode()).decode().rstrip("=") + ".imza"
WINDOWS = os.name == "nt"


class SahteOAuth:
    """Google token/iptal, GA4 metadata ve Admin API mülk listesi için sahte yanıtlar; istekleri kaydeder."""

    def __init__(self, token_hatasi=None, kapsam=f"{google_hesap.SCOPE} {google_hesap.SHEETS_SCOPE}", refresh_ver=True):
        self.istekler, self.token_hatasi, self.kapsam, self.refresh_ver = [], token_hatasi, kapsam, refresh_ver
        self.sayac = 0

    def form(self, i):
        return {k: v[0] for k, v in parse_qs(self.istekler[i].data.decode()).items()}

    def open(self, istek, timeout=None):
        self.istekler.append(istek)
        url = istek.full_url
        if url == google_hesap.TOKEN_URL:
            if self.token_hatasi:
                raise urllib.error.HTTPError(url, 400, "x", {}, io.BytesIO(json.dumps({"error": self.token_hatasi}).encode()))
            self.sayac += 1
            yanit = {"access_token": f"SAHTE-ERISIM-{self.sayac}", "expires_in": 3599, "scope": f"openid {self.kapsam}", "id_token": KIMLIK}
            if self.refresh_ver:
                yanit["refresh_token"] = "SAHTE-YENILEME"
            return Yanit(json.dumps(yanit).encode())
        if url == google_hesap.REVOKE_URL:
            return Yanit(b"{}")
        if url.endswith("/metadata"):
            return Yanit(json.dumps({"dimensions": [{"apiName": "platform"}], "metrics": [{"apiName": "activeUsers"}]}).encode())
        if "accountSummaries" in url:
            return Yanit(json.dumps({"accountSummaries": [{"displayName": "Sentetik Hesap", "propertySummaries": [
                {"property": "properties/111", "displayName": "Sentetik Web"}, {"property": "properties/x", "displayName": "bozuk"}]}]}).encode())
        raise AssertionError(f"beklenmeyen adres {url}")


def tarayici_sahte(sorgu=None, durum_bozuk=False):
    """İzin ekranı yerine geri dönüş adresine Google gibi istek atar."""
    gorulen = {}

    def ac(adres):
        q = {k: v[0] for k, v in parse_qs(urlsplit(adres).query).items()}
        gorulen.update(q)
        cevap = sorgu if sorgu is not None else {"code": "SAHTE-KOD"}
        cevap = {**cevap, "state": "baska" if durum_bozuk else q["state"]}
        hedef = q["redirect_uri"] + "?" + urlencode(cevap)

        def git():
            try:
                urllib.request.urlopen(hedef, timeout=5).read()
            except urllib.error.HTTPError as exc:
                gorulen["geri_donus_kodu"] = exc.code
        threading.Thread(target=git, daemon=True).start()
        return True
    ac.gorulen = gorulen
    return ac


class IstemciTestleri(TempDataCase):
    def test_istemci_dosyasi_dogrulama(self):
        yol = google_hesap.istemci_kaydet(ISTEMCI)
        self.assertEqual(GoogleHesabi(yol).istemci()["client_id"], "000-sentetik.apps.googleusercontent.com")
        kotu = [{"web": {"client_id": "a.apps.googleusercontent.com", "client_secret": "s"}},
                {"type": "service_account", "client_email": "x@y"},
                {"installed": {"client_id": "kotu", "client_secret": "s"}},
                {"installed": {"client_id": "a.apps.googleusercontent.com", "client_secret": ""}},
                {"installed": {"client_id": "a.apps.googleusercontent.com", "client_secret": "s", "token_uri": "https://kotu.example.com/token"}}]
        for raw in kotu:
            with self.subTest(raw=raw), self.assertRaises(ClientError):
                google_hesap.istemci_kaydet(json.dumps(raw))

    def test_ayar_kimligi_ve_mulkler_korunur(self):
        ayarlar.mulk_kaydet("PROP_PROD", "123")
        ayarlar.google_istemci_kaydet(ISTEMCI)
        ayar = ayarlar.load()
        self.assertEqual(ayar["kimlik"]["tur"], "google_hesabi")
        self.assertEqual(ayar["propertyler"]["PROP_PROD"]["property_id"], "123")
        ayarlar.kimlik_ayarla(None)
        self.assertNotIn("kimlik", ayarlar.load())
        with self.assertRaises(ClientError) as yakala:
            ayarlar.token_provider(ayarlar.load())()
        self.assertEqual(yakala.exception.code, "KIMLIK_SECILMEDI")


@unittest.skipUnless(WINDOWS, "DPAPI yalnız Windows'ta")
class GirisTestleri(TempDataCase):
    def setUp(self):
        super().setUp()
        self.saat = [1_000_000.0]
        self.google = SahteOAuth()
        google_hesap.istemci_kaydet(ISTEMCI)

    def hesap(self, **kw):
        return GoogleHesabi(opener=self.google, saat=lambda: self.saat[0], **kw)

    def test_baglan_pkce_ve_sifreli_saklama(self):
        tarayici = tarayici_sahte()
        self.assertEqual(self.hesap(tarayici=tarayici).baglan(bekleme=10), EPOSTA)
        q = tarayici.gorulen
        self.assertEqual((q["code_challenge_method"], q["access_type"], q["prompt"]), ("S256", "offline", "consent"))
        self.assertEqual(q["scope"].split(), ["openid", "email", google_hesap.SCOPE, google_hesap.SHEETS_SCOPE])
        self.assertTrue(q["redirect_uri"].startswith("http://127.0.0.1:"))
        form = self.google.form(0)
        self.assertEqual((form["grant_type"], form["code"], form["redirect_uri"]), ("authorization_code", "SAHTE-KOD", q["redirect_uri"]))
        self.assertEqual(base64.urlsafe_b64encode(__import__("hashlib").sha256(form["code_verifier"].encode()).digest()).decode().rstrip("="),
                         q["code_challenge"])
        ham = (self.tmp / "veri" / google_hesap.OTURUM_DOSYASI).read_text()
        self.assertNotIn("SAHTE-YENILEME", ham)  # dosyada düz metin belirteç yok
        self.assertNotIn(EPOSTA, ham)
        self.assertEqual(self.hesap().durum(), {"istemci": True, "bagli": True, "suresi_doldu": False, "eposta": EPOSTA, "sheets": True})
        self.assertTrue(self.hesap().sheets_izni())

    def test_belirtec_onbellek_yenileme_ve_suresi_dolma(self):
        self.hesap(tarayici=tarayici_sahte()).baglan(bekleme=10)
        h = self.hesap()
        self.assertEqual(h.belirtec(), "SAHTE-ERISIM-1")
        self.assertEqual(len(self.google.istekler), 1)  # önbellekten
        self.saat[0] += 3599 - 60  # yenileme payının içinde
        self.assertEqual(h.belirtec(), "SAHTE-ERISIM-2")
        self.assertEqual(self.google.form(1)["grant_type"], "refresh_token")
        self.saat[0] += 4000
        self.google.token_hatasi = "invalid_grant"
        with self.assertRaises(ClientError) as yakala:
            h.belirtec()
        self.assertIn("yeniden bağlan", str(yakala.exception))
        self.assertEqual(h.durum(), {"istemci": True, "bagli": False, "suresi_doldu": True, "eposta": EPOSTA, "sheets": False})
        with self.assertRaises(ClientError):  # yeniden denemede Google'a gitmeden aynı hata
            h.belirtec()
        self.assertEqual(len(self.google.istekler), 3)

    def test_izin_reddi_kapsam_eksigi_ve_sahte_geri_donus(self):
        with self.assertRaises(ClientError) as yakala:
            self.hesap(tarayici=tarayici_sahte({"error": "access_denied"})).baglan(bekleme=10)
        self.assertIn("Test kullanıcıları", str(yakala.exception))
        self.google.kapsam = "email"
        with self.assertRaises(ClientError) as yakala:
            self.hesap(tarayici=tarayici_sahte()).baglan(bekleme=10)
        self.assertEqual(yakala.exception.code, "KAPSAM_YOK")
        self.google.kapsam, self.google.refresh_ver = google_hesap.SCOPE, False
        with self.assertRaises(ClientError):
            self.hesap(tarayici=tarayici_sahte()).baglan(bekleme=10)
        tarayici = tarayici_sahte(durum_bozuk=True)
        with self.assertRaises(ClientError) as yakala:
            self.hesap(tarayici=tarayici).baglan(bekleme=1)
        self.assertEqual(yakala.exception.code, "ZAMAN_ASIMI")
        self.assertEqual(tarayici.gorulen["geri_donus_kodu"], 400)  # state uymayan istek reddedildi
        self.assertFalse(self.hesap().durum()["bagli"])

    def test_iptal_ve_cikis(self):
        iptal = threading.Event()
        iptal.set()
        with self.assertRaises(ClientError) as yakala:
            self.hesap(tarayici=lambda adres: True).baglan(iptal=iptal)
        self.assertEqual(yakala.exception.code, "IPTAL")
        self.hesap(tarayici=tarayici_sahte()).baglan(bekleme=10)
        self.hesap().cikis()
        self.assertEqual(self.google.istekler[-1].full_url, google_hesap.REVOKE_URL)
        self.assertFalse((self.tmp / "veri" / google_hesap.OTURUM_DOSYASI).exists())
        self.assertFalse(self.hesap().durum()["bagli"])

    def test_bozuk_oturum_dosyasi(self):
        (self.tmp / "veri" / google_hesap.OTURUM_DOSYASI).write_text(json.dumps({"surum": 1, "korumali": "AAAA"}))
        self.assertTrue(self.hesap().durum()["suresi_doldu"])
        with self.assertRaises(ClientError) as yakala:
            self.hesap().belirtec()
        self.assertEqual(yakala.exception.code, "OTURUM_OKUNAMADI")


@unittest.skipUnless(WINDOWS, "DPAPI yalnız Windows'ta")
class SunucuGoogleTestleri(SunucuTemel):
    def test_google_ile_bagla_mulk_sec_test_et_cik(self):
        kod, r = self.istek("POST", "/api/google/istemci", {"icerik": '{"type": "service_account"}'})
        self.assertEqual(kod, 400)
        kod, r = self.istek("POST", "/api/google/istemci", {"icerik": ISTEMCI})
        self.assertEqual(kod, 200, r)
        b = r["baglanti"]
        self.assertEqual((b["yontem"], b["kimlik_hazir"], b["kurulu"], b["google"]["istemci"]), ("google_hesabi", False, False, True))
        kod, t = self.istek("POST", "/api/baglanti", {})
        self.assertFalse(t["basarili"])
        self.assertIn("Google hesabı bağlı değil", t["adimlar"][-1]["mesaj"])

        google = SahteOAuth()
        self.app.tarayici = tarayici_sahte()
        with mock.patch("urllib.request.build_opener", return_value=google):
            kod, r = self.istek("POST", "/api/google/baglan", {})
            self.assertEqual(kod, 200, r)
            self.assertEqual((r["eposta"], r["baglanti"]["kimlik_hazir"], r["baglanti"]["kurulu"]), (EPOSTA, True, False))
            kod, m = self.istek("GET", "/api/google/mulkler")
            self.assertEqual(m["mulkler"], [{"hesap": "Sentetik Hesap", "ad": "Sentetik Web", "property_id": "111"}])
            self.assertEqual(self.istek("POST", "/api/mulk", {"property_ref": "PROP_PROD", "property_id": "G-1"})[0], 400)
            kod, r = self.istek("POST", "/api/mulk", {"property_ref": "prop_prod", "property_id": "111", "aciklama": "Sentetik Web"})
            self.assertEqual(kod, 200, r)
            self.assertTrue(r["baglanti"]["kurulu"])
            kod, t = self.istek("POST", "/api/baglanti", {})
        self.assertTrue(t["basarili"], t)
        self.assertEqual([a["ad"] for a in t["adimlar"]], ["Ayar dosyası", "Google hesabı", "Google girişi", "Mülk PROP_PROD"])
        self.assertEqual(t["adimlar"][1]["mesaj"], EPOSTA)
        self.assertNotIn("SAHTE-ERISIM", json.dumps(t) + json.dumps(self.istek("GET", "/api/durum")[1]))

        with mock.patch("urllib.request.build_opener", return_value=google):
            kod, r = self.istek("POST", "/api/google/cikis", {})
        self.assertEqual((r["baglanti"]["kimlik_hazir"], r["baglanti"]["son_test"]), (False, None))
        self.assertEqual(r["baglanti"]["propertyler"], ["PROP_PROD"])  # mülk ayarı kalır

    def test_yeni_tiklama_yarim_girisi_birakir_ve_iptal(self):
        self.istek("POST", "/api/google/istemci", {"icerik": ISTEMCI})
        acilan = []
        self.app.tarayici = lambda adres: acilan.append(adres) or False  # tarayıcı açılamasa da giriş sürer
        sonuc = {}

        def baslat(ad):
            th = threading.Thread(target=lambda: sonuc.update({ad: self.istek("POST", "/api/google/baglan", {})}))
            th.start()
            return th

        ilk = baslat("ilk")
        for _ in range(50):
            if acilan:
                break
            threading.Event().wait(0.1)
        adres = self.istek("GET", "/api/google/adres")[1]["adres"]
        self.assertEqual(adres, acilan[0])
        self.assertTrue(adres.startswith(google_hesap.AUTH_URL + "?"))
        ikinci = baslat("ikinci")
        ilk.join(5)
        self.assertEqual(sonuc["ilk"][0], 400)
        self.assertIn("iptal", sonuc["ilk"][1]["error"])
        for _ in range(50):
            if len(acilan) == 2:
                break
            threading.Event().wait(0.1)
        self.assertEqual(len(acilan), 2)
        self.assertEqual(self.istek("POST", "/api/google/iptal", {})[0], 200)
        ikinci.join(5)
        self.assertEqual(sonuc["ikinci"][0], 400)
        self.assertIsNone(self.istek("GET", "/api/google/adres")[1]["adres"])
        gunluk = (self.tmp / "veri" / "arayuz-gunluk.txt").read_text(encoding="utf-8")
        self.assertEqual(gunluk.count("google girişi başladı"), 2)
        self.assertEqual(gunluk.count("google girişi bitmedi: IPTAL"), 2)
        self.assertNotIn("accounts.google.com", gunluk)

if __name__ == "__main__":
    unittest.main()
