"""Projeler: eski iş dosyasının taşınması, oluşturma/yeniden adlandırma/silme, projeler arası ayrım, taslak dışa/içe aktarma,
sunucu uç noktaları. Gerçek Windows görevine dokunmaz (görev işlemleri sahte)."""
import json
import unittest
from unittest import mock

from netmera_automation.ga4 import isler, projeler, store, zamanlayici
from tests.ga4.helpers import TempDataCase, job
from tests.ga4.test_server import SunucuTemel

TABLO = "1SentetikTabloKimligi_0123456789abc"


class ProjeDeposu(TempDataCase):
    @unittest.skip("Birleşik uygulamada proje = Netmera projesi (oluşturma/silme/taşıma Netmera'da; bkz. tests/test_ga4_merge.py).")
    def test_eski_is_dosyasi_genel_projesine_tasinir(self):
        veri = self.tmp / "veri"
        veri.mkdir()
        (veri / "isler.json").write_text(json.dumps({"isler": [job(ad="Eski iş")]}), encoding="utf-8")
        (veri / "zamanlama.json").write_text(json.dumps({"saat": "07:15", "gunler": ["MON"]}), encoding="utf-8")
        self.assertEqual([p["ad"] for p in projeler.listele()], ["Genel"])
        self.assertEqual([j["ad"] for j in store.load()], ["Eski iş"])
        self.assertFalse((veri / "isler.json").exists())
        self.assertEqual(projeler.oku("genel")["zamanlama"], {"saat": "07:15", "gunler": ["MON"]})
        self.assertEqual(store.path(), veri / "projeler" / "genel" / "isler.json")

    @unittest.skip("Birleşik uygulamada proje = Netmera projesi (oluşturma/silme/taşıma Netmera'da; bkz. tests/test_ga4_merge.py).")
    def test_es_zamanli_ilk_acilis_tek_tasima(self):
        """Arayüz ilk açılışta birkaç isteği aynı anda gönderir; taşıma bir kez ve hatasız yapılmalı."""
        import threading
        veri = self.tmp / "veri"
        veri.mkdir()
        (veri / "isler.json").write_text(json.dumps({"isler": [job(ad="Eski iş")]}), encoding="utf-8")
        hatalar, engel = [], threading.Barrier(8)

        def ac():
            try:
                engel.wait()
                projeler.listele()
                store.load()
            except Exception as exc:  # noqa: BLE001 — testte her hata yakalanır
                hatalar.append(repr(exc))
        is_parcaciklari = [threading.Thread(target=ac) for _ in range(8)]
        for t in is_parcaciklari:
            t.start()
        for t in is_parcaciklari:
            t.join(10)
        self.assertEqual(hatalar, [])
        self.assertEqual([p["id"] for p in projeler.listele()], ["genel"])
        self.assertEqual([j["ad"] for j in store.load()], ["Eski iş"])

    @unittest.skip("Birleşik uygulamada proje = Netmera projesi (oluşturma/silme/taşıma Netmera'da; bkz. tests/test_ga4_merge.py).")
    def test_olustur_adlandir_ayir_ve_sil(self):
        self.assertEqual(projeler.varsayilan(), "genel")
        aylik = projeler.olustur("Aylık Rapor — Şube", "Ay sonu")
        self.assertEqual(aylik["id"], "aylik-rapor-sube")
        with self.assertRaises(projeler.ProjeHatasi):
            projeler.olustur("aylık rapor — şube")  # aynı ad (büyük/küçük harf farkı yok)
        self.assertEqual(projeler.olustur("Aylık Rapor — Şube!")["id"], "aylik-rapor-sube-2")  # kimlik çakışırsa numaralanır
        store.save([job(ad="Genel iş")])
        store.save([job(ad="Aylık iş"), job(ad="İkinci")], "aylik-rapor-sube")
        self.assertEqual([j["ad"] for j in store.load("genel")], ["Genel iş"])
        self.assertEqual(len(store.load("aylik-rapor-sube")), 2)
        g = projeler.guncelle("aylik-rapor-sube", {"ad": "Aylık", "sheets": {"tablo": f"https://docs.google.com/spreadsheets/d/{TABLO}/edit", "guncelle_gun": 5}})
        self.assertEqual((g["id"], g["ad"], g["sheets"]), ("aylik-rapor-sube", "Aylık", {"tablo": TABLO, "guncelle_gun": 5, "toplam": True}))
        with self.assertRaises(projeler.ProjeHatasi):
            projeler.guncelle("aylik-rapor-sube", {"sheets": {"guncelle_gun": 40}})
        with self.assertRaises(projeler.ProjeHatasi):
            projeler.guncelle("aylik-rapor-sube", {"ad": "genel"})
        kaldirilan = []
        projeler.sil("aylik-rapor-sube", gorev_kaldir=lambda pid: kaldirilan.append(pid) or {"kurulu": False})
        self.assertEqual(kaldirilan, ["aylik-rapor-sube"])
        self.assertEqual(sorted(p["id"] for p in projeler.listele()), ["aylik-rapor-sube-2", "genel"])
        projeler.sil("aylik-rapor-sube-2", gorev_kaldir=lambda pid: {"kurulu": False})
        with self.assertRaises(projeler.ProjeHatasi):
            projeler.oku("aylik-rapor-sube")
        with self.assertRaises(projeler.ProjeHatasi):
            projeler.sil("genel", gorev_kaldir=lambda pid: {"kurulu": False})  # son proje silinmez
        with self.assertRaises(projeler.ProjeHatasi):
            projeler.klasor("../genel")

    @unittest.skip("Birleşik uygulamada proje = Netmera projesi (oluşturma/silme/taşıma Netmera'da; bkz. tests/test_ga4_merge.py).")
    def test_gorev_okunamazsa_proje_silinmez(self):
        p = projeler.olustur("Deneme")
        store.save([job(ad="Kalmalı")], p["id"])

        def okunamadi(pid):
            raise zamanlayici.ZamanlamaHatasi("Windows görev durumu okunamadı.")
        with self.assertRaises(zamanlayici.ZamanlamaHatasi):
            projeler.sil(p["id"], gorev_kaldir=okunamadi)
        self.assertEqual([j["ad"] for j in store.load(p["id"])], ["Kalmalı"])
        with self.assertRaises(projeler.ProjeHatasi):
            projeler.sil(p["id"], gorev_kaldir=lambda pid: {"kurulu": True})  # görev silinemediyse proje de silinmez
        self.assertTrue(projeler.klasor(p["id"]).exists())

    def test_taslak_disa_ve_ice_aktarma(self):
        store.save([job(ad="Platform", boyutlar=["platform"], sheets={"sekme": "Platform"}), job(ad="Toplam", etkin=False)])
        taslak = store.export_jobs()
        self.assertTrue(taslak["ad"].startswith("analytics-taslak-"))  # birleşik: varsayılan proje Netmera projesi
        icerik = json.loads(taslak["icerik"])
        self.assertEqual([j["ad"] for j in icerik["isler"]], ["Platform", "Toplam"])
        self.assertNotIn("satirlar", taslak["icerik"])  # yalnız tanım, sonuç yok
        yeni = projeler.olustur("Kopya")
        sonuc = store.import_jobs(taslak["icerik"], yeni["id"])
        self.assertEqual(sonuc["eklenen"], ["Platform", "Toplam"])
        self.assertEqual(store.load(yeni["id"])[0]["sheets"], {"sekme": "Platform"})
        self.assertEqual(store.import_jobs(taslak["icerik"], yeni["id"])["eklenen"], ["Platform (2)", "Toplam (2)"])
        with self.assertRaises(isler.JobError):
            store.export_jobs(projeler.olustur("Boş")["id"])


class SunucuProjeleri(SunucuTemel):
    @unittest.skip("Birleşik uygulamada proje = Netmera projesi (oluşturma/silme/taşıma Netmera'da; bkz. tests/test_ga4_merge.py).")
    def test_proje_uclari_ve_ayrim(self):
        kod, d = self.istek("GET", "/api/durum")
        self.assertEqual((kod, d["proje"]["id"], [p["ad"] for p in d["projeler"]]), (200, "genel", ["Genel"]))
        kod, r = self.istek("POST", "/api/projeler/olustur", {"ad": "Günlük Rapor"})
        self.assertEqual((kod, r["proje"]["id"]), (200, "gunluk-rapor"))
        self.assertEqual(self.istek("POST", "/api/projeler/olustur", {"ad": ""})[0], 400)
        self.istek("POST", "/api/isler/kaydet", {"proje": "gunluk-rapor", "is": job(ad="Rapor işi", sheets={})})
        self.assertEqual(self.istek("GET", "/api/isler?proje=genel")[1]["isler"], [])
        self.assertEqual([j["ad"] for j in self.istek("GET", "/api/isler?proje=gunluk-rapor")[1]["isler"]], ["Rapor işi"])
        kod, d = self.istek("GET", "/api/durum?proje=gunluk-rapor")
        self.assertEqual((d["proje"]["ad"], d["proje"]["is_sayisi"], d["proje"]["sheets_is"]), ("Günlük Rapor", 1, 1))
        self.assertEqual(self.istek("GET", "/api/durum?proje=yok")[1]["proje"]["id"], "genel")  # bilinmeyen → varsayılan
        kod, r = self.istek("POST", "/api/projeler/guncelle", {"proje": "gunluk-rapor", "sheets": {"tablo": TABLO, "toplam": False}})
        self.assertEqual((kod, r["proje"]["sheets"]["tablo"], r["proje"]["sheets"]["toplam"]), (200, TABLO, False))
        kod, t = self.istek("GET", "/api/isler/taslak?proje=gunluk-rapor")
        self.assertEqual((kod, t["is_sayisi"]), (200, 1))
        self.istek("POST", "/api/calistir", {"proje": "gunluk-rapor", "kaynak": "sentetik"})
        with mock.patch.object(zamanlayici, "_gorev_oku", lambda pid: None):
            kod, z = self.istek("GET", "/api/zamanlama?proje=gunluk-rapor")
            self.assertEqual((z["isler"][0]["tablo"], z["isler"][0]["toplam"], z["ayar"]["saat"]), (TABLO, False, "09:30"))
            kod, r = self.istek("POST", "/api/projeler/sil", {"proje": "gunluk-rapor"})
        self.assertEqual((kod, [p["id"] for p in r["projeler"]], r["varsayilan"]), (200, ["genel"], "genel"))
        self.assertEqual(self.istek("POST", "/api/disari-aktar", {"hedef": "csv"})[0], 400)  # silinen projenin sonucu da gider
        kod, r = self.istek("POST", "/api/projeler/sil", {"proje": "genel"})
        self.assertEqual(kod, 400)
        self.assertIn("En az bir proje", r["error"])
