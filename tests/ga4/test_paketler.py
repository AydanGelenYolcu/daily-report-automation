"""Hazır rapor kütüphanesi, GA4 adları ve paketler (grup halinde çekme, eşitleme, hariç tutma). Yalnız sentetik veri."""
import json
from unittest import mock

from netmera_automation.ga4 import adlar, hazir_raporlar as H, isler, meta, paketler, projeler, store
from netmera_automation.ga4.sentetik import SyntheticClient
from tests.ga4.helpers import TempDataCase, job
from tests.ga4.test_server import SunucuTemel

EDINME = "kutuphane:yasam-dongusu/edinme"


def cekilebilir(p):
    return [r for r in p["raporlar"] if not r.get("desteklenmiyor")]


def taslak(isler_, raporlar=None, etiketler=None):
    veri = {"aciklama": "sentetik taslak", "isler": isler_}
    if raporlar is not None:
        veri["raporlar"] = raporlar
    if etiketler is not None:
        veri["etiketler"] = etiketler
    return json.dumps(veri, ensure_ascii=False)


class Adlar(TempDataCase):
    def test_rapor_adi_onde_kesfet_ve_api_aranabilir(self):
        b = adlar.bilgi("activeUsers", "Active users")
        self.assertEqual(b["ad"], "Etkin kullanıcı sayısı")
        self.assertEqual(b["kesfet"], "Etkin kullanıcılar")
        self.assertIn("Active users", b["diger"])

    def test_ogrenilen_ad_one_gecer_ve_ozel_alan_kendi_adini_korur(self):
        self.assertEqual(adlar.ogren({"Aktif kullanıcılar": "activeUsers", "kötü": "1 geçersiz", "Kampanyam": "customEvent:kampanya"}), 2)
        b = adlar.bilgi("activeUsers", "Active users")
        self.assertEqual(b["ad"], "Aktif kullanıcılar")
        self.assertIn("Etkin kullanıcı sayısı", [b["kesfet"], *b["diger"]])
        self.assertEqual(adlar.bilgi("customEvent:kampanya", "kampanya", ozel=True)["ad"], "Kampanyam")
        self.assertEqual(adlar.ogren({"Aktif kullanıcılar": "activeUsers"}), 0)

    def test_ice_aktarma_etiketleri_ogrenir(self):
        sonuc = store.import_jobs(taslak([job(ad="Tarama 1")], etiketler={"Etkin kullanıcı": "activeUsers"}))
        self.assertEqual(sonuc["ogrenilen_ad"], 1)
        self.assertEqual(adlar.bilgi("activeUsers")["ad"], "Etkin kullanıcı")


class Kutuphane(TempDataCase):
    def test_her_rapor_sentetik_semayla_gecerli_ve_uyumlu(self):
        client = SyntheticClient()
        m = meta.fetch(client, "PROP_TEST")
        for p in H.KUTUPHANE:
            for r in p["raporlar"]:
                if r.get("desteklenmiyor"):
                    continue
                for boyut in r.get("secenekler") or [None]:
                    j = isler.normalize_job(H.is_tanimi(p, r, boyut=boyut, property_ref="PROP_TEST"))
                    self.assertEqual(meta.validate(j, client, m), [], (p["kimlik"], r["kimlik"], boyut))
                    self.assertLessEqual(len(j["metrikler"]), 10)

    def test_kimlikler_benzersiz_ve_adlar_turkce(self):
        kimlikler = [p["kimlik"] for p in H.KUTUPHANE]
        self.assertEqual(len(kimlikler), len(set(kimlikler)))
        for p in H.KUTUPHANE:
            rk = [r["kimlik"] for r in p["raporlar"]]
            self.assertEqual(len(rk), len(set(rk)))
        b, m = H.butun_alanlar()
        eksik = sorted(a for a in b | m if a not in adlar.SOZLUK)
        self.assertEqual(eksik, [], "kütüphane alanlarının Türkçe adı olmalı")


class Paketler(TempDataCase):
    def setUp(self):
        super().setUp()
        self.pid = projeler.varsayilan()
        self.meta = meta.fetch(SyntheticClient(), "PROP_PROD")

    def isler(self):
        return store.load(self.pid)

    def test_grup_cekilince_butun_raporlar_ve_paket_kaydi(self):
        s = paketler.cek(self.pid, [{"paket": EDINME}], meta=self.meta)
        p = H.paket("yasam-dongusu/edinme")
        self.assertFalse(s["onay_gerekli"])
        self.assertEqual(len(s["eklenen"]), len(cekilebilir(p)))
        self.assertTrue(all(j["paket"]["kimlik"] == EDINME for j in self.isler()))
        kayit = projeler.oku(self.pid)["paketler"]
        self.assertEqual([(k["kimlik"], k["ad"], k["kaynak"]) for k in kayit], [(EDINME, "Life cycle › Edinme", "kutuphane")])
        s2 = paketler.cek(self.pid, [{"paket": EDINME}], meta=self.meta)  # ikinci çekiş çoğaltmaz
        self.assertEqual(s2["eklenen"], [])
        self.assertEqual(len(s2["zaten"]), len(cekilebilir(p)))

    def test_ad_cakismasi_numaralanir_ve_desteklenmeyen_atlanir(self):
        store.upsert(job(ad="Trafik edinme"), proje=self.pid)
        paketler.cek(self.pid, [{"paket": EDINME, "raporlar": ["trafik-edinme"]}, {"paket": "kutuphane:yasam-dongusu/elde-tutma"}],
                     meta=self.meta)
        self.assertEqual([j["ad"] for j in self.isler()], ["Trafik edinme", "Trafik edinme (2)"])

    def test_mulkte_olmayan_alan_onay_ister(self):
        eksik_meta = {**self.meta, "boyutlar": {k: v for k, v in self.meta["boyutlar"].items() if k != "sessionCampaignName"}}
        s = paketler.cek(self.pid, [{"paket": EDINME}], meta=eksik_meta)
        self.assertTrue(s["onay_gerekli"])
        self.assertEqual(self.isler(), [])
        self.assertIn("sessionCampaignName", s["eksikler"][0]["alanlar"][0])
        paketler.cek(self.pid, [{"paket": EDINME}], meta=eksik_meta, eksik_onay=True)
        kampanya = next(j for j in self.isler() if j["paket"]["rapor"] == "google-disi-kampanyalar")
        self.assertEqual(kampanya["boyutlar"], [])  # alan çıkarıldı, metrikler kaldı

    def test_esitleme_yeni_raporu_ekler_harici_ve_kullanici_degisikligini_korur(self):
        paketler.cek(self.pid, [{"paket": EDINME, "raporlar": ["trafik-edinme", "kullanici-edinme"]}], meta=self.meta)
        silinen = next(j for j in self.isler() if j["paket"]["rapor"] == "kullanici-edinme")
        store.delete(silinen["ad"], proje=self.pid)
        paketler.is_silindi(self.pid, silinen)
        degisen = next(j for j in self.isler() if j["paket"]["rapor"] == "trafik-edinme")
        store.upsert({**degisen, "metrikler": ["sessions"]}, degisen["ad"], proje=self.pid)
        s = paketler.esitle(self.pid, self.meta)
        p = H.paket("yasam-dongusu/edinme")
        beklenen = {r["kimlik"] for r in cekilebilir(p)} - {"kullanici-edinme"}
        self.assertEqual({j["paket"]["rapor"] for j in self.isler()}, beklenen)
        self.assertEqual(len(s["eklenen"]), len(beklenen) - 1)
        self.assertEqual(next(j for j in self.isler() if j["paket"]["rapor"] == "trafik-edinme")["metrikler"], ["sessions"])
        self.assertEqual(paketler.esitle(self.pid, self.meta)["eklenen"], [])

    def test_kutuphaneye_eklenen_rapor_esitlenir_kaldirilan_isaretlenir(self):
        paketler.cek(self.pid, [{"paket": EDINME}], meta=self.meta)
        p = H.paket("yasam-dongusu/edinme")
        yeni = {"kimlik": "yeni-rapor", "ad": "Yeni rapor", "boyutlar": ["deviceCategory"], "metrikler": ["sessions"]}
        degismis = {**p, "raporlar": [*p["raporlar"][1:], yeni]}
        with mock.patch.object(H, "KUTUPHANE", [degismis if x is p else x for x in H.KUTUPHANE]):
            s = paketler.esitle(self.pid, self.meta)
            self.assertEqual([e["rapor"] for e in s["eklenen"]], ["Yeni rapor"])
            self.assertEqual(len(s["isaretlenen"]), 1)
            self.assertEqual(paketler.ozet(self.pid)[0]["yeni"], 0)
        isaretli = [j for j in self.isler() if j["paket"].get("durum") == "kutuphanede_yok"]
        self.assertEqual([j["paket"]["rapor"] for j in isaretli], [p["raporlar"][0]["kimlik"]])
        paketler.esitle(self.pid, self.meta)  # rapor kütüphaneye geri gelince işaret kalkar; testteki rapor artık yok
        self.assertEqual([j["paket"]["rapor"] for j in self.isler() if j["paket"].get("durum")], ["yeni-rapor"])

    def test_duzenleyici_paket_gondermese_de_is_pakette_kalir(self):
        paketler.cek(self.pid, [{"paket": EDINME, "raporlar": ["trafik-edinme"]}], meta=self.meta)
        j = self.isler()[0]
        store.upsert({**{k: v for k, v in j.items() if k != "paket"}, "ad": "Kanallar"}, j["ad"], proje=self.pid)
        self.assertEqual(self.isler()[0]["paket"]["rapor"], "trafik-edinme")

    def test_paket_sil(self):
        paketler.cek(self.pid, [{"paket": EDINME}], meta=self.meta)
        store.upsert(job(ad="Elle iş"), proje=self.pid)
        paketler.paket_sil(self.pid, EDINME)
        self.assertEqual([j["ad"] for j in self.isler()], ["Elle iş"])
        self.assertEqual(projeler.oku(self.pid)["paketler"], [])
        with self.assertRaises(isler.JobError):
            paketler.paket_sil(self.pid, EDINME)


class PaketUclari(SunucuTemel):
    def test_hazir_raporlar_cek_esitle_sil(self):
        d, veri = self.istek("GET", "/api/hazir-raporlar")
        self.assertEqual(d, 200)
        self.assertIsNone(veri["meta_hatasi"])
        self.assertIn(EDINME, [p["kimlik"] for p in veri["agac"]])
        d, veri = self.istek("POST", "/api/paketler/cek", {"secim": [{"paket": EDINME}], "tarih": {"tur": "dun"}})
        self.assertEqual(d, 200, veri)
        self.assertTrue(veri["eklenen"])
        self.assertEqual(veri["paketler"][0]["rapor_sayisi"], len(veri["eklenen"]))
        self.assertTrue(all(j["tarih"] == {"tur": "dun"} for j in veri["isler"]))
        d, veri = self.istek("POST", "/api/isler/sil", {"ad": veri["isler"][0]["ad"]})
        self.assertEqual(d, 200)
        d, veri = self.istek("POST", "/api/paketler/esitle", {})
        self.assertEqual(veri["eklenen"], [])  # silinen geri gelmez
        d, veri = self.istek("POST", "/api/isler/etkin", {"adlar": [j["ad"] for j in veri["isler"]], "etkin": False})
        self.assertFalse(any(j["etkin"] for j in veri["isler"]))
        d, veri = self.istek("POST", "/api/paketler/sil", {"kimlik": EDINME})
        self.assertEqual((d, veri["isler"], veri["paketler"]), (200, [], []))
        d, veri = self.istek("POST", "/api/paketler/cek", {"secim": [{"paket": "kutuphane:yok/yok"}]})
        self.assertEqual(d, 400)
