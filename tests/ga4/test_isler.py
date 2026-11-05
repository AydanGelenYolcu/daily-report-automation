import json
import unittest

from netmera_automation.ga4 import isler as I
from tests.ga4.helpers import SAMPLE, TODAY, TempDataCase, job


class JobTests(unittest.TestCase):
    def normalize(self, **changes):
        return I.normalize_job(job(**changes), today=TODAY)

    def test_sample_file_is_valid(self):
        jobs = I.load_jobs(SAMPLE, today=TODAY)
        self.assertEqual(len(jobs), 4)
        self.assertTrue(all(j["etkin"] for j in jobs))
        self.assertEqual(jobs[2]["filtreler"][1]["degerler"], ["Android", "iOS"])

    def test_defaults_are_explicit(self):
        result = self.normalize()
        self.assertEqual((result["boyutlar"], result["filtreler"]), ([], []))
        self.assertEqual(result["limit"], I.DEFAULT_LIMIT)
        self.assertFalse(result["kayit_yoksa_sifir"])
        self.assertTrue(result["etkin"])

    def test_rejects_unknown_and_malformed_fields(self):
        bad = [{"metrik": ["activeUsers"]}, {"metrikler": []}, {"metrikler": ["active users"]},
               {"metrikler": ["activeUsers", "activeUsers"]}, {"metrikler": [f"m{i}" for i in range(11)]},
               {"metrikler": "activeUsers"}, {"boyutlar": [f"d{i}" for i in range(10)]},
               {"property_ref": "123456789"}, {"property_ref": "prop"}, {"limit": 0}, {"limit": True},
               {"limit": 10001}, {"etkin": "evet"}, {"ad": " "},
               {"boyutlar": ["deviceCategory"], "kayit_yoksa_sifir": True}]
        for changes in bad:
            with self.subTest(changes=changes), self.assertRaises(I.JobError):
                self.normalize(**changes)
        raw = job()
        del raw["tarih"]
        with self.assertRaises(I.JobError):
            I.normalize_job(raw, today=TODAY)

    def test_custom_dimension_names(self):
        result = self.normalize(boyutlar=["customEvent:kategori", "customUser:uye_tipi"])
        self.assertEqual(result["boyutlar"], ["customEvent:kategori", "customUser:uye_tipi"])

    def test_periods(self):
        ok = [{"tur": "dun"}, {"tur": "bugun"}, {"tur": "son_n_gun", "n": 7},
              {"tur": "aralik", "baslangic": "2026-10-01", "bitis": "2026-10-24"}]
        for period in ok:
            with self.subTest(period=period):
                self.assertEqual(self.normalize(tarih=period)["tarih"], period)
        bad = [{}, {"tur": "gecen_ay"}, {"tur": "dun", "n": 3}, {"tur": "son_n_gun"}, {"tur": "son_n_gun", "n": 0},
               {"tur": "son_n_gun", "n": 366}, {"tur": "son_n_gun", "n": "7"},
               {"tur": "aralik", "baslangic": "2026-10-05", "bitis": "2026-10-01"},
               {"tur": "aralik", "baslangic": "2026-10-01", "bitis": "2026-10-25"},
               {"tur": "aralik", "baslangic": "01.10.2026", "bitis": "2026-10-02"},
               {"tur": "aralik", "baslangic": "2015-01-01", "bitis": "2015-12-01"}, "dun"]
        for period in bad:
            with self.subTest(period=period), self.assertRaises(I.JobError):
                self.normalize(tarih=period)

    def test_filters(self):
        result = self.normalize(filtreler=[
            {"alan": "platform", "operator": "IN_LIST", "degerler": ["web", "iOS"]},
            {"alan": "eventName", "operator": "EXACT", "deger": " purchase ", "haric": True, "harf_duyarli": True}])
        self.assertEqual(result["filtreler"][0], {"alan": "platform", "operator": "IN_LIST", "harf_duyarli": False,
                                                  "haric": False, "degerler": ["web", "iOS"]})
        self.assertEqual(result["filtreler"][1]["deger"], "purchase")
        self.assertIn("DEĞİL", I.describe(result))
        bad = [{"alan": "platform", "operator": "IN_LIST", "deger": "web"},
               {"alan": "platform", "operator": "EXACT", "degerler": ["web"]},
               {"alan": "platform", "operator": "IN_LIST", "degerler": []},
               {"alan": "platform", "operator": "IN_LIST", "degerler": ["web", "web"]},
               {"alan": "platform", "operator": "Equals", "deger": "web"},
               {"alan": "platform", "operator": "EXACT"},
               {"alan": "event name", "operator": "EXACT", "deger": "x"},
               {"alan": "platform", "op": "EXACT", "deger": "x"},
               {"alan": "platform", "operator": "EXACT", "deger": "x", "haric": "evet"}]
        for item in bad:
            with self.subTest(item=item), self.assertRaises(I.JobError):
                self.normalize(filtreler=[item])

    def test_select(self):
        jobs = [I.normalize_job(job(ad="A"), today=TODAY), I.normalize_job(job(ad="B", etkin=False), today=TODAY)]
        self.assertEqual([j["ad"] for j in I.select(jobs)], ["A"])
        self.assertEqual([j["ad"] for j in I.select(jobs, " b ")], ["B"])
        with self.assertRaises(I.JobError):
            I.select(jobs, "C")
        with self.assertRaises(I.JobError):
            I.select(jobs[1:])


class FileTests(TempDataCase):
    def write(self, content):
        path = self.tmp / "isler.json"
        path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
        return path

    def test_duplicate_names_and_bad_files(self):
        with self.assertRaisesRegex(I.JobError, "iki kez"):
            I.load_jobs(self.write({"isler": [job(ad="Rapor"), job(ad="rapor")]}), today=TODAY)
        with self.assertRaisesRegex(I.JobError, "satır 1"):
            I.load_jobs(self.write("{bozuk"), today=TODAY)
        for content in ({"isler": []}, {"islem": [job()]}, []):
            with self.subTest(content=content), self.assertRaises(I.JobError):
                I.load_jobs(self.write(content), today=TODAY)
        with self.assertRaisesRegex(I.JobError, "bulunamadı"):
            I.load_jobs(self.tmp / "yok.json")


if __name__ == "__main__":
    unittest.main()
