"""Segment hesapla işi, günlere bölünmüş çalışmada anlık işler ve proje silme. Yalnız sentetik veri;
tarayıcı testleri sahte paneli kullanır (Netmera'ya istek gitmez)."""
from datetime import date, datetime, timedelta
import json
import unittest
from zoneinfo import ZoneInfo

from netmera_automation import jobs as J, runner, store
from netmera_automation.panel import PanelError, PanelReader
from netmera_automation.server import App
from tests.helpers import TempDataCase, browser_test_args, event_job
from tests.test_panel_browser import offline_route, sync_playwright

TODAY = datetime.now(ZoneInfo("Europe/Istanbul")).date()
REBUILD = {"type": "segment_run", "label": "Sentetik Segment Hesabı", "name": "Sentetik Yeni Segment", "rebuild": True,
           "create_if_missing": True, "items": [
               {"field": "Behavior", "value": "Sentetik Davranış", "steps": ["Did", "more than", "between dates"], "numbers": [0],
                "attributes": [{"name": "item category", "operator": "In", "values": ["Futbol", "DemoStore"]}]}]}


class SegmentJobTests(unittest.TestCase):
    def test_dates_only_job_is_dated_and_needs_no_rules(self):
        job = J.normalize_job({"type": "segment_run", "label": "S", "name": "demo-segment-2"})
        self.assertIn("segment_run", J.DATED_TYPES)
        self.assertEqual((job["poll_seconds"], job["max_wait_minutes"], job["rebuild"]), (60, 60, False))
        self.assertNotIn("items", job)
        self.assertEqual(J.normalize_job({'type':'segment_run','label':'Sentetik','name':'Sentetik segment','poll_seconds':20})['poll_seconds'],20)
        with self.assertRaises(J.JobError):
            J.normalize_job({'type':'segment_run','label':'Sentetik','name':'Sentetik segment','poll_seconds':19})

    def test_rebuild_rules_take_run_dates_and_attributes(self):
        job = J.normalize_job(REBUILD)
        rule = J.segment_rules_with_dates(job, date(2026, 10, 20), date(2026, 10, 21))[0]
        self.assertEqual(rule["date_range"], {"start": "2026-10-20", "end": "2026-10-21"})
        self.assertEqual(rule["attributes"][0]["values"], ["Futbol", "DemoStore"])
        self.assertNotIn("date_range", job["items"][0])  # sabit tarih saklanmaz

    def test_rule_tree_with_group_becomes_segment_items(self):
        node = lambda *buttons, **kw: {"tur": "kural", "derinlik": kw.get("d", 0), "alan": kw.get("alan", "Behavior"),  # noqa: E731
                                       "ana": list(buttons), "sayilar": kw.get("n", []), "ozellikler": kw.get("attrs", [])}
        tree = [node("Did", "add to cart", "more than", "between dates", n=["2"],
                     attrs=[{"name": "item category", "operator": "In", "values": ["Futbol", "Müzik"]}]),
                {"tur": "baglac", "derinlik": 0, "deger": "OR"},
                {"tur": "grup", "derinlik": 0}, {"tur": "baglac", "derinlik": 1, "deger": "AND"},
                node("Greater Than", "Android 1.2.3", alan="App Version", d=1), {"tur": "grup_son", "derinlik": 0}]
        got = J.segment_items_from_tree(tree)
        self.assertEqual(got["connector"], "OR")
        first, group = got["items"]
        self.assertEqual((first["value"], first["steps"], first["numbers"]), ("add to cart", ["Did", "more than", "between dates"], [2]))
        self.assertEqual(first["attributes"][0]["values"], ["Futbol", "Müzik"])
        self.assertEqual(group["rules"][0], {"field": "App Version", "value": "Android 1.2.3", "operator": "Greater Than"})
        with self.assertRaises(J.JobError):
            J.segment_items_from_tree([node("Did", "x", alan="Olmayan Alan")])

    def test_condition_summary_fills_empty_attribute_values(self):
        rule = lambda value, *attrs: {"tur": "kural", "alan": "Behavior", "ana": ["Did not", value, "between dates"],  # noqa: E731
                                      "ozellikler": [dict(a) for a in attrs]}
        tree = [rule("add to cart", {"name": "item category", "operator": "Equals", "values": ["DemoStore"]}),
                rule("purchase", {"name": "item category", "operator": "In", "values": []},
                     {"name": "item brand", "operator": "Equals", "values": []})]
        texts = ["Behavior Did add to cart between 20/10/2026 and 27/10/2026 (item category equals DemoStore)",
                 "Behavior Did not purchase between 20/10/2026 and 27/10/2026 (item category in DemoStore, DemoCategory and item brand equals Sentetik)"]
        J.fill_attribute_values(tree, texts)
        self.assertEqual(tree[1]["ozellikler"][0]["values"], ["DemoStore", "DemoCategory"])
        self.assertEqual(tree[1]["ozellikler"][1]["values"], ["Sentetik"])
        untouched = [rule("purchase", {"name": "item category", "operator": "In", "values": []})]
        J.fill_attribute_values(untouched, ["Behavior Did view item (item brand equals X)"])  # eşleşmeyen özet kullanılmaz
        self.assertEqual(untouched[0]["ozellikler"][0]["values"], [])

    def test_same_rules_ignore_dates_and_step_order(self):
        job = J.normalize_job({**REBUILD, "name": "x"})
        read = {"connector": "OR", "items": [{"kind": "rule", **{**job["items"][0], "steps": ["between dates", "Did", "more than"],
                                                                "date_range": {"start": "2026-10-01", "end": "2026-10-02"}}}]}
        self.assertTrue(J.same_segment_rules(job, read))  # tek öğede bağlaç önemsiz
        other = {"connector": "AND", "items": [{"kind": "rule", **{**job["items"][0], "value": "Başka Davranış"}}]}
        self.assertFalse(J.same_segment_rules(job, other))

    def test_current_summary_replaces_connector_tags_and_requires_exact_rule(self):
        node={'tur':'kural','alan':'Behavior','ana':['Did','Sentetik Bildirim','between dates'],
              'ozellikler':[{'name':'synthetic id','operator':'In','values':['or']}]}
        text='Behavior Did Sentetik Bildirim greater than 0 times between 01/10/2026 and 01/10/2026 (synthetic id in synthetic-a, synthetic-b)'
        self.assertEqual(J.attribute_values_from_summary(node,[text],'synthetic id'),['synthetic-a','synthetic-b'])
        J.fill_attribute_values([node],[text],prefer_summary=True)
        self.assertEqual(node['ozellikler'][0]['values'],['synthetic-a','synthetic-b'])
        self.assertIsNone(J.attribute_values_from_summary(node,[text.replace('Did ','Did not ')],'synthetic id'))
        self.assertIsNone(J.attribute_values_from_summary(node,[text.replace('Sentetik Bildirim','Baska Bildirim')],'synthetic id'))
        self.assertIsNone(J.attribute_values_from_summary(node,[text,text.replace('synthetic-b','synthetic-c')],'synthetic id'))
        self.assertEqual(J.attribute_values_from_summary(node,[text.replace(' synthetic-a, synthetic-b','')],'synthetic id'),[])

    def test_summary_values_are_bounded_by_other_attributes(self):
        node={'tur':'kural','alan':'Behavior','ana':['Did','Sentetik Bildirim','between dates'],
              'ozellikler':[{'name':'synthetic id','operator':'In','values':[]},
                            {'name':'synthetic brand','operator':'Equals','values':[]}]}
        text='Behavior Did Sentetik Bildirim between 01/10/2026 and 01/10/2026 (synthetic id In synthetic-a, synthetic-b and synthetic brand Equals synthetic-static)'
        self.assertEqual(J.attribute_values_from_summary(node,[text],'synthetic id'),['synthetic-a','synthetic-b'])
        self.assertEqual(J.attribute_values_from_summary(node,[text],'synthetic brand'),['synthetic-static'])

    def test_split_days_option_is_kept_and_described(self):
        job = J.normalize_job({"type": "segment_run", "label": "S", "name": "x", "split_days": True,
                               "period": {"type": "last_n_days", "days": 3}})
        self.assertTrue(job["split_days"])
        self.assertIn("her gün ayrı", J.describe(job))

    def test_invalid_segment_jobs_fail_with_message(self):
        cases = [({"create_if_missing": True}, "kuralları"),
                 ({"rebuild": True, "items": [{"field": "Platform", "value": "iOS"}]}, "between dates"),
                 ({"poll_seconds": 5}, "20–600"),
                 ({"rebuild": True, "items": [{**REBUILD["items"][0], "attributes": [{"name": "item category", "values": ["a", "b"]}]}]}, "In"),
                 ({"rebuild": True, "items": [{**REBUILD["items"][0], "attributes": [{"name": "", "values": ["a"]}]}]}, "attribute adı")]
        for change, text in cases:
            with self.subTest(change=change), self.assertRaises(J.JobError) as ctx:
                J.normalize_job({"type": "segment_run", "label": "S", "name": "x", **change})
            self.assertIn(text, str(ctx.exception))


class SplitRunTests(TempDataCase):
    def profile(self, jobs):
        profile = store.create_profile("Sentetik Demo", source="demo")
        profile.update(jobs=jobs, output_file=str(self.tmp / "demo.xlsx"))
        return store.save_profile(profile)

    def test_split_days_reads_snapshot_jobs_once_at_capture_time(self):
        people = {"type": "people", "label": "Sentetik Kişi", "items": [{"field": "Platform", "value": "iOS"}]}
        segment = {"type": "segment_run", "label": "Sentetik Seg", "name": "Sentetik Segment"}
        profile = self.profile([people, segment])
        first = TODAY - timedelta(days=4)
        with unittest.mock.patch("netmera_automation.synthetic.time.sleep"):
            summary = runner.run_profile(profile, write=False, dates=(first, first + timedelta(days=2)), split_days=True, log=lambda _: None)
        snapshot = [r for r in summary["rows"] if r["job_type"] == "people"]
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0]["status"], "OK")
        self.assertIn("çekim anı", snapshot[0]["note"])
        days = sorted(r["period_start"][:10] for r in summary["day_rows"])
        self.assertEqual(days, [(first + timedelta(days=n)).isoformat() for n in range(3)])

    def test_segment_job_split_days_runs_each_day_in_order(self):
        segment = {"type": "segment_run", "label": "Sentetik Seg", "name": "Sentetik Segment", "split_days": True,
                   "period": {"type": "last_n_days", "days": 3}}
        profile = self.profile([segment])
        lines = []
        with unittest.mock.patch("netmera_automation.synthetic.time.sleep"):
            summary = runner.run_profile(profile, write=False, log=lines.append)
        days = [r["period_start"][:10] for r in summary["day_rows"]]
        today = datetime.fromisoformat(summary["planned_at"]).date()  # gece yarısında sabit TODAY kayabilir
        self.assertEqual(days, [(today - timedelta(days=n)).isoformat() for n in (3, 2, 1)])
        self.assertEqual(summary["rows"], [])
        with self.assertRaises(runner.RunError):  # yazarken hücre eşlemesi gerekir
            runner.run_profile(profile, write=True, log=lambda _: None)

    def test_only_people_with_old_date_still_runs(self):
        profile = self.profile([{"type": "people", "label": "Sentetik Kişi", "items": [{"field": "Platform", "value": "iOS"}]}])
        old = TODAY - timedelta(days=4)
        with unittest.mock.patch("netmera_automation.synthetic.time.sleep"):
            summary = runner.run_profile(profile, write=False, dates=(old, old), split_days=True, log=lambda _: None)
            plain = runner.run_profile(profile, write=False, dates=(old, old), log=lambda _: None)
        for result in (summary, plain):
            self.assertEqual([r["status"] for r in result["rows"]], ["OK"])
            self.assertIsNone(result["rows"][0]["period_start"])


class DeleteProjectTests(TempDataCase):
    def test_broken_project_can_be_deleted(self):
        store.ensure_defaults()
        folder = store.profile_dir("bozuk-proje")
        store.write_json(folder / "profil.json", {"id": "bozuk-proje", "name": "Bozuk", "jobs": [{"type": "yok"}]})
        self.assertEqual([b["id"] for b in store.broken_profiles()], ["bozuk-proje"])
        with unittest.mock.patch("netmera_automation.server.scheduler.remove", return_value=False) as remove:
            App(0).handle("POST", "/api/profile/delete", {"profile_id": "bozuk-proje"}, {})
        self.assertEqual(remove.call_args[0][0]["id"], "bozuk-proje")
        self.assertFalse(folder.exists())


class StaleServerTests(TempDataCase):
    """Eski sürümün açık siyah penceresi yeni sayfaya 'Bilinmeyen istek' döndürüyordu."""
    def serve(self, ping_version, busy=False):
        from http.server import ThreadingHTTPServer
        import threading
        from netmera_automation import server

        class Old(App):
            def handle(self, method, route, body, query):
                if route == "/api/ping":
                    return {"ok": True, **({"version": ping_version} if ping_version else {})}
                if route == "/api/status":
                    return {"busy": busy}
                return super().handle(method, route, body, query)
        app = Old(server._free_port(0))
        app.httpd = ThreadingHTTPServer(("127.0.0.1", app.port), app.make_handler())
        thread = threading.Thread(target=app.httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (thread.is_alive() and app.httpd.shutdown(), app.httpd.server_close()))
        store.write_json(server.data_root() / "arayuz.json", {"port": app.port, "token": app.token})
        return server, thread

    def test_same_version_is_reused_old_version_is_shut_down(self):
        from netmera_automation import __version__
        server, thread = self.serve(__version__)
        self.assertIn("/?t=", server._existing_instance())
        server_old, old_thread = self.serve(None)  # eski sürüm ping'de sürüm göndermez
        with unittest.mock.patch("builtins.print"):
            self.assertIsNone(server_old._existing_instance())
        old_thread.join(5)
        self.assertFalse(old_thread.is_alive())

    def test_busy_old_version_is_left_running(self):
        server, thread = self.serve("0.13.9", busy=True)
        with unittest.mock.patch("builtins.print"):
            self.assertIn("/?t=", server._existing_instance())
        self.assertTrue(thread.is_alive())


@unittest.skipIf(sync_playwright is None, "playwright yok")
class SegmentPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(channel="chrome", headless=True, args=browser_test_args())
        except Exception:
            cls.pw.stop()
            raise unittest.SkipTest("Chrome bulunamadı")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.context.route("**/*", offline_route)
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.lines = []
        self.reader = PanelReader(self.page, "Sentetik App", timeout_seconds=10, log=self.lines.append)
        self.reader.open_panel()

    def tearDown(self):
        self.context.close()

    def job(self, raw):
        return {**J.normalize_job(raw), "poll_seconds": 1}  # test: liste saniyede bir yenilenir

    def stored(self):
        return json.loads(self.page.evaluate("localStorage.getItem('mockSegs')"))

    def test_existing_segment_dates_are_changed_saved_and_count_read_after_calculating(self):
        day = TODAY - timedelta(days=5)
        values, note = self.reader.fetch(self.job({"type": "segment_run", "label": "S", "name": "demo-segment-2"}), day, day)
        saved = self.stored()["941"]
        self.assertEqual(saved["dates"], [[day.strftime("%d/%m/%Y")] * 2] * 2)  # iki kuralın tarihleri de
        self.assertEqual(saved["status"], "Ready")
        self.assertEqual(values[None], saved["users"])
        self.assertNotEqual(values[None], 3593)  # hesap öncesi eski sayı alınmadı
        self.assertTrue(any("Calculating" in line for line in self.lines))
        self.assertIn(f"{day:%d.%m.%Y}", note)

    def test_instant_ready_takes_new_count_without_waiting(self):
        """Kayıttan hemen sonra liste Ready gösterir (Calculating yok): sayı değiştiyse beklemeden alınır."""
        self.page.add_init_script("window.mockCalcMs = 0")
        job = {**self.job({"type": "segment_run", "label": "S", "name": "demo-segment-2"}), "poll_seconds": 30}
        values, note = self.reader.fetch(job, TODAY - timedelta(days=3), TODAY - timedelta(days=3))
        self.assertEqual(values[None], self.stored()["941"]["users"])
        self.assertNotEqual(values[None], 3593)
        self.assertIn("sayı değişti", note)
        self.assertFalse(any("tekrar bakılacak" in line for line in self.lines))

    def test_instant_ready_with_same_count_is_checked_again_then_taken(self):
        self.page.add_init_script("window.mockCalcMs = 0; window.mockSameCount = true")
        values, note = self.reader.fetch(self.job({"type": "segment_run", "label": "S", "name": "demo-segment-2"}), TODAY, TODAY)
        self.assertEqual(values[None], 3593)
        self.assertIn("sayı aynı kaldı", note)
        self.assertEqual(sum("tekrar bakılacak" in line for line in self.lines), 3)

    def test_dependent_date_pickers_are_filled_in_safe_order(self):
        """Bitiş 05.10'dayken 22.10'a ileri, sonra 25–27.10'dayken 10.10'a geri: ikisi de yazılabilmeli."""
        for stored, day in ((["01/10/2026", "05/10/2026"], date(2026, 10, 22)), (["25/10/2026", "27/10/2026"], date(2026, 10, 10))):
            with self.subTest(stored=stored):
                seg = {"name": "demo-segment-2", "users": 3593, "status": "Ready", "date": "27/10/2026", "dates": [stored, stored]}
                self.page.evaluate("s => localStorage.setItem('mockSegs', JSON.stringify({'941': s}))", seg)
                self.reader.fetch(self.job({"type": "segment_run", "label": "S", "name": "demo-segment-2"}), day, day)
                self.assertEqual(self.stored()["941"]["dates"], [[day.strftime("%d/%m/%Y")] * 2] * 2)

    def test_missing_segment_is_created_with_rules_and_attributes(self):
        day = TODAY - timedelta(days=2)
        values, _ = self.reader.fetch(self.job(REBUILD), day, day)
        saved = self.stored()["990"]
        self.assertEqual(saved["name"], "Sentetik Yeni Segment")
        self.assertEqual(saved["dates"], [[day.strftime("%d/%m/%Y")] * 2])
        self.assertEqual(values[None], saved["users"])

    def test_rebuild_is_skipped_when_rules_already_match(self):
        rules = self.reader.segment_rules("demo-segment-2")  # 2. kuralın In değerleri listedeki özetten gelir
        job = self.job({"type": "segment_run", "label": "S", "name": "demo-segment-2", "rebuild": True, **rules})
        day = TODAY - timedelta(days=4)
        self.reader.fetch(job, day, day)
        self.assertTrue(any("yeniden kurulmadı" in line for line in self.lines))
        self.assertFalse(any("Segmentin mevcut kuralları siliniyor" in line for line in self.lines))
        self.assertEqual(self.stored()["941"]["dates"], [[day.strftime("%d/%m/%Y")] * 2] * 2)  # iki kural da duruyor

    def test_dynamic_values_are_cleared_and_refilled_each_day_and_other_rules_survive(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from openpyxl import Workbook
        from tests.test_filter_source import source
        rules = self.reader.segment_rules('demo-segment-2')
        first = TODAY - timedelta(days=3)
        self.page.add_init_script('window.mockCalcMs = 1')
        with TemporaryDirectory(prefix='netmera-attribute-') as folder:
            path=Path(folder)/'source.xlsx'
            book=Workbook();sheet=book.active;sheet.title='Kaynak'
            sheet.append(['Tarih','Diger','Deger'])
            sheet.append([first,'x','001']);sheet.append([first,'x','002']);sheet.append([first+timedelta(days=1),'x','003'])
            book.save(path);book.close()
            rules['items'][1]['attributes'][0]['value_source']=source(path)
            job=self.job({'type':'segment_run','label':'Sentetik kaynak','name':'demo-segment-2','rebuild':True,**rules})
            for day, values in ((first,['001','002']),(first+timedelta(days=1),['003'])):
                self.reader.fetch(job,day,day)
                saved=self.stored()['941']
                self.assertEqual(len(saved['attributes']),2)
                self.assertEqual(saved['attributes'][0],[{'name':'item category','operator':'Equals','values':['DemoStore']}])
                self.assertEqual(saved['attributes'][1],[{'name':'item category','operator':'In','values':values}])
                self.assertEqual(saved['dates'],[[day.strftime('%d/%m/%Y')]*2]*2)
            self.assertEqual(self.page.evaluate("localStorage.getItem('mockAttrCleared')"),'2')
            self.assertEqual(self.page.evaluate("localStorage.getItem('mockValuesAdded')"),'3')
            self.assertFalse(any('Yeni attribute sat\u0131r\u0131 ekleniyor' in line for line in self.lines))
            before=self.stored()
            with self.assertRaises(PanelError) as caught:
                self.reader.fetch(job,first+timedelta(days=2),first+timedelta(days=2))
            self.assertEqual(caught.exception.code,'ATTRIBUTE_SOURCE')
            self.assertEqual(self.stored(),before)

    def test_dynamic_attribute_refuses_changes_to_other_rules(self):
        from unittest.mock import patch
        from netmera_automation import filter_source
        rules=self.reader.segment_rules('demo-segment-2')
        rules['items'][0]['numbers']=[9]  # kaynak bağlı olmayan koşul paneldekinden farklı
        rules['items'][1]['attributes'][0]['value_source']={'source':'google_sheets','sheets_id':'synthetic_sheet_12345','sheet':'Kaynak','date_column':'A','value_column':'B'}
        job=self.job({'type':'segment_run','label':'Sentetik kaynak','name':'demo-segment-2','rebuild':True,**rules})
        with patch.object(filter_source,'read',return_value=['synthetic']):
            with self.assertRaises(PanelError) as caught:self.reader.fetch(job,TODAY,TODAY)
        self.assertEqual(caught.exception.code,'ATTRIBUTE_TARGET')
        self.assertIsNone(self.page.evaluate("localStorage.getItem('mockSegs')"))

    def test_new_segment_handles_inline_blank_or_default_row_before_name_menu(self):
        from copy import deepcopy
        from unittest.mock import patch
        from netmera_automation import filter_source
        self.page.add_init_script("const attrCase=JSON.parse(localStorage.getItem('mockAttrTestConfig')||'{}'); window.mockAttributeInline=true;window.mockAttributeDropdown=true;window.mockCalcMs=1;window.mockAttributeDefaultName=attrCase.initial||'';")
        for initial in ('', 'item brand', 'item category'):
            with self.subTest(initial=initial):
                self.page.evaluate("c=>{localStorage.clear();localStorage.setItem('mockAttrTestConfig',JSON.stringify(c));}", {'initial':initial})
                raw=deepcopy(REBUILD)
                raw['items'][0]['attributes'][0]['value_source']={'source':'google_sheets','sheets_id':'synthetic_sheet_12345','sheet':'Kaynak','date_column':'A','value_column':'B'}
                self.lines.clear()
                with patch.object(filter_source,'read',return_value=['synthetic-a','synthetic-b']):
                    self.reader.fetch(self.job(raw),TODAY,TODAY)
                saved=self.stored()['990']['attributes']
                self.assertEqual(saved[0],[{'name':'item category','operator':'In','values':['synthetic-a','synthetic-b']}])
                self.assertEqual(any('Attribute adı seçiliyor' in line for line in self.lines),initial!='item category')

    def test_removed_attribute_is_recreated_for_each_day_without_touching_sibling(self):
        from unittest.mock import patch
        from netmera_automation import filter_source
        self.page.add_init_script('window.mockAttributeRemoveRow=true;window.mockSiblingAttribute=true;window.mockAttributeInline=true;window.mockAttributeDropdown=true;window.mockCalcMs=1')
        rules=self.reader.segment_rules('demo-segment-2')
        rules['items'][1]['attributes'][0]['value_source']={'source':'google_sheets','sheets_id':'synthetic_sheet_12345','sheet':'Kaynak','date_column':'A','value_column':'B'}
        job=self.job({'type':'segment_run','label':'Sentetik koruma','name':'demo-segment-2','rebuild':True,**rules})
        for offset,values in ((2,['synthetic-a','synthetic-b']),(1,['synthetic-c'])):
            day=TODAY-timedelta(days=offset)
            with patch.object(filter_source,'read',return_value=values):
                self.reader.fetch(job,day,day)
            saved=self.stored()['941']
            self.assertEqual(saved['attributes'][0],[{'name':'item category','operator':'Equals','values':['DemoStore']}])
            self.assertCountEqual(saved['attributes'][1],[{'name':'item category','operator':'In','values':values},
                                                         {'name':'item brand','operator':'Equals','values':['synthetic-static']}])
            self.assertEqual(saved['dates'],[[day.strftime('%d/%m/%Y')]*2]*2)
        self.assertEqual(self.page.evaluate("localStorage.getItem('mockAttrCleared')"),'2')
        self.assertEqual(self.page.evaluate("localStorage.getItem('mockValuesAdded')"),'3')

    def test_transiently_detached_attribute_is_not_added_twice(self):
        from unittest.mock import patch
        from netmera_automation import filter_source
        self.page.add_init_script('window.mockAttributeRemount=true;window.mockCalcMs=1')
        rules=self.reader.segment_rules('demo-segment-2')
        rules['items'][1]['attributes'][0]['value_source']={'source':'google_sheets','sheets_id':'synthetic_sheet_12345','sheet':'Kaynak','date_column':'A','value_column':'B'}
        job=self.job({'type':'segment_run','label':'Sentetik yenileme','name':'demo-segment-2','rebuild':True,**rules})
        with patch.object(filter_source,'read',return_value=['synthetic']):
            self.reader.fetch(job,TODAY,TODAY)
        self.assertEqual(self.stored()['941']['attributes'][1],[{'name':'item category','operator':'In','values':['synthetic']}])
        self.assertFalse(any('Yeni attribute satırı ekleniyor' in line for line in self.lines))
        self.assertEqual(self.page.evaluate("localStorage.getItem('mockAttrCleared')"),'1')

    def test_removal_affecting_other_conditions_stops_without_recreating_or_saving(self):
        from unittest.mock import patch
        from netmera_automation import filter_source
        rules=self.reader.segment_rules('demo-segment-2')
        rules['items'][1]['attributes'][0]['value_source']={'source':'google_sheets','sheets_id':'synthetic_sheet_12345','sheet':'Kaynak','date_column':'A','value_column':'B'}
        job=self.job({'type':'segment_run','label':'Sentetik koruma','name':'demo-segment-2','rebuild':True,**rules})
        self.page.add_init_script("const c=localStorage.getItem('mockClearFailure');window.mockAttributeRemoveRow=true;window.mockAttributeRemoveOther=c==='value';window.mockAttributeRemoveRule=c==='rule'")
        self.reader.timeout=1
        for case in ('value','rule'):
            with self.subTest(case=case):
                self.page.evaluate('c=>{localStorage.clear();localStorage.setItem("mockClearFailure",c);}',case)
                self.lines.clear()
                with patch.object(filter_source,'read',return_value=['synthetic']):
                    with self.assertRaises(PanelError) as caught:self.reader.fetch(job,TODAY,TODAY)
                self.assertEqual(caught.exception.code,'ATTRIBUTE_TARGET')
                self.assertIsNone(self.page.evaluate("localStorage.getItem('mockSegs')"))
                self.assertFalse(any('Yeni attribute satırı ekleniyor' in line for line in self.lines))

    def test_add_attribute_with_no_row_or_menu_stops_without_save(self):
        self.reader.segment_rules('demo-segment-2')
        self.page.evaluate('window.mockAttributeNoResponse=true')
        wrapper=self.page.locator('[class*="targeting-condition-list"] [class*="condition-wrapper"]').last
        self.reader.timeout=0.5
        with self.assertRaises(PanelError) as caught:
            self.reader._add_attribute(wrapper,{'name':'item category','operator':'In','values':['synthetic']},'Sentetik attribute')
        self.assertEqual(caught.exception.code,'ATTRIBUTE_TARGET')
        self.assertEqual(caught.exception.step,'ATTRIBUTE_ADD')
        self.assertIsNone(self.page.evaluate("localStorage.getItem('mockSegs')"))

    def test_dynamic_attribute_accepts_more_than_100_values_without_truncation(self):
        from unittest.mock import patch
        from netmera_automation import filter_source
        rules=self.reader.segment_rules('demo-segment-2')
        rules['items'][1]['attributes'][0]['value_source']={'source':'google_sheets','sheets_id':'synthetic_sheet_12345','sheet':'Kaynak','date_column':'A','value_column':'B'}
        job=self.job({'type':'segment_run','label':'Sentetik buyuk filtre','name':'demo-segment-2','rebuild':True,**rules})
        values=[f'synthetic-{n:03d}' for n in range(125)]
        self.page.add_init_script('window.mockCalcMs = 1')
        with patch.object(filter_source,'read',return_value=values):
            self.reader.fetch(job,TODAY,TODAY)
        self.assertEqual(self.stored()['941']['attributes'][1][0]['values'],values)
        self.assertEqual(self.page.evaluate("localStorage.getItem('mockAttrCleared')"),'1')  # aynı günün 125 değeri boyunca tek ×
        self.assertEqual(self.page.evaluate("localStorage.getItem('mockValuesAdded')"),'125')
        self.assertTrue(any('125/125' in message for message in self.lines))

    def test_new_attribute_keeps_values_in_inputs_and_fills_the_next_empty_box(self):
        from copy import deepcopy
        self.page.add_init_script('window.mockAttributeInputs=true;window.mockAttributeInline=true;window.mockValueDelayMs=120;window.mockCalcMs=1')
        raw=deepcopy(REBUILD)
        values=['synthetic-first','synthetic-second','synthetic-third']
        raw['items'][0]['attributes'][0]['values']=values
        self.reader.fetch(self.job(raw),TODAY,TODAY)
        self.assertEqual(self.stored()['990']['attributes'][0][0]['values'],values)
        self.assertEqual(self.page.evaluate("localStorage.getItem('mockValuesAdded')"),'3')

    def test_removed_dynamic_attribute_fills_all_476_inputs_without_overwriting(self):
        from unittest.mock import patch
        from netmera_automation import filter_source
        rules=self.reader.segment_rules('demo-segment-2')
        rules['items'][1]['attributes'][0]['value_source']={'source':'google_sheets','sheets_id':'synthetic_sheet_12345','sheet':'Kaynak','date_column':'A','value_column':'B'}
        job=self.job({'type':'segment_run','label':'Sentetik coklu kutu','name':'demo-segment-2','rebuild':True,**rules})
        values=[f'synthetic-{n:04d}' for n in range(476)]
        self.page.add_init_script('window.mockAttributeRemoveRow=true;window.mockAttributeInputs=true;window.mockAttributeInline=true;window.mockCalcMs=1')
        with patch.object(filter_source,'read',return_value=values):
            self.reader.fetch(job,TODAY,TODAY)
        self.assertEqual(self.stored()['941']['attributes'][1][0]['values'],values)
        self.assertEqual(self.page.evaluate("localStorage.getItem('mockValuesAdded')"),'476')
        self.assertEqual(self.page.evaluate("localStorage.getItem('mockAttrCleared')"),'1')
        self.assertTrue(any('476/476' in line for line in self.lines))

    def test_second_add_value_not_committed_stops_without_save_or_duplicate_click(self):
        from copy import deepcopy
        self.page.add_init_script('window.mockAttributeInputs=true;window.mockAttributeInline=true;window.mockValueNoCommitAt=2')
        self.reader.timeout=1
        raw=deepcopy(REBUILD)
        raw['items'][0]['attributes'][0]['values']=['synthetic-first','synthetic-second','synthetic-third']
        with self.assertRaises(PanelError) as caught:
            self.reader.fetch(self.job(raw),TODAY,TODAY)
        self.assertEqual(caught.exception.code,'ATTRIBUTE_UNVERIFIED')
        self.assertEqual(caught.exception.step,'ATTRIBUTE_VALUES')
        self.assertIn('2/3:',caught.exception.detail)
        self.assertNotIn('synthetic-second',caught.exception.detail)
        self.assertIsNone(self.page.evaluate("localStorage.getItem('mockSegs')"))
        self.assertEqual(self.page.evaluate("localStorage.getItem('mockValuesAdded')"),'1')

    def test_values_visible_only_in_condition_text_are_verified(self):
        from copy import deepcopy
        self.page.add_init_script('window.mockSummaryOnly=true;window.mockAttributeInline=true;window.mockCalcMs=1')
        raw=deepcopy(REBUILD)
        values=['synthetic-a','synthetic-b','synthetic-c']
        raw['items'][0]['attributes'][0]['values']=values
        self.reader.fetch(self.job(raw),TODAY,TODAY)
        self.assertEqual(self.stored()['990']['attributes'][0][0]['values'],values)
        self.assertEqual(self.page.evaluate("localStorage.getItem('mockValuesAdded')"),'3')

    def test_summary_only_dynamic_attribute_accepts_all_476_values(self):
        from unittest.mock import patch
        from netmera_automation import filter_source
        rules=self.reader.segment_rules('demo-segment-2')
        rules['items'][1]['attributes'][0]['value_source']={'source':'google_sheets','sheets_id':'synthetic_sheet_12345','sheet':'Kaynak','date_column':'A','value_column':'B'}
        job=self.job({'type':'segment_run','label':'Sentetik kural ozeti','name':'demo-segment-2','rebuild':True,**rules})
        values=[f'synthetic-{n:04d}' for n in range(476)]
        self.page.add_init_script('window.mockSummaryOnly=true;window.mockAttributeRemoveRow=true;window.mockAttributeInline=true;window.mockCalcMs=1')
        with patch.object(filter_source,'read',return_value=values):
            self.reader.fetch(job,TODAY,TODAY)
        saved=self.stored()['941']['attributes']
        self.assertEqual(saved[1][0]['values'],values)
        self.assertEqual(saved[0],[{'name':'item category','operator':'Equals','values':['DemoStore']}])
        self.assertEqual(self.page.evaluate("localStorage.getItem('mockValuesAdded')"),'476')
        self.assertEqual(self.page.evaluate("localStorage.getItem('mockAttrCleared')"),'1')

    def test_unchanged_summary_after_second_value_stops_before_save(self):
        from copy import deepcopy
        self.page.add_init_script('window.mockSummaryOnly=true;window.mockFreezeSummaryAt=2;window.mockAttributeInline=true')
        raw=deepcopy(REBUILD)
        raw['items'][0]['attributes'][0]['values']=['synthetic-a','synthetic-b','synthetic-c']
        self.reader.timeout=1
        with self.assertRaises(PanelError) as caught:
            self.reader.fetch(self.job(raw),TODAY,TODAY)
        self.assertEqual(caught.exception.code,'ATTRIBUTE_UNVERIFIED')
        self.assertIn('2/3:',caught.exception.detail)
        self.assertIsNone(self.page.evaluate("localStorage.getItem('mockSegs')"))

    def test_long_attribute_entry_can_be_cancelled_before_save(self):
        import threading
        from unittest.mock import patch
        from netmera_automation import filter_source
        rules=self.reader.segment_rules('demo-segment-2')
        rules['items'][1]['attributes'][0]['value_source']={'source':'google_sheets','sheets_id':'synthetic_sheet_12345','sheet':'Kaynak','date_column':'A','value_column':'B'}
        job=self.job({'type':'segment_run','label':'Sentetik iptal','name':'demo-segment-2','rebuild':True,**rules})
        self.reader.cancel=threading.Event()
        def log(message):
            self.lines.append(message)
            if '50/125' in message:self.reader.cancel.set()
        self.reader.log=log
        with patch.object(filter_source,'read',return_value=[f'synthetic-{n:03d}' for n in range(125)]):
            with self.assertRaises(PanelError) as caught:self.reader.fetch(job,TODAY,TODAY)
        self.assertEqual(caught.exception.code,'CANCELLED')
        self.assertIsNone(self.page.evaluate("localStorage.getItem('mockSegs')"))

    def test_rebuild_replaces_existing_rules(self):
        day = TODAY - timedelta(days=1)
        job = self.job({**REBUILD, "name": "demo-segment-2", "create_if_missing": False})
        self.reader.fetch(job, day, day)
        self.assertEqual(self.stored()["941"]["dates"], [[day.strftime("%d/%m/%Y")] * 2])  # eski iki kural silindi

    def test_unknown_segment_and_timeout_fail_without_number(self):
        with self.assertRaises(PanelError) as ctx:
            self.reader.fetch(self.job({"type": "segment_run", "label": "S", "name": "Olmayan Segment"}), TODAY, TODAY)
        self.assertEqual(ctx.exception.code, "NAME_NOT_FOUND_OR_AMBIGUOUS")
        self.page.evaluate("window.mockCalcMs = 1e9")
        self.page.add_init_script("window.mockCalcMs = 1e9")
        job = {**self.job({"type": "segment_run", "label": "S", "name": "demo-segment-2"}), "max_wait_minutes": 0.03}
        with self.assertRaises(PanelError) as ctx:
            self.reader.fetch(job, TODAY, TODAY)
        self.assertEqual(ctx.exception.code, "SEGMENT_CALC_TIMEOUT")

    def test_existing_segment_rules_are_read_without_saving(self):
        rules = self.reader.segment_rules("demo-segment-2")
        self.assertIsNone(self.page.evaluate("localStorage.getItem('mockSegs')"))  # kaydedilmedi
        first, second = rules["items"]
        self.assertEqual(rules["connector"], "AND")
        self.assertEqual((first["value"], first["steps"], first["numbers"]), ("add to cart", ["Did", "more than", "between dates"], [0]))
        self.assertEqual(first["attributes"], [{"name": "item category", "operator": "Equals", "values": ["DemoStore"]}])
        self.assertEqual((second["value"], second["steps"]), ("purchase", ["Did not", "between dates"]))
        # Kutu boş; değerler listedeki 'Show details' özetinden ('… (item category in DemoStore, DemoCategory)') alınır.
        self.assertEqual(second["attributes"], [{"name": "item category", "operator": "In", "values": ["DemoStore", "DemoCategory"]}])
        J.normalize_job({"type": "segment_run", "label": "S", "name": "demo-segment-2", "rebuild": True, **rules})  # editöre yazılabilir


if __name__ == "__main__":
    unittest.main()
