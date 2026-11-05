"""Testler yalnız sentetik veri ve geçici klasör kullanır; Netmera'ya bağlanmaz."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


def browser_test_args():
    """İsteğe bağlı düşük bellek modu; üretim tarayıcısı ayarlarını değiştirmez."""
    return ['--disable-gpu','--renderer-process-limit=1'] if os.environ.get('NETMERA_TEST_LOW_MEMORY')=='1' else []


class TempDataCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory(prefix="netmera-test-")
        self.tmp = Path(self._tmp.name)
        self._old = os.environ.get("NETMERA_OTOMASYON_DATA")
        os.environ["NETMERA_OTOMASYON_DATA"] = str(self.tmp / "veri")
        self._ga4_env = {key: os.environ.get(key) for key in ("ANALYTICS_CEKME_DATA", "NETMERA_GA4_LEGACY")}
        os.environ["ANALYTICS_CEKME_DATA"] = str(self.tmp / "ga4")
        os.environ["NETMERA_GA4_LEGACY"] = str(self.tmp / "eski-yok")
        # Geliştiricinin klasöründeki gerçek ekip istemcisi testlere karışmasın.
        from unittest.mock import patch
        from netmera_automation import google_auth
        team = patch.object(google_auth, "TEAM_FILE", self.tmp / "config" / "google-ekip-istemcisi.json")
        team.start(); self.addCleanup(team.stop)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("NETMERA_OTOMASYON_DATA", None)
        else:
            os.environ["NETMERA_OTOMASYON_DATA"] = self._old
        for key, value in self._ga4_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()


def event_job(**changes):
    job = {"type": "event", "label": "Sentetik Olay", "event": "Sentetik Event", "filters": [],
           "metric": "total_event_count", "period": {"type": "yesterday"}, "columns": ["All Total"]}
    job.update(changes)
    return job
