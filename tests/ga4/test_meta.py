import unittest

from netmera_automation.ga4 import isler as I, meta as M
from netmera_automation.ga4.sentetik import SyntheticClient
from tests.ga4.helpers import TODAY, TempDataCase, job


class CountingClient(SyntheticClient):
    kaynak = "ga4"  # önbellek yolunu denemek için gerçek kaynak gibi davranır

    def __init__(self):
        super().__init__(TODAY)
        self.calls = 0

    def metadata(self, ref):
        self.calls += 1
        return super().metadata(ref)


def normalize(**changes):
    return I.normalize_job(job(**changes), today=TODAY)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.client = SyntheticClient(TODAY)
        self.meta = M.fetch(self.client, "PROP_TEST")

    def test_names_with_suggestions(self):
        errors = M.check_names(normalize(metrikler=["activeUser"], boyutlar=["deviceCategry"]), self.meta)
        self.assertEqual(len(errors), 2)
        self.assertIn("Benzer: activeUsers", errors[0])
        self.assertIn("deviceCategory", errors[1])
        errors = M.check_names(normalize(filtreler=[{"alan": "sessions", "operator": "EXACT", "deger": "1"}]), self.meta)
        self.assertIn("bir metrik", errors[0])
        self.assertEqual(M.check_names(normalize(), self.meta), [])

    def test_compatibility(self):
        errors = M.validate(normalize(metrikler=["sessions"], boyutlar=["customEvent:sentetik_kategori"]), self.client, self.meta)
        self.assertEqual(len(errors), 2)
        self.assertEqual(M.validate(normalize(boyutlar=["deviceCategory"]), self.client, self.meta), [])


class CacheTests(TempDataCase):
    def test_real_source_metadata_is_cached_for_a_day(self):
        client = CountingClient()
        first = M.fetch(client, "PROP_TEST", now=1000.0)
        self.assertEqual(M.fetch(client, "PROP_TEST", now=2000.0), first)
        self.assertEqual(client.calls, 1)
        M.fetch(client, "PROP_TEST", now=2000.0, refresh=True)
        M.fetch(client, "PROP_TEST", now=2000.0 + M.CACHE_SECONDS + 1)
        self.assertEqual(client.calls, 3)
        (self.tmp / "veri" / "meta" / "PROP_TEST.json").write_text("bozuk", encoding="utf-8")
        M.fetch(client, "PROP_TEST", now=2000.0 + M.CACHE_SECONDS + 2)
        self.assertEqual(client.calls, 4)


if __name__ == "__main__":
    unittest.main()
