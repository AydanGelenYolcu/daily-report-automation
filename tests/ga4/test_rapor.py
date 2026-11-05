from datetime import date, datetime
import unittest

from netmera_automation.ga4 import isler as I, rapor as R
from netmera_automation.ga4.istemci import ClientError
from tests.ga4.helpers import TODAY, job


class Canned:
    kaynak = "sentetik"

    def __init__(self, response=None, error=None):
        self.response, self.error, self.bodies = response, error, []

    def run_report(self, ref, body):
        self.bodies.append(body)
        if self.error:
            raise self.error
        return self.response


def response(rows, dims=(), metrics=("activeUsers",), **extra):
    return {"dimensionHeaders": [{"name": d} for d in dims],
            "metricHeaders": [{"name": m, "type": "TYPE_INTEGER" if m != "engagementRate" else "TYPE_FLOAT"} for m in metrics],
            "rows": [{"dimensionValues": [{"value": v} for v in dv], "metricValues": [{"value": v} for v in mv]} for dv, mv in rows],
            "rowCount": len(rows), **extra}


def normalize(**changes):
    return I.normalize_job(job(**changes), today=TODAY)


class RequestTests(unittest.TestCase):
    def test_dates(self):
        cases = [({"tur": "dun"}, "yesterday", "yesterday", date(2026, 10, 23), date(2026, 10, 23)),
                 ({"tur": "bugun"}, "today", "today", TODAY, TODAY),
                 ({"tur": "son_n_gun", "n": 7}, "7daysAgo", "yesterday", date(2026, 10, 17), date(2026, 10, 23)),
                 ({"tur": "aralik", "baslangic": "2026-10-01", "bitis": "2026-10-05"}, "2026-10-01", "2026-10-05",
                  date(2026, 10, 1), date(2026, 10, 5))]
        for period, start, end, first, last in cases:
            with self.subTest(period=period):
                self.assertEqual(R.date_range(period), {"startDate": start, "endDate": end})
                self.assertEqual(R.resolve_dates(period, TODAY), (first, last))

    def test_build_request(self):
        body = R.build_request(normalize(metrikler=["sessions", "activeUsers"], boyutlar=["deviceCategory"], limit=50))
        self.assertEqual(body["metrics"], [{"name": "sessions"}, {"name": "activeUsers"}])
        self.assertEqual(body["dimensions"], [{"name": "deviceCategory"}])
        self.assertEqual(body["orderBys"], [{"metric": {"metricName": "sessions"}, "desc": True}])
        self.assertEqual(body["metricAggregations"], ["TOTAL"])
        self.assertEqual(body["limit"], 50)
        self.assertTrue(body["returnPropertyQuota"])
        self.assertNotIn("dimensionFilter", body)
        self.assertNotIn("dimensions", R.build_request(normalize()))
        self.assertNotIn("metricAggregations", R.build_request(normalize()))  # tek satırda toplam zaten kendisi

    def test_filters(self):
        single = R.build_request(normalize(filtreler=[{"alan": "eventName", "operator": "EXACT", "deger": "purchase"}]))
        self.assertEqual(single["dimensionFilter"], {"filter": {"fieldName": "eventName", "stringFilter": {
            "matchType": "EXACT", "value": "purchase", "caseSensitive": False}}})
        group = R.build_request(normalize(filtreler=[
            {"alan": "platform", "operator": "IN_LIST", "degerler": ["iOS", "Android"], "harf_duyarli": True},
            {"alan": "country", "operator": "CONTAINS", "deger": "Tur", "haric": True}]))["dimensionFilter"]
        first, second = group["andGroup"]["expressions"]
        self.assertEqual(first["filter"]["inListFilter"], {"values": ["iOS", "Android"], "caseSensitive": True})
        self.assertEqual(second["notExpression"]["filter"]["stringFilter"]["matchType"], "CONTAINS")


class ResponseTests(unittest.TestCase):
    def test_parse_values_and_types(self):
        current = normalize(metrikler=["activeUsers", "engagementRate"], boyutlar=["deviceCategory"])
        rows, warnings, info = R.parse_response(current, response(
            [(["mobile"], ["120", "0.55"])], dims=["deviceCategory"], metrics=["activeUsers", "engagementRate"],
            metadata={"timeZone": "Europe/Istanbul"}))
        self.assertEqual(rows, [{"boyutlar": {"deviceCategory": "mobile"}, "metrikler": {"activeUsers": 120, "engagementRate": 0.55}}])
        self.assertEqual(warnings, [])
        self.assertEqual(info["saat_dilimi"], "Europe/Istanbul")

    def test_warnings(self):
        current = normalize(boyutlar=["country"])
        resp = response([(["(other)"], ["5"])], dims=["country"], metadata={"subjectToThresholding": True,
                        "samplingMetadatas": [{}]}, propertyQuota={"tokensPerDay": {"consumed": 195000, "remaining": 5000}})
        resp["rowCount"] = 3
        _, warnings, info = R.parse_response(current, resp)
        self.assertEqual(warnings, ["esik", "diger_satiri", "ornekleme", "satir_kesildi", "kota_az"])
        self.assertEqual(info["gunluk_kota_kalan"], 5000)

    def test_mismatched_or_broken_response_is_rejected(self):
        current = normalize()
        broken = [response([([], ["1"])], metrics=["sessions"]), response([(["x"], ["1"])], dims=["country"]),
                  response([([], ["bir"])]), response([([], ["1", "2"])]), response([([], ["nan"])], metrics=["engagementRate"]), []]
        broken[4]["metricHeaders"][0]["name"] = "activeUsers"
        for item in broken:
            with self.subTest(item=item), self.assertRaises(ClientError):
                R.parse_response(current, item)


class RunTests(unittest.TestCase):
    def test_empty_result_is_an_error_unless_zero_is_allowed(self):
        empty = {"dimensionHeaders": [], "metricHeaders": [{"name": "activeUsers", "type": "TYPE_INTEGER"}]}
        result = R.run_job(normalize(), Canned(empty), today=TODAY)
        self.assertEqual((result["durum"], result["hata_kodu"], result["satirlar"]), ("hata", "KAYIT_YOK", []))
        result = R.run_job(normalize(kayit_yoksa_sifir=True), Canned(empty), today=TODAY)
        self.assertEqual(result["durum"], "uyari")
        self.assertEqual(result["satirlar"], [{"boyutlar": {}, "metrikler": {"activeUsers": 0}}])
        self.assertEqual(result["uyarilar"], ["sifir_yazildi"])

    def test_errors_never_produce_numbers(self):
        for error, code in ((ClientError("PERMISSION_DENIED"), "PERMISSION_DENIED"), (RuntimeError("iç"), "BEKLENMEYEN")):
            with self.subTest(code=code):
                result = R.run_job(normalize(kayit_yoksa_sifir=True), Canned(error=error), today=TODAY)
                self.assertEqual((result["durum"], result["hata_kodu"], result["satirlar"]), ("hata", code, []))

    def test_ga4_total_row_is_kept_separately(self):
        yanit = response([(["Turkey"], ["90"]), (["Germany"], ["20"])], dims=("country",),
                         totals=[{"dimensionValues": [{"value": "RESERVED_TOTAL"}], "metricValues": [{"value": "105"}]}])
        result = R.run_job(normalize(boyutlar=["country"]), Canned(yanit), today=TODAY)
        self.assertEqual(result["toplam"], {"activeUsers": 105})  # GA4'ün tekil toplamı; satırların toplamı (110) değil
        self.assertEqual(len(result["satirlar"]), 2)
        bozuk = response([(["Turkey"], ["90"])], dims=("country",), totals=[{"metricValues": []}])
        self.assertEqual(R.run_job(normalize(boyutlar=["country"]), Canned(bozuk), today=TODAY)["hata_kodu"], "YANIT_GECERSIZ")
        self.assertIsNone(R.run_job(normalize(), Canned(response([([], ["7"])])), today=TODAY)["toplam"])

    def test_synthetic_total_sorting_and_csv(self):
        from netmera_automation.ga4 import cikti
        from netmera_automation.ga4.sentetik import SyntheticClient
        result = R.run_job(normalize(boyutlar=["country"], metrikler=["activeUsers", "sessions"]), SyntheticClient(TODAY), today=TODAY)
        satirlar = result["satirlar"]
        kullanicilar = [r["metrikler"]["activeUsers"] for r in satirlar]
        self.assertEqual(kullanicilar, sorted(kullanicilar, reverse=True))  # GA4 ekranı gibi büyükten küçüğe
        self.assertLess(result["toplam"]["activeUsers"], sum(kullanicilar))  # tekil toplam satırların toplamından küçük
        self.assertEqual(result["toplam"]["sessions"], sum(r["metrikler"]["sessions"] for r in satirlar))
        csv_satirlari = cikti.rows({"calisma": "x", "zaman": "z", "kaynak": "sentetik", "sonuclar": [result]})
        self.assertEqual([r["kirilim"] for r in csv_satirlari[:2]], ["(toplam)", "(toplam)"])
        self.assertEqual(len(csv_satirlari), 2 + 2 * len(satirlar))

    def test_invalid_argument_names_the_missing_field(self):
        class Bilen(Canned):
            def metadata(self, ref):
                return {"dimensions": [{"apiName": "platform"}, {"apiName": "pagePath"}], "metrics": [{"apiName": "activeUsers"}]}
        hatali = normalize(boyutlar=["customEvent:sentetik_kategori", "pagepath"])
        result = R.run_job(hatali, Bilen(error=ClientError("INVALID_ARGUMENT")), today=TODAY)
        self.assertEqual(result["hata_kodu"], "INVALID_ARGUMENT")
        self.assertIn("Bilinmeyen boyut: customEvent:sentetik_kategori", result["hata"])
        self.assertIn("Benzer: pagePath", result["hata"])
        self.assertEqual(result["satirlar"], [])
        uyumsuz = R.run_job(normalize(boyutlar=["platform"]), Bilen(error=ClientError("INVALID_ARGUMENT")), today=TODAY)
        self.assertIn("uyumsuz", uyumsuz["hata"])  # adlar doğruysa genel ileti kalır
        metasiz = R.run_job(hatali, Canned(error=ClientError("INVALID_ARGUMENT")), today=TODAY)
        self.assertIn("uyumsuz", metasiz["hata"])  # liste alınamazsa da çalıştırma bozulmaz

    def test_run_jobs_counts(self):
        ok = Canned(response([([], ["7"])]))
        summary = R.run_jobs([normalize(), normalize(ad="İkinci")], ok, now=datetime(2026, 10, 24, 9, 0).astimezone())
        self.assertEqual((summary["tamam"], summary["uyari"], summary["hata"]), (2, 0, 0))
        self.assertEqual(summary["sonuclar"][0]["tarih_cozulen"], ["2026-10-23", "2026-10-23"])
        self.assertEqual(len(summary["calisma"]), 12)


if __name__ == "__main__":
    unittest.main()
