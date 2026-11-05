"""Zaman kırılımı (gün/hafta/ay/yıl) ve hafta/ay/yıl tarih türleri. Yalnız sentetik veri."""
from datetime import date
import unittest

from netmera_automation.ga4 import isler as I, meta, rapor as R
from netmera_automation.ga4.sentetik import SyntheticClient
from tests.ga4.helpers import job

PAZAR = date(2026, 10, 25)  # pazar; ISO haftası 43


def normalize(**changes):
    return I.normalize_job(job(**changes), today=PAZAR)


def run(today=PAZAR, **changes):
    return R.run_job(I.normalize_job(job(**changes), today=today), SyntheticClient(today), today=today)


class TarihTurleri(unittest.TestCase):
    def test_tam_donemler_ve_donem_basindan(self):
        ornekler = [
            (PAZAR, {"tur": "son_n_hafta", "n": 2}, ("2026-10-05", "2026-10-18")),
            (date(2026, 10, 19), {"tur": "son_n_hafta", "n": 1}, ("2026-10-12", "2026-10-18")),  # pazartesi
            (PAZAR, {"tur": "son_n_ay", "n": 3}, ("2026-07-01", "2026-09-30")),
            (date(2026, 2, 10), {"tur": "son_n_ay", "n": 3}, ("2025-11-01", "2026-01-31")),  # yıl geçişi
            (date(2024, 3, 5), {"tur": "son_n_ay", "n": 1}, ("2024-02-01", "2024-02-29")),  # artık yıl
            (PAZAR, {"tur": "son_n_yil", "n": 2}, ("2024-01-01", "2025-12-31")),
            (PAZAR, {"tur": "ay_basindan"}, ("2026-10-01", "2026-10-24")),
            (date(2026, 11, 1), {"tur": "ay_basindan"}, ("2026-10-01", "2026-10-31")),  # ayın 1'i: önceki ayın tamamı
            (PAZAR, {"tur": "yil_basindan"}, ("2026-01-01", "2026-10-24")),
            (date(2027, 1, 1), {"tur": "yil_basindan"}, ("2026-01-01", "2026-12-31")),
        ]
        for bugun, tarih, beklenen in ornekler:
            with self.subTest(bugun=bugun, tarih=tarih):
                bas, bit = R.resolve_dates(I.normalize_period(tarih, "t", today=bugun), bugun)
                self.assertEqual((bas.isoformat(), bit.isoformat()), beklenen)
                self.assertEqual(R.date_range(tarih, bugun), {"startDate": beklenen[0], "endDate": beklenen[1]})

    def test_sinirlar_ve_metin(self):
        for tarih in ({"tur": "son_n_ay", "n": 37}, {"tur": "son_n_yil", "n": 0}, {"tur": "son_n_hafta"},
                      {"tur": "ay_basindan", "n": 3}):
            with self.subTest(tarih=tarih), self.assertRaises(I.JobError):
                I.normalize_period(tarih, "t", today=PAZAR)
        self.assertEqual(I.period_text({"tur": "son_n_ay", "n": 6}), "Son 6 tam ay")
        self.assertEqual(R.date_range({"tur": "son_n_gun", "n": 7}), {"startDate": "7daysAgo", "endDate": "yesterday"})  # değişmedi


class ZamanKirilimi(unittest.TestCase):
    def test_dogrulama(self):
        self.assertNotIn("zaman", normalize())  # yazılmazsa alan eklenmez (eski iş dosyaları aynı kalır)
        self.assertEqual(normalize(zaman="ay")["zaman"], "ay")
        kotu = [{"zaman": "aylik"}, {"zaman": "gun", "boyutlar": ["date"]}, {"zaman": "yil", "kayit_yoksa_sifir": True},
                {"zaman": "gun", "boyutlar": [f"customEvent:b{i}" for i in range(9)]}]
        for changes in kotu:
            with self.subTest(changes=changes), self.assertRaises(I.JobError):
                normalize(**changes)
        self.assertIn("ay ay", I.describe(normalize(zaman="ay", boyutlar=["platform"])))

    def test_istek_zaman_boyutu_basta_ve_sirali(self):
        body = R.build_request(normalize(zaman="ay", boyutlar=["platform"], tarih={"tur": "son_n_ay", "n": 3}), PAZAR)
        self.assertEqual([d["name"] for d in body["dimensions"]], ["yearMonth", "platform"])
        self.assertEqual(body["orderBys"], [{"dimension": {"dimensionName": "yearMonth"}}, {"metric": {"metricName": "activeUsers"}, "desc": True}])
        self.assertEqual(body["dateRanges"], [{"startDate": "2026-07-01", "endDate": "2026-09-30"}])
        govde = {"gun": "date", "hafta": "isoYearIsoWeek", "ay": "yearMonth", "yil": "year"}
        for zaman, boyut in govde.items():
            self.assertEqual(R.build_request(normalize(zaman=zaman), PAZAR)["dimensions"], [{"name": boyut}])

    def test_sentetik_donemler(self):
        aylik = run(zaman="ay", metrikler=["activeUsers", "sessions"], tarih={"tur": "son_n_ay", "n": 3})
        self.assertEqual(aylik["durum"], "tamam", aylik["hata"])
        self.assertEqual(aylik["boyutlar"], ["yearMonth"])
        self.assertEqual([r["boyutlar"]["yearMonth"] for r in aylik["satirlar"]], ["202607", "202608", "202609"])
        gunluk = run(zaman="gun", boyutlar=["platform"], tarih={"tur": "son_n_gun", "n": 7})
        self.assertEqual(len(gunluk["satirlar"]), 21)
        self.assertEqual(gunluk["satirlar"][0]["boyutlar"]["date"], "20261018")
        ilk_gun = [r["metrikler"]["activeUsers"] for r in gunluk["satirlar"][:3]]
        self.assertEqual(ilk_gun, sorted(ilk_gun, reverse=True))  # gün içinde büyükten küçüğe (GA4 ekranı gibi)
        self.assertIsNotNone(gunluk["toplam"])
        haftalik = run(zaman="hafta", tarih={"tur": "son_n_hafta", "n": 2})
        self.assertEqual([r["boyutlar"]["isoYearIsoWeek"] for r in haftalik["satirlar"]], ["202641", "202642"])
        yillik = run(zaman="yil", tarih={"tur": "son_n_yil", "n": 2})
        self.assertEqual([r["boyutlar"]["year"] for r in yillik["satirlar"]], ["2024", "2025"])
        # Yıllık oturum (toplanabilir metrik) günlükten büyük; kullanıcı sayısı günlere oranla toplanmaz
        bir_gun = run(tarih={"tur": "dun"}, metrikler=["sessions"])["satirlar"][0]["metrikler"]["sessions"]
        self.assertGreater(yillik["satirlar"][0]["metrikler"]["activeUsers"], 0)
        self.assertGreater(run(zaman="yil", metrikler=["sessions"], tarih={"tur": "son_n_yil", "n": 1})["satirlar"][0]["metrikler"]["sessions"], bir_gun)

    def test_uyumluluk_denetimi_zaman_boyutunu_gonderir(self):
        client = SyntheticClient(PAZAR)
        gonderilen = []
        asil = client.check_compatibility
        client.check_compatibility = lambda ref, body: gonderilen.append(body) or asil(ref, body)
        self.assertEqual(meta.validate(normalize(zaman="hafta", boyutlar=["platform"]), client, meta.fetch(client, "PROP_TEST")), [])
        self.assertEqual([d["name"] for d in gonderilen[0]["dimensions"]], ["isoYearIsoWeek", "platform"])


if __name__ == "__main__":
    unittest.main()
