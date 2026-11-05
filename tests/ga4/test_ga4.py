import io
import json
import unittest
import urllib.error

from netmera_automation.ga4 import ayarlar
from netmera_automation.ga4.ga4 import RestClient
from netmera_automation.ga4.istemci import ClientError
from tests.ga4.helpers import TempDataCase

SETTINGS = {"propertyler": {"PROP_TEST": {"property_id": "987654"}}}


class Body:
    def __init__(self, data):
        self.data = data

    def read(self, limit=-1):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Sırayla yanıt veya HTTP hatası döndürür; gerçek ağa çıkmaz."""

    def __init__(self, *answers):
        self.answers, self.requests = list(answers), []

    def open(self, request, timeout=None):
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return Body(json.dumps(answer).encode() if not isinstance(answer, bytes) else answer)


def http_error(code, payload):
    return urllib.error.HTTPError("https://analyticsdata.googleapis.com/", code, "x", {}, io.BytesIO(json.dumps(payload).encode()))


def client(*answers, sleeps=None):
    opener = FakeOpener(*answers)
    rest = RestClient(SETTINGS, token_provider=lambda: "sentetik-belirtec", opener=opener,
                      sleep=(sleeps.append if sleeps is not None else lambda s: None))
    return rest, opener


class RestTests(unittest.TestCase):
    def test_requests_use_read_only_endpoints(self):
        rest, opener = client({"rows": []}, {"dimensions": [], "metrics": []}, {})
        rest.run_report("PROP_TEST", {"metrics": [{"name": "activeUsers"}]})
        rest.metadata("PROP_TEST")
        rest.check_compatibility("PROP_TEST", {"metrics": []})
        report, meta, compat = opener.requests
        self.assertEqual(report.full_url, "https://analyticsdata.googleapis.com/v1beta/properties/987654:runReport")
        self.assertEqual((report.get_method(), json.loads(report.data)), ("POST", {"metrics": [{"name": "activeUsers"}]}))
        self.assertEqual(report.get_header("Authorization"), "Bearer sentetik-belirtec")
        self.assertEqual((meta.get_method(), meta.full_url.rsplit("/", 1)[1]), ("GET", "metadata"))
        self.assertTrue(compat.full_url.endswith(":checkCompatibility"))

    def test_error_codes_without_raw_details(self):
        cases = [(http_error(403, {"error": {"status": "PERMISSION_DENIED", "message": "properties/987654 gizli",
                                             "details": [{"reason": "SERVICE_DISABLED"}]}}), "SERVICE_DISABLED"),
                 (http_error(403, {"error": {"status": "PERMISSION_DENIED"}}), "PERMISSION_DENIED"),
                 (http_error(401, {}), "UNAUTHENTICATED"), (http_error(400, {"error": "x"}), "INVALID_ARGUMENT"),
                 (http_error(418, {}), "HTTP_418"), (urllib.error.URLError("dns"), "AG_HATASI"), (b"<html>", "YANIT_GECERSIZ")]
        for answer, code in cases:
            with self.subTest(code=code):
                rest, _ = client(answer)
                with self.assertRaises(ClientError) as caught:
                    rest.run_report("PROP_TEST", {})
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("987654", str(caught.exception))
                self.assertNotIn("gizli", str(caught.exception))

    def test_retry_only_once_and_only_for_server_errors(self):
        sleeps = []
        rest, opener = client(http_error(503, {}), {"ok": True}, sleeps=sleeps)
        self.assertEqual(rest.run_report("PROP_TEST", {}), {"ok": True})
        self.assertEqual((len(opener.requests), sleeps), (2, [2]))
        rest, opener = client(http_error(500, {}), http_error(503, {}))
        with self.assertRaises(ClientError) as caught:
            rest.run_report("PROP_TEST", {})
        self.assertEqual((caught.exception.code, len(opener.requests)), ("UNAVAILABLE", 2))
        rest, opener = client(http_error(429, {"error": {"status": "RESOURCE_EXHAUSTED"}}))
        with self.assertRaises(ClientError):
            rest.run_report("PROP_TEST", {})
        self.assertEqual(len(opener.requests), 1)

    def test_unknown_reference_and_missing_auth_do_not_call_network(self):
        rest, opener = client()
        with self.assertRaises(ClientError) as caught:
            rest.run_report("PROP_YOK", {})
        self.assertEqual(caught.exception.code, "AYAR_EKSIK")
        default = RestClient(SETTINGS, opener=opener)
        with self.assertRaises(ClientError) as caught:
            default.run_report("PROP_TEST", {})
        self.assertEqual(caught.exception.code, "KIMLIK_SECILMEDI")
        self.assertEqual(opener.requests, [])


class SettingsTests(TempDataCase):
    def write(self, content):
        path = ayarlar.settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content), encoding="utf-8")

    def test_load_and_validate(self):
        with self.assertRaisesRegex(ayarlar.SettingsError, "kurulum --anahtar"):
            ayarlar.load()
        self.write(SETTINGS)
        self.assertEqual(ayarlar.property_id(ayarlar.load(), "PROP_TEST"), "987654")
        bad = [{}, {"propertyler": []}, {"propertyler": {}, "kimlik": {"tur": "google_hesabi"}}, {"propertyler": {"prop": {"property_id": "1"}}},
               {"propertyler": {"PROP": {"property_id": "12a"}}}, {"propertyler": {"PROP": {"property_id": 12}}},
               {"propertyler": {"PROP": {"property_id": "1", "anahtar": "x"}}}, {"propertyler": {"PROP": {"property_id": "1"}}, "fazla": 1}]
        for content in bad:
            with self.subTest(content=content):
                self.write(content)
                with self.assertRaises(ayarlar.SettingsError):
                    ayarlar.load()


if __name__ == "__main__":
    unittest.main()
