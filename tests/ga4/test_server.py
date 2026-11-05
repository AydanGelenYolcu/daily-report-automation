"""Yerel arayüz sunucusu: güvenlik (yerel erişim, oturum anahtarı), iş deposu, içe aktarma, doğrulama, sentetik
çalıştırma, CSV, kurulum ve bağlantı testi. Ağa çıkmaz: Google yanıtları sahte."""
import json
from http.client import HTTPConnection
from pathlib import Path
import shutil
import subprocess
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request

from netmera_automation.ga4 import server, store
from netmera_automation.ga4.istemci import ClientError
from tests.ga4.helpers import TempDataCase, job
from tests.ga4.test_kimlik import EPOSTA, OPENSSL, SahteGoogle


class SunucuTemel(TempDataCase):
    def setUp(self):
        super().setUp()
        self.app = server.App(server._free_port(0))
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", self.app.port), self.app.make_handler())
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.kok = f"http://127.0.0.1:{self.app.port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def istek(self, yontem, yol, govde=None, token=True, host=None, tur="application/json"):
        basliklar = {"Content-Type": tur} if govde is not None else {}
        if token:
            basliklar["X-Token"] = self.app.token
        if host:
            basliklar["Host"] = host
        veri = json.dumps(govde).encode() if govde is not None else None
        r = urllib.request.Request(self.kok + yol, data=veri, method=yontem, headers=basliklar)
        try:
            with urllib.request.urlopen(r, timeout=10) as yanit:
                govde = yanit.read()
                return yanit.status, (json.loads(govde) if yanit.headers.get_content_type() == "application/json" else govde.decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")


class SurumTestleri(unittest.TestCase):
    @unittest.skip("Ayrı Analytics arayüz sayfası birleşik uygulamada kullanılmıyor (arayüz Netmera'da).")
    def test_arayuz_surumu_paketle_ayni(self):
        import re
        from netmera_automation.ga4 import VERSION
        sayfa = server.UI_FILE.read_text(encoding="utf-8")
        self.assertEqual(re.search(r"const ARAYUZ_SURUMU = '([^']+)'", sayfa).group(1), VERSION)

    @unittest.skipUnless(shutil.which("node"), "Node.js yok")
    @unittest.skip("Ayrı Analytics arayüz sayfası birleşik uygulamada kullanılmıyor (arayüz Netmera'da).")
    def test_sayfa_betigi_sozdizimi(self):
        """Tek bir sözdizimi hatası bütün arayüzü açılmaz yapar; Node.js varsa betik denetlenir."""
        import tempfile
        sayfa = server.UI_FILE.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as klasor:
            yol = Path(klasor) / "betik.js"
            yol.write_text(sayfa[sayfa.index("<script>") + 8:sayfa.rindex("</script>")], encoding="utf-8")
            sonuc = subprocess.run([shutil.which("node"), "--check", str(yol)], capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(sonuc.returncode, 0, sonuc.stderr[:800])


class GuvenlikTestleri(SunucuTemel):
    def test_gecersiz_govde_ve_uzunluk_yanit_kesmez(self):
        for raw, length in [(b'[]', '2'), (b'null', '4'), (b'{}', '-1'), (b'{}', 'invalid')]:
            with self.subTest(raw=raw, length=length):
                connection = HTTPConnection('127.0.0.1', self.app.port, timeout=2)
                try:
                    connection.putrequest('POST', '/api/isler/kaydet')
                    for name, value in {'X-Token': self.app.token, 'Content-Type': 'application/json', 'Content-Length': length}.items():
                        connection.putheader(name, value)
                    connection.endheaders(raw)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 400)
                    self.assertIn('error', json.loads(response.read()))
                finally:
                    connection.close()

    @unittest.skip("Ayrı Analytics arayüz sayfası birleşik uygulamada kullanılmıyor (arayüz Netmera'da).")
    def test_sayfa_ve_api_korumasi(self):
        with urllib.request.urlopen(f"{self.kok}/?t={self.app.token}") as yanit:
            html = yanit.read().decode()
            self.assertIn(self.app.token, html)
            self.assertNotIn("__TOKEN__", html)
            self.assertIn("default-src 'self'", yanit.headers["Content-Security-Policy"])
        with self.assertRaises(urllib.error.HTTPError) as yakala:
            urllib.request.urlopen(f"{self.kok}/?t=yanlis")
        self.assertEqual(yakala.exception.code, 403)
        self.assertEqual(self.istek("GET", "/api/durum", token=False)[0], 403)
        self.assertEqual(self.istek("GET", "/api/durum", host="kotu.example.com")[0], 403)
        self.assertEqual(self.istek("POST", "/api/isler/kaydet", {"is": {}}, tur="text/plain")[0], 415)


class IsTestleri(SunucuTemel):
    def test_durum_ve_is_yasam_dongusu(self):
        kod, d = self.istek("GET", "/api/durum")
        self.assertEqual((kod, d["is_sayisi"], d["baglanti"]["kurulu"]), (200, 0, False))
        kod, r = self.istek("POST", "/api/isler/kaydet", {"is": job(ad="Günlük kullanıcı", boyutlar=["platform"])})
        self.assertEqual(kod, 200)
        self.assertEqual([j["ad"] for j in r["isler"]], ["Günlük kullanıcı"])
        # Ad değiştirerek düzenleme; aynı adla ikinci iş reddedilir.
        kod, r = self.istek("POST", "/api/isler/kaydet", {"is": job(ad="Platform kullanıcı"), "eski_ad": "Günlük kullanıcı"})
        self.assertEqual([j["ad"] for j in r["isler"]], ["Platform kullanıcı"])
        self.istek("POST", "/api/isler/kaydet", {"is": job(ad="İkinci")})
        kod, r = self.istek("POST", "/api/isler/kaydet", {"is": job(ad="ikinci")})
        self.assertEqual(kod, 400)
        self.assertIn("kullanılıyor", r["error"])
        kod, r = self.istek("POST", "/api/isler/kaydet", {"is": job(metrikler=[])})
        self.assertEqual(kod, 400)
        kod, r = self.istek("POST", "/api/isler/etkin", {"ad": "İkinci", "etkin": False})
        self.assertFalse(next(j for j in r["isler"] if j["ad"] == "İkinci")["etkin"])
        kod, r = self.istek("POST", "/api/isler/sil", {"ad": "İkinci"})
        self.assertEqual([j["ad"] for j in r["isler"]], ["Platform kullanıcı"])
        # Depo CLI biçimindedir.
        self.assertEqual(store.load()[0]["ad"], "Platform kullanıcı")  # birleşik: Netmera profilinde ga4 işi
        # Son iş silinince depo boş kalır ama bozulmaz: liste, durum ve yeni iş ekleme çalışır.
        kod, r = self.istek("POST", "/api/isler/sil", {"ad": "Platform kullanıcı"})
        self.assertEqual((kod, r["isler"]), (200, []))
        self.assertEqual(self.istek("GET", "/api/durum")[1]["is_sayisi"], 0)
        kod, r = self.istek("POST", "/api/isler/kaydet", {"is": job(ad="Yeniden")})
        self.assertEqual((kod, [j["ad"] for j in r["isler"]]), (200, ["Yeniden"]))

    def test_taslak_ice_aktarma(self):
        self.istek("POST", "/api/isler/kaydet", {"is": job(ad="Etkin kullanıcı sayısı — Şehir")})
        taslak = {"aciklama": "GA4 taslağı", "isler": [
            {"ad": "Etkin kullanıcı sayısı — Şehir", "aciklama": "Kaynak: Ana Sayfa", "property_ref": "PROP_PROD",
             "metrikler": ["activeUsers"], "boyutlar": ["city"], "tarih": {"tur": "dun"}, "etkin": False},
            {"ad": "Bozuk", "property_ref": "PROP_PROD", "metrikler": ["bir şey yanlış"], "tarih": {"tur": "dun"}},
        ]}
        kod, r = self.istek("POST", "/api/isler/ice-aktar", {"icerik": json.dumps(taslak, ensure_ascii=False)})
        self.assertEqual(kod, 200)
        self.assertEqual(r["eklenen"], ["Etkin kullanıcı sayısı — Şehir (2)"])
        self.assertEqual(len(r["atlanan"]), 1)
        self.assertEqual(len(r["isler"]), 2)
        self.assertEqual(self.istek("POST", "/api/isler/ice-aktar", {"icerik": "{bozuk"})[0], 400)

    def test_meta_dogrulama_calistirma_csv(self):
        kod, m = self.istek("GET", "/api/meta?kaynak=sentetik&property=PROP_PROD")
        self.assertEqual(kod, 200)
        self.assertIn("activeUsers", [x["api"] for x in m["metrikler"]])
        self.assertTrue(any(x["ozel"] for x in m["boyutlar"]))
        kod, r = self.istek("POST", "/api/dogrula", {"is": job(metrikler=["activeUser"]), "kaynak": "sentetik"})
        self.assertIn("Benzer: activeUsers", r["hatalar"][0])
        kod, r = self.istek("POST", "/api/dogrula", {"is": job(), "kaynak": "sentetik"})
        self.assertEqual(r["hatalar"], [])
        self.assertEqual(self.istek("POST", "/api/calistir", {"kaynak": "sentetik"})[0], 400)  # iş yok
        self.istek("POST", "/api/isler/kaydet", {"is": job(ad="A", boyutlar=["platform"])})
        self.istek("POST", "/api/isler/kaydet", {"is": job(ad="B", property_ref="SENTETIK_YETKI")})
        kod, o = self.istek("POST", "/api/calistir", {"kaynak": "sentetik"})
        self.assertEqual((kod, o["kaynak"], o["tamam"], o["hata"]), (200, "sentetik", 1, 1))
        self.assertEqual(len(o["sonuclar"][0]["satirlar"]), 3)
        kod, o = self.istek("POST", "/api/calistir", {"kaynak": "sentetik", "adlar": ["a"]})
        self.assertEqual(len(o["sonuclar"]), 1)
        kod, csv = self.istek("GET", "/api/csv")
        self.assertTrue(csv["ad"].startswith("ga4-sonuc-sentetik-"))
        self.assertTrue(csv["icerik"].startswith("﻿calisma;zaman;kaynak"))
        self.assertEqual(self.istek("POST", "/api/calistir", {"kaynak": "baska"})[0], 400)
        # GA4 seçilmiş ama ayar yok: anlaşılır hata.
        kod, r = self.istek("POST", "/api/calistir", {"kaynak": "ga4"})
        self.assertEqual(kod, 400)
        self.assertIn("ayar dosyası yok", r["error"])


@unittest.skipUnless(OPENSSL, "OpenSSL bulunamadı")
class KurulumTestleri(SunucuTemel):
    def anahtar(self):
        klasor = self.tmp / "k"
        klasor.mkdir()
        subprocess.run([OPENSSL, "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(klasor / "k.pem")],
                       check=True, capture_output=True)
        return json.dumps({"type": "service_account", "client_email": EPOSTA, "private_key": (klasor / "k.pem").read_text(),
                           "token_uri": "https://oauth2.googleapis.com/token"})

    def test_kurulum_ve_baglanti_testi(self):
        kod, r = self.istek("POST", "/api/kurulum", {"anahtar": "{}", "property_id": "1"})
        self.assertEqual(kod, 400)
        anahtar = self.anahtar()
        kod, r = self.istek("POST", "/api/kurulum", {"anahtar": anahtar, "property_ref": "PROP_PROD", "property_id": "G-ABC"})
        self.assertEqual(kod, 400)
        kod, r = self.istek("POST", "/api/kurulum", {"anahtar": anahtar, "property_ref": "PROP_PROD", "property_id": "123456789"})
        self.assertEqual(kod, 200, r)
        self.assertEqual(r["hizmet_hesabi"], EPOSTA)
        self.assertTrue(r["baglanti"]["kurulu"])
        self.assertEqual([p.name for p in (self.tmp / "veri" / "anahtar").iterdir()], ["ga4-hizmet-hesabi.json"])  # geçici dosya silindi
        with mock.patch("urllib.request.build_opener", return_value=SahteGoogle()):
            kod, t = self.istek("POST", "/api/baglanti", {})
        self.assertEqual(kod, 200)
        self.assertTrue(t["basarili"], t)
        self.assertEqual([a["ad"] for a in t["adimlar"]], ["Ayar dosyası", "Hizmet hesabı", "Google girişi", "Mülk PROP_PROD"])
        self.assertIn("1 özel tanım", t["adimlar"][-1]["mesaj"])
        self.assertTrue(self.istek("GET", "/api/durum")[1]["baglanti"]["son_test"]["basarili"])
        with mock.patch("urllib.request.build_opener", return_value=SahteGoogle(metadata_hatasi="PERMISSION_DENIED")):
            kod, t = self.istek("POST", "/api/baglanti", {})
        self.assertFalse(t["basarili"])
        self.assertIn("Görüntüleyen", t["adimlar"][-1]["mesaj"])
        self.assertNotIn("123456789", json.dumps(t))


if __name__ == "__main__":
    unittest.main()
