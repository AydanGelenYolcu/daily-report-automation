"""Birleşik uygulama: GA4 işi Netmera projesinde. Yalnız sentetik GA4 kaynağı ve geçici klasörler; ağ yok."""
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from openpyxl import Workbook, load_workbook

from netmera_automation import ga4_migrate, jobs as J, runner, store
from netmera_automation.ga4 import projeler as GP, store as GS
from netmera_automation.server import App
from tests.helpers import TempDataCase


def ga4_job(label="Sentetik GA4", **ga4):
    return {"type": "ga4", "label": label, "ga4": {"property_ref": "PROP_PROD", "metrikler": ["activeUsers"], "tarih": {"tur": "dun"}, **ga4}}


class Ga4Case(TempDataCase):
    def setUp(self):
        super().setUp()
        self._env = {k: os.environ.get(k) for k in ("ANALYTICS_CEKME_DATA", "NETMERA_GA4_LEGACY")}
        os.environ.pop("ANALYTICS_CEKME_DATA", None)  # GA4 verisi Netmera veri klasöründe: <veri>\ga4
        os.environ["NETMERA_GA4_LEGACY"] = str(self.tmp / "eski")
        store.ensure_defaults()

    def tearDown(self):
        for k, v in self._env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        super().tearDown()

    def demo(self, jobs, **changes):
        p = store.load_profile("demo-sahte-veri")
        p.update(jobs=jobs, **changes)
        return store.save_profile(p)

    def run_demo(self, p, **kw):
        with mock.patch("netmera_automation.synthetic.time.sleep"):
            return runner.run_profile(p, log=lambda _: None, **kw)


class Ga4JobTests(Ga4Case):
    def test_ga4_job_lives_in_netmera_project_with_metric_columns(self):
        p = self.demo([{"type": "people", "label": "Sentetik Kişi", "items": [{"field": "Platform", "value": "iOS"}]},
                       ga4_job(metrikler=["activeUsers", "sessions"])])
        job = p["jobs"][1]
        self.assertEqual((job["type"], job["columns"]), ("ga4", ["activeUsers", "sessions"]))
        self.assertTrue(J.describe(job).startswith("GA4 · activeUsers, sessions"))
        with self.assertRaises(J.JobError):
            self.demo([ga4_job(metrikler=[])])  # Analytics doğrulaması aynen uygulanır

    def test_ga4_store_keeps_other_jobs_order_and_id_on_rename(self):
        self.demo([ga4_job("A GA4"), {"type": "people", "label": "Kişi", "items": [{"field": "Platform", "value": "iOS"}]}, ga4_job("B GA4")])
        self.assertEqual([j["ad"] for j in GS.load("demo-sahte-veri")], ["A GA4", "B GA4"])
        GS.upsert({**GS.load("demo-sahte-veri")[0], "ad": "A yeni"}, "A GA4", proje="demo-sahte-veri")
        jobs = store.load_profile("demo-sahte-veri")["jobs"]
        self.assertEqual([(j["id"], j["label"]) for j in jobs], [("a_ga4", "A yeni"), ("kisi", "Kişi"), ("b_ga4", "B GA4")])
        GS.delete("B GA4", proje="demo-sahte-veri")
        self.assertEqual([j["label"] for j in store.load_profile("demo-sahte-veri")["jobs"]], ["A yeni", "Kişi"])

    def test_project_level_ga4_settings_are_in_profile(self):
        GP.guncelle("demo-sahte-veri", {"sheets": {"guncelle_gun": 5, "toplam": False}, "aciklama": "Sentetik"})
        self.assertEqual(store.load_profile("demo-sahte-veri")["ga4"]["sheets"], {"tablo": None, "guncelle_gun": 5, "toplam": False})
        self.assertEqual(GP.oku("demo-sahte-veri")["ad"], "Demo (sahte veri)")


class Ga4RunTests(Ga4Case):
    def test_run_reads_ga4_without_browser_and_feeds_formula(self):
        p = self.demo([ga4_job("Etkin", metrikler=["activeUsers", "sessions"]), ga4_job("Cihaz", boyutlar=["deviceCategory"]),
                       {"type": "formula", "label": "Toplam", "expression": "A + B", "variables": [
                           {"name": "A", "kind": "job", "job_id": "etkin", "column": "activeUsers"},
                           {"name": "B", "kind": "job", "job_id": "etkin", "column": "sessions"}]}])
        with mock.patch.object(runner, "Browser", side_effect=AssertionError("GA4 için tarayıcı açılmamalı")), \
             mock.patch.object(runner, "SyntheticReader", side_effect=AssertionError("Netmera okuyucusu gerekmez")):
            s = self.run_demo(p, write=False)
        rows = {(r["job_label"], r["column"]): r for r in s["rows"]}
        a, b = rows[("Etkin", "activeUsers")]["value"], rows[("Etkin", "sessions")]["value"]
        self.assertEqual(rows[("Toplam", None)]["value"], a + b)
        self.assertIn("GA4 toplamı", rows[("Cihaz", "activeUsers")]["note"])
        table = next(t for t in s["ga4"] if t["is"] == "Cihaz")
        self.assertEqual(rows[("Cihaz", "activeUsers")]["value"], table["toplam"]["activeUsers"])  # GA4'ün kendi toplamı
        self.assertGreater(len(table["satirlar"]), 1)

    def test_split_days_and_selected_dates_apply_to_ga4(self):
        from datetime import date, timedelta
        p = self.demo([ga4_job("Etkin")])
        today = runner.now().date()
        first = today - timedelta(days=3)
        s = self.run_demo(p, write=False, dates=(first, first + timedelta(days=1)), split_days=True)
        days = sorted(r["period_start"][:10] for r in s["day_rows"])
        self.assertEqual(days, [first.isoformat(), (first + timedelta(days=1)).isoformat()])
        self.assertTrue(all(t["tarih_istegi"]["startDate"] == t["tarih_istegi"]["endDate"] for t in s["ga4"]))

    def test_not_connected_real_project_fails_ga4_job_only(self):
        p = store.load_profile("production")
        p.update(netmera_app="Sentetik", jobs=[ga4_job("Etkin")])
        p = store.save_profile(p)
        s = runner.run_profile(p, write=False, log=lambda _: None)
        self.assertEqual([r["status"] for r in s["rows"]], ["GA4_NOT_CONNECTED"])

    def test_ga4_value_written_to_mapped_cell_and_sheets_tabs_called(self):
        path = self.tmp / "cikti" / "rapor.xlsx"
        path.parent.mkdir()
        book = Workbook(); book.active.title = "Rapor"; book.save(path); book.close()
        p = self.demo([ga4_job("Etkin", sheets={"sekme": "Etkin"})], output_file=str(path), excel_mode="mapped_cells",
                      excel_mappings=[{"job_id": "etkin", "column": "activeUsers", "sheet": "Rapor", "mode": "fixed_cell", "cell": "B2"}])
        s = self.run_demo(p, write=True)
        value = next(r["value"] for r in s["rows"] if r["job_id"] == "etkin")
        book = load_workbook(path)
        self.assertEqual(book["Rapor"]["B2"].value, value)
        book.close()
        self.assertNotIn("ga4_sheets", s)  # Demo: sentetik sayılar Sheets'e gitmez
        real = store.load_profile("production")
        real.update(netmera_app="Sentetik", jobs=[ga4_job("Etkin", sheets={})])
        real = store.save_profile(real)
        logs = []
        with mock.patch("netmera_automation.ga4.zamanli.kayitli_calistir", return_value={"isler": [], "tamam": 1, "uyari": 0, "hata": 0}) as call:
            runner._ga4_sheets(real, None, {}, logs.append)
        self.assertEqual([j["ad"] for j in call.call_args[0][1]], ["Etkin"])


class Ga4OnlyProjectTests(Ga4Case):
    """Analytics'ten gelen proje: Netmera uygulaması ve Excel hedefi yok, GA4 işleri kendi Sheets sekmelerine yazar."""
    def ga4_project(self, enabled=True):
        p = store.create_profile("Sentetik GA4 projesi")
        p["jobs"] = [{**ga4_job("Etkin", sheets={}), "enabled": enabled}]
        return store.save_profile(p)

    def test_ga4_only_project_is_ready_without_excel_or_netmera_app(self):
        from netmera_automation import server
        p = self.ga4_project()
        self.assertEqual(J.readiness(p), [])
        self.assertEqual(len(server.readiness(p)), 1)  # yalnız GA4 bağlantısı eksik
        self.assertIn("GA4 bağlantısı", server.readiness(p)[0])
        with mock.patch("netmera_automation.ga4.ayarlar.load", return_value={}):
            self.assertEqual(server.readiness(p), [])
        self.assertTrue(server.project_summary(p)["ga4_only"])
        p["jobs"].append({"type": "people", "label": "Kişi", "items": [{"field": "Platform", "value": "iOS"}]})
        self.assertIn("Ayarlar: Netmera'da seçili uygulamanın tam adını yazın.", J.readiness(J.normalize_profile(p)))

    def test_write_run_without_output_keeps_results_and_reports_clearly(self):
        p = self.demo([ga4_job("Etkin")], output_file="")
        s = self.run_demo(p, write=True)
        self.assertEqual([r["status"] for r in s["rows"]], ["OK"])
        self.assertIn("hücre hedefi seçilmedi", s["excel"])
        self.assertTrue((store.profile_dir(p["id"]) / "sonuclar.jsonl").is_file())

    def test_disabled_ga4_jobs_still_write_their_sheets_tabs(self):
        p = self.ga4_project(enabled=False)
        with mock.patch("netmera_automation.ga4.zamanli.kayitli_calistir", return_value={"isler": [], "tamam": 1, "uyari": 0, "hata": 0}) as call,              mock.patch.object(runner, "Browser", side_effect=AssertionError("tarayıcı açılmamalı")):
            s = runner.run_profile(p, write=True, log=lambda _: None)
        self.assertEqual(([j["ad"] for j in call.call_args[0][1]], s["ok"], s["failed"]), (["Etkin"], 1, 0))
        with self.assertRaises(runner.RunError):
            runner.run_profile(p, write=False, log=lambda _: None)  # test çalışması: okunacak iş yok

    def test_copied_project_does_not_write_to_same_ga4_table(self):
        GP.guncelle("demo-sahte-veri", {"sheets": {"tablo": "a" * 30}})
        copy = store.create_profile("Kopya", source="demo", copy_from="demo-sahte-veri")
        self.assertIsNone(copy["ga4"]["sheets"]["tablo"])
        self.assertEqual(store.load_profile("demo-sahte-veri")["ga4"]["sheets"]["tablo"], "a" * 30)


class Ga4ImportTests(Ga4Case):
    def legacy(self):
        old = self.tmp / "eski"
        (old / "projeler" / "aylik").mkdir(parents=True)
        (old / "google-istemci.json").write_text("{}", encoding="utf-8")
        (old / "ayarlar.json").write_text(json.dumps({"kimlik": {"tur": "google_hesabi", "istemci_dosyasi": str(old / "google-istemci.json")},
                                                      "propertyler": {"PROP_PROD": {"property_id": "123456789"}}}), encoding="utf-8")
        (old / "projeler" / "aylik" / "proje.json").write_text(json.dumps({
            "id": "aylik", "ad": "Aylık Rapor", "sheets": {"tablo": None, "guncelle_gun": 4, "toplam": True},
            "zamanlama": {"saat": "08:15", "gunler": ["MON", "WED"]}}), encoding="utf-8")
        (old / "projeler" / "aylik" / "isler.json").write_text(json.dumps({"isler": [
            {"ad": "Sentetik etkin", "property_ref": "PROP_PROD", "metrikler": ["activeUsers"], "tarih": {"tur": "dun"}, "sheets": {}},
            {"ad": "Bozuk", "property_ref": "küçük", "metrikler": ["x"], "tarih": {"tur": "dun"}}]}), encoding="utf-8")
        return old

    def test_import_once_converts_projects_and_settings_without_touching_source(self):
        old = self.legacy()
        before = {p: p.read_bytes() for p in old.rglob("*") if p.is_file()}
        result = ga4_migrate.import_legacy(log=lambda _: None)
        item = result["projeler"][0]
        p = store.load_profile(item["proje"])
        self.assertEqual((p["name"], [j["label"] for j in p["jobs"]]), ("Aylık Rapor", ["Sentetik etkin"]))
        self.assertEqual((p["ga4"]["sheets"]["guncelle_gun"], p["schedule"]["times"], p["schedule"]["days"]), (4, ["08:15"], ["MON", "WED"]))
        self.assertEqual(len(item["atlanan"]), 1)
        from netmera_automation.ga4.paths import data_root
        settings = json.loads((data_root() / "ayarlar.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(settings["kimlik"]["istemci_dosyasi"]), data_root() / "google-istemci.json")
        self.assertEqual(before, {p: p.read_bytes() for p in old.rglob("*") if p.is_file()})  # eski klasör değişmedi
        self.assertEqual(ga4_migrate.import_legacy(log=lambda _: None), result)  # ikinci kez çalışmaz
        self.assertEqual(sum(pr["name"] == "Aylık Rapor" for pr in store.list_profiles()), 1)


class SharedGoogleTests(Ga4Case):
    def test_old_profile_accounts_never_replace_shared_ga4_account(self):
        from netmera_automation import google_auth, google_shared
        from netmera_automation.ga4.istemci import ClientError
        pid = "demo-sahte-veri"
        google_auth._save(pid, {"refresh_token": "old-synthetic-refresh", "access_token": "old-synthetic-access", "expires_at": 9999999999})
        shared = {"client": True, "connected": True, "sheets": True, "email": "shared@example.invalid", "expired": False}
        with mock.patch.object(google_shared, "status", return_value=shared), mock.patch.object(google_shared, "token", return_value="shared-synthetic-access"):
            self.assertTrue(google_auth.status(pid)["shared"])
            self.assertEqual(google_auth.access_token(pid), "shared-synthetic-access")
        for update in ({"connected": False}, {"sheets": False}, {"connected": False, "expired": True}):
            with self.subTest(update=update), mock.patch.object(google_shared, "status", return_value={**shared, **update}), \
                    mock.patch.object(google_shared, "token", side_effect=ClientError("SHEETS_GIRIS", "Sentetik ortak giriş hatası")):
                self.assertFalse(google_auth.status(pid)["connected"])
                with self.assertRaises(google_auth.GoogleError):
                    google_auth.access_token(pid)

    def test_imported_ga4_json_is_the_single_google_client_for_every_project(self):
        from netmera_automation import google_auth
        old = self.tmp / "eski"
        old.mkdir()
        client = {"installed": {"client_id": "sentetik-ga4.apps.googleusercontent.com", "client_secret": "sentetik-sir"}}
        (old / "google-istemci.json").write_text(json.dumps(client), encoding="utf-8")
        ga4_migrate.import_legacy(log=lambda _: None)
        for pid in ("production", store.create_profile("Sentetik yeni proje")["id"]):
            info = google_auth.status(pid)
            self.assertEqual((info["configured"], info["shared_client"], info["team"]), (True, True, False), pid)
        from netmera_automation import google_shared
        self.assertEqual(google_shared.shared_client()["client_id"], "sentetik-ga4.apps.googleusercontent.com")
        newer = {"installed": {"client_id": "sentetik-son.apps.googleusercontent.com", "client_secret": "sir2"}}
        google_auth.save_client("production", newer)  # Netmera Ayarlar'dan seçilen JSON da ortak dosyaya yazılır
        from netmera_automation.ga4 import google_hesap
        self.assertEqual(google_hesap.GoogleHesabi().istemci()["client_id"], "sentetik-son.apps.googleusercontent.com")


class Ga4ServerTests(Ga4Case):
    def test_mounted_routes_use_current_project_and_source(self):
        self.demo([ga4_job("Etkin")])
        app = App(0)
        d = app.handle("GET", "/api/ga4/isler", {}, {"profile": ["demo-sahte-veri"]})
        self.assertEqual([j["ad"] for j in d["isler"]], ["Etkin"])
        r = app.handle("POST", "/api/ga4/calistir", {"profile_id": "demo-sahte-veri"}, {})
        self.assertEqual((r["kaynak"], r["tamam"] + r["uyari"]), ("sentetik", 1))
        with self.assertRaises(ValueError):
            app.handle("POST", "/api/ga4/zamanlama/kur", {"profile_id": "demo-sahte-veri"}, {})
        state = app.state("demo-sahte-veri")
        self.assertIn("ga4", state)
        self.assertEqual(state["constants"]["job_types"]["ga4"], "Google Analytics (GA4)")


if __name__ == "__main__":
    unittest.main()
