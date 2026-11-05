import unittest

from netmera_automation.ga4 import isler as I, rapor as R
from netmera_automation.ga4.istemci import ClientError
from netmera_automation.ga4.sentetik import SyntheticClient
from tests.ga4.helpers import TODAY, job


def run(client=None, **changes):
    client = client or SyntheticClient(TODAY)
    return R.run_job(I.normalize_job(job(**changes), today=TODAY), client, today=TODAY)


def values(result, metric="activeUsers"):
    return {tuple(r["boyutlar"].values()): r["metrikler"][metric] for r in result["satirlar"]}


class SyntheticTests(unittest.TestCase):
    def test_repeatable_and_filter_sensitive(self):
        first, second = run(boyutlar=["deviceCategory"]), run(boyutlar=["deviceCategory"])
        self.assertEqual(first["durum"], "tamam")
        self.assertEqual(values(first), values(second))
        filtered = run(filtreler=[{"alan": "eventName", "operator": "EXACT", "deger": "purchase"}])
        self.assertNotEqual(values(filtered), values(run()))

    def test_filters_on_requested_dimensions(self):
        result = run(boyutlar=["platform"], filtreler=[{"alan": "platform", "operator": "IN_LIST", "degerler": ["ios", "WEB"]}])
        self.assertEqual(set(values(result)), {("web",), ("iOS",)})
        result = run(boyutlar=["platform"], filtreler=[{"alan": "platform", "operator": "IN_LIST", "degerler": ["ios"], "harf_duyarli": True}])
        self.assertEqual(result["hata_kodu"], "KAYIT_YOK")
        result = run(boyutlar=["platform"], filtreler=[{"alan": "platform", "operator": "EXACT", "deger": "web", "haric": True}])
        self.assertEqual(set(values(result)), {("Android",), ("iOS",)})
        result = run(boyutlar=["pagePath"], filtreler=[{"alan": "pagePath", "operator": "FULL_REGEXP", "deger": "/etkinlik/.*"}])
        self.assertEqual(set(values(result)), {("/etkinlik/sentetik-1",)})

    def test_date_dimension_and_limit(self):
        result = run(boyutlar=["date"], tarih={"tur": "son_n_gun", "n": 5})
        self.assertEqual(sorted(values(result)), [("20261019",), ("20261020",), ("20261021",), ("20261022",), ("20261023",)])
        truncated = run(boyutlar=["date"], tarih={"tur": "son_n_gun", "n": 5}, limit=2)
        self.assertEqual((len(truncated["satirlar"]), truncated["uyarilar"]), (2, ["satir_kesildi"]))

    def test_invalid_requests_like_ga4(self):
        for changes in ({"metrikler": ["bilinmeyen"]}, {"metrikler": ["sessions"], "boyutlar": ["customEvent:sentetik_kategori"]},
                        {"filtreler": [{"alan": "pagePath", "operator": "FULL_REGEXP", "deger": "("}], "boyutlar": ["pagePath"]}):
            with self.subTest(changes=changes):
                self.assertEqual(run(**changes)["hata_kodu"], "INVALID_ARGUMENT")

    def test_scenarios(self):
        for ref, code in (("SENTETIK_YETKI", "PERMISSION_DENIED"), ("SENTETIK_KOTA", "RESOURCE_EXHAUSTED"),
                          ("SENTETIK_API_KAPALI", "SERVICE_DISABLED"), ("SENTETIK_BOS", "KAYIT_YOK")):
            with self.subTest(ref=ref):
                self.assertEqual(run(property_ref=ref)["hata_kodu"], code)
        self.assertEqual(run(property_ref="SENTETIK_ESIK")["uyarilar"], ["esik"])
        other = run(property_ref="SENTETIK_DIGER", boyutlar=["country"])
        self.assertIn("diger_satiri", other["uyarilar"])
        self.assertEqual(run(property_ref="SENTETIK_BOS", kayit_yoksa_sifir=True)["satirlar"][0]["metrikler"], {"activeUsers": 0})

    def test_compatibility_and_metadata(self):
        client = SyntheticClient(TODAY)
        names = {m["apiName"] for m in client.metadata("PROP_TEST")["metrics"]}
        self.assertIn("activeUsers", names)
        response = client.check_compatibility("PROP_TEST", {"dimensions": [{"name": "customEvent:sentetik_kategori"}],
                                                            "metrics": [{"name": "sessions"}, {"name": "eventCount"}]})
        self.assertEqual([m["compatibility"] for m in response["metricCompatibilities"]], ["INCOMPATIBLE", "COMPATIBLE"])
        with self.assertRaises(ClientError):
            client.metadata("SENTETIK_YETKI")


if __name__ == "__main__":
    unittest.main()
