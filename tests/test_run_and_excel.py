from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET

from openpyxl import Workbook, load_workbook

from netmera_automation import runner, scheduler, store
from netmera_automation.excel import ExcelLockedError, write_workbook
from netmera_automation.panel import PanelError
from tests.helpers import TempDataCase, event_job


def demo_profile(tmp: Path, **changes):
    store.ensure_defaults()
    profile = next(p for p in store.list_profiles() if p["source"] == "demo")
    profile.update(output_file=str(tmp / "cikti" / "demo.xlsx"), **changes)
    return store.save_profile(profile)


class FlakyReader:
    """İlk işte bir kez hata verip sonra başarılı olan, ikinci işte hep hata veren sahte okuyucu."""

    def __init__(self, *args, **kwargs):
        self.calls = 0

    def fetch(self, job, first, last):
        self.calls += 1
        if job["label"] == "B":
            raise PanelError("RESULT_ROW_MISSING_OR_AMBIGUOUS", "RESULT_READ")
        if self.calls == 1:
            raise PanelError("COUNT_REFRESH_UNVERIFIED", "QUERY_RUN")
        return {c: 7 for c in job.get("columns") or [None]}, None


class StoreAndRunTests(TempDataCase):
    def test_elapsed_seconds_include_output_writing(self):
        profile = demo_profile(self.tmp, jobs=[event_job()])
        clock = [10.0]
        def write(*args):
            clock[0] = 21.0
            return 1
        with patch.object(runner.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(runner, 'rebuild_excel', side_effect=write), patch('netmera_automation.synthetic.time.sleep'):
            summary = runner.run_profile(profile, log=lambda _: None)
        self.assertEqual(summary['seconds'], 11)
        saved = store.read_json(store.profile_dir(profile['id']) / 'son-calisma.json')
        self.assertEqual(saved['seconds'], 11)

    def test_session_tag_metadata_excel_and_history_remain_distinct(self):
        profile=demo_profile(self.tmp,jobs=[event_job(metric='by_session',audience='tag',audience_name='Sentetik Tag')])
        result=runner.run_profile(profile,log=lambda _:None)
        row=result['rows'][0]
        self.assertEqual((row['metric'],row['audience'],row['audience_name']),('by_session','tag','Sentetik Tag'))
        book=load_workbook(profile['output_file'])
        self.assertEqual(book['Sonuclar'].cell(6,4).value,'Oturuma göre')
        self.assertIn('Tag: Sentetik Tag',book['Sonuclar'].cell(6,12).value)
        book.close()
        profile['jobs'][0].update(metric='total_event_count',audience_name='Diğer Sentetik Tag')
        profile=store.save_profile(profile)
        runner.run_profile(profile,log=lambda _:None)
        saved=list(store.load_results(profile['id']).values())
        self.assertEqual(len(saved),2)
        self.assertEqual(len({r['signature'] for r in saved}),2)
        book=load_workbook(profile['output_file'])
        self.assertEqual({book['Sonuclar'].cell(n,4).value for n in (6,7)},{'Oturuma göre','Toplam olay'})
        book.close()

    def test_legacy_between_dates_can_be_opened_but_cannot_run_without_dates(self):
        from netmera_automation import jobs as J
        profile = demo_profile(self.tmp)
        profile['jobs'] = [{"id":"eski", "label":"Eski aralık", "type":"people", "enabled":True,
                            "items":[{"field":"Behavior", "value":"Sentetik", "steps":["exactly", "between dates"], "numbers":[1]}]}]
        # v0.5 dosyası: tarih modu var fakat iki tarih henüz saklanamıyordu.
        path = store.profile_dir(profile['id']) / 'profil.json'
        store.write_json(path, profile)
        loaded = store.load_profile(profile['id'])
        self.assertIn(profile['id'], [p['id'] for p in store.list_profiles()])
        self.assertTrue(any('başlangıç ve bitiş' in m for m in J.readiness(loaded)))
        with patch('netmera_automation.runner.SyntheticReader') as reader:
            with self.assertRaises(runner.RunError):
                runner.run_profile(loaded, write=False, log=lambda _:None)
            reader.assert_not_called()
        with self.assertRaises(J.JobError):
            store.save_profile(loaded)
        loaded['jobs'][0]['items'][0]['date_range'] = {'start':'2026-03-01', 'end':'2026-03-02'}
        saved = store.save_profile(loaded)
        self.assertEqual(saved['jobs'][0]['items'][0]['date_range']['end'], '2026-03-02')

    def test_defaults_profiles_and_copy(self):
        store.ensure_defaults()
        profiles = store.list_profiles()
        self.assertEqual(sorted(p["source"] for p in profiles), ["demo", "netmera"])
        prod = next(p for p in profiles if p["source"] == "netmera")
        self.assertEqual(prod["browser"], "chrome")
        copy = store.create_profile("DemoCompany Web", copy_from=prod["id"])
        self.assertEqual(copy["output_file"], "")
        self.assertEqual(len(copy["jobs"]), 6)
        store.ensure_defaults()  # ikinci kez çoğaltmaz
        self.assertEqual(len(store.list_profiles()), 3)

    def test_demo_run_writes_excel_and_reruns_update(self):
        profile = demo_profile(self.tmp)
        clock = iter(datetime.fromisoformat("2026-03-10T09:00:00+03:00") + timedelta(minutes=m) for m in range(100))
        with patch("netmera_automation.synthetic.time.sleep"), patch("netmera_automation.runner.now", lambda: next(clock)):
            first = runner.run_profile(profile, log=lambda _: None)
            second = runner.run_profile(profile, log=lambda _: None)
        self.assertEqual((first["ok"], first["failed"]), (6, 0))
        self.assertEqual(second["excel"], "Excel güncellendi.")
        book = load_workbook(profile["output_file"])
        self.assertEqual(book.sheetnames, ["Gunluk", "Sonuclar"])
        daily = book["Gunluk"]
        headers = [c.value for c in daily[5]]
        self.assertEqual(headers[:3], ["Çalışma günü", "Son çalışma", "Event verisinin dönemi"])
        self.assertEqual(headers[3:], [j["label"] for j in profile["jobs"]])
        self.assertEqual(daily.max_row, 6)  # aynı gün: tek satır
        self.assertTrue(all(isinstance(c.value, int) for c in daily[6][3:]))
        # event satırları aynı dönem için güncellenir; People her çalışmada ayrı anlık görüntü
        detail = book["Sonuclar"]
        self.assertEqual(detail.max_row - 5, 5 + 2)
        self.assertIn("SENTETİK", book["Gunluk"]["A1"].value)

    def test_failures_are_not_zero_and_do_not_erase_good_values(self):
        profile = demo_profile(self.tmp, jobs=[event_job(label="A"), event_job(label="B", event="Başka")])
        with patch("netmera_automation.runner.SyntheticReader", FlakyReader):
            summary = runner.run_profile(profile, log=lambda _: None)
        self.assertEqual((summary["ok"], summary["failed"]), (1, 1))  # A ikinci denemede başarılı
        bad = next(r for r in summary["rows"] if r["job_label"] == "B")
        self.assertIsNone(bad["value"])
        self.assertEqual(bad["status"], "RESULT_ROW_MISSING_OR_AMBIGUOUS")
        daily = load_workbook(profile["output_file"])["Gunluk"]
        self.assertEqual([c.value for c in daily[6]][3:], [7, "HATA"])

        good = dict(next(r for r in summary["rows"] if r["job_label"] == "A"))
        store.append_results(profile["id"], [{**good, "status": "TIMEOUT", "value": None}])
        kept = [r for r in store.load_results(profile["id"]).values() if r["job_label"] == "A"]
        self.assertEqual([(r["status"], r["value"]) for r in kept], [("OK", 7)])

    def test_run_date_override_applies_to_event_jobs_only(self):
        profile = demo_profile(self.tmp)
        with patch("netmera_automation.synthetic.time.sleep"):
            summary = runner.run_profile(profile, write=False, dates=(date(2026, 3, 1), date(2026, 3, 2)), log=lambda _: None)
        events = [r for r in summary["rows"] if r["job_type"] == "event"]
        self.assertTrue(events and all(r["period_start"].startswith("2026-03-01") and r["period_end"].startswith("2026-03-03") for r in events))
        self.assertTrue(all(r["period_start"] is None for r in summary["rows"] if r["job_type"] == "people"))

    def test_job_is_read_three_times_before_failing(self):
        class Twice:
            calls = {}
            def __init__(self, *a, **k): pass
            def fetch(self, job, first, last):
                n = Twice.calls[job["label"]] = Twice.calls.get(job["label"], 0) + 1
                if job["label"] == "A" and n <= 2: raise PanelError("COUNT_REFRESH_UNVERIFIED", "QUERY_RUN")
                if job["label"] == "B": raise PanelError("RESULT_ROW_MISSING_OR_AMBIGUOUS", "RESULT_READ")
                return {c: 7 for c in job.get("columns") or [None]}, None
        profile = demo_profile(self.tmp, jobs=[event_job(label="A"), event_job(label="B", event="Başka")])
        with patch("netmera_automation.runner.SyntheticReader", Twice):
            summary = runner.run_profile(profile, write=False, log=lambda _: None)
        self.assertEqual(runner.MAX_ATTEMPTS, 3)
        self.assertEqual((summary["ok"], summary["failed"]), (1, 1))  # A üçüncü denemede başarılı
        self.assertEqual(Twice.calls, {"A": 3, "B": 3})

    def test_unexpected_browser_error_is_retried_not_crash(self):
        class Broken:
            calls = 0
            def __init__(self, *a, **k): pass
            def fetch_many(self, jobs, first, last, per_day=False):
                Broken.calls += 1
                raise RuntimeError("Execution context was destroyed")
        profile = demo_profile(self.tmp, jobs=[event_job(label="A")])
        with patch("netmera_automation.runner.SyntheticReader", Broken):
            summary = runner.run_profile(profile, write=False, log=lambda _: None)
        self.assertEqual((summary["ok"], summary["failed"], Broken.calls), (0, 1, 3))
        self.assertEqual(summary["rows"][0]["status"], "STEP_FAILED")

    def test_write_nothing_policy(self):
        profile = demo_profile(self.tmp, on_error="write_nothing", jobs=[event_job(label="A"), event_job(label="B")])
        with patch("netmera_automation.runner.SyntheticReader", FlakyReader):
            summary = runner.run_profile(profile, log=lambda _: None)
        self.assertFalse(Path(profile["output_file"]).exists())
        self.assertIn("kaydedilmedi", summary["excel"])

    def test_fatal_login_error_stops_remaining_jobs(self):
        class LoggedOut:
            def __init__(self, *a, **k): pass
            def fetch(self, job, first, last): raise PanelError("LOGIN_REQUIRED")
        profile = demo_profile(self.tmp, jobs=[event_job(label="A"), event_job(label="B")])
        with patch("netmera_automation.runner.SyntheticReader", LoggedOut):
            summary = runner.run_profile(profile, write=False, log=lambda _: None)
        self.assertEqual([r["status"] for r in summary["rows"]], ["LOGIN_REQUIRED", "LOGIN_REQUIRED"])

    def test_profile_lock_blocks_parallel_run(self):
        profile = demo_profile(self.tmp)
        with store.profile_lock(profile["id"]):
            with self.assertRaises(store.BusyError):
                runner.run_profile(profile, log=lambda _: None)
        stale = store.profile_dir(profile["id"]) / "calisiyor.lock"
        stale.write_text("999999")  # artık yaşamayan işlem
        with store.profile_lock(profile["id"]):
            pass

    def test_run_requires_settings_for_prod(self):
        store.ensure_defaults()
        prod = next(p for p in store.list_profiles() if p["source"] == "netmera")
        with self.assertRaises(runner.RunError):
            runner.run_profile(prod, log=lambda _: None)
        bad = {**prod, "netmera_app": "DemoCompany", "output_file": str(Path(__file__).resolve().parents[1] / "x.xlsx")}
        with self.assertRaisesRegex(runner.RunError, "OneDrive|uygulama klasör"):
            runner.run_profile(bad, log=lambda _: None)


class ExcelTests(TempDataCase):
    def test_refuses_foreign_workbook_and_reports_lock(self):
        foreign = self.tmp / "baska.xlsx"
        Workbook().save(foreign)
        row = {"source": "demo", "job_label": "A", "column_label": "A", "column": None, "job_type": "event",
               "metric": "total_event_count", "value": 1, "status": "OK", "period_start": None, "period_end": None,
               "planned_at": "2026-03-10T09:00:00+03:00", "captured_at": "2026-03-10T09:00:05+03:00",
               "description": "x", "signature": "abc"}
        with self.assertRaises(ValueError):
            write_workbook(foreign, [row], "demo", ["A"])
        target = self.tmp / "ok.xlsx"
        write_workbook(target, [row], "demo", ["A"])
        with self.assertRaises(ValueError):  # demo dosyasına prod yazılmaz
            write_workbook(target, [{**row, "source": "netmera"}], "netmera", ["A"])
        with patch("netmera_automation.excel.os.replace", side_effect=PermissionError):
            with self.assertRaises(ExcelLockedError):
                write_workbook(target, [row], "demo", ["A"])
        self.assertEqual(load_workbook(target)["Gunluk"]["D6"].value, 1)
        cell = load_workbook(target)["Sonuclar"]["L6"]
        self.assertEqual(cell.data_type, "s")


class SchedulerTests(TempDataCase):
    def test_task_xml(self):
        profile = demo_profile(self.tmp, schedule={"times": ["09:00", "14:30"], "days": ["MON", "FRI"]})
        xml = scheduler.build_task_xml(profile, Path("C:/App/python/pythonw.exe"), Path("C:/App Klasör"), start=date(2026, 3, 10))
        root = ET.fromstring(xml.replace('encoding="UTF-16"', ""))
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        boundaries = [e.text for e in root.findall(".//t:CalendarTrigger/t:StartBoundary", ns)]
        self.assertEqual(boundaries, ["2026-03-10T09:00:00", "2026-03-10T14:30:00"])
        self.assertEqual([d.tag.split("}")[1] for d in root.find(".//t:DaysOfWeek", ns)], ["Monday", "Friday"])
        self.assertEqual(root.find(".//t:LogonType", ns).text, "InteractiveToken")
        self.assertEqual(root.find(".//t:Arguments", ns).text, f"-m netmera_automation run --profile {profile['id']} --zamanli")
        self.assertEqual(root.find(".//t:WorkingDirectory", ns).text, str(Path("C:/App Klasör")))
