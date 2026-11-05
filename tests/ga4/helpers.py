"""Testler yalnız sentetik veri ve geçici klasör kullanır; GA4'e bağlanmaz."""
from datetime import date
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from netmera_automation.ga4.paths import APP_ROOT

SAMPLE = APP_ROOT / "config" / "ga4-ornek-isler.json"
TODAY = date(2026, 10, 24)


class TempDataCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory(prefix="ga4-test-")
        self.tmp = Path(self._tmp.name)
        self._old = os.environ.get("ANALYTICS_CEKME_DATA")
        os.environ["ANALYTICS_CEKME_DATA"] = str(self.tmp / "veri")
        # Birleşik uygulama: GA4 projeleri Netmera projeleridir (ayrı geçici klasör)
        self._old_netmera = os.environ.get("NETMERA_OTOMASYON_DATA")
        os.environ["NETMERA_OTOMASYON_DATA"] = str(self.tmp / "netmera")
        self._old_legacy = os.environ.get("NETMERA_GA4_LEGACY")
        os.environ["NETMERA_GA4_LEGACY"] = str(self.tmp / "eski-yok")

    def tearDown(self):
        if self._old_legacy is None:
            os.environ.pop("NETMERA_GA4_LEGACY", None)
        else:
            os.environ["NETMERA_GA4_LEGACY"] = self._old_legacy
        if self._old_netmera is None:
            os.environ.pop("NETMERA_OTOMASYON_DATA", None)
        else:
            os.environ["NETMERA_OTOMASYON_DATA"] = self._old_netmera
        if self._old is None:
            os.environ.pop("ANALYTICS_CEKME_DATA", None)
        else:
            os.environ["ANALYTICS_CEKME_DATA"] = self._old
        self._tmp.cleanup()


def job(**changes):
    raw = {"ad": "Sentetik iş", "property_ref": "PROP_TEST", "metrikler": ["activeUsers"], "tarih": {"tur": "dun"}}
    raw.update(changes)
    return raw
