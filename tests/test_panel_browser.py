"""Panel okuyucusunu gerçek Chrome ile SENTETİK bir sahte panelde çalıştırır.

Tüm istekler Playwright tarafından yakalanır; hiçbir istek internete veya
Netmera'ya gitmez. Bu test kodun akışını doğrular, gerçek Netmera DOM'unu değil.
Chrome/Edge veya playwright yoksa atlanır.
"""
from datetime import date, datetime, timedelta
from pathlib import Path
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from netmera_automation import jobs as J, runner, store
from netmera_automation.panel import PanelError, PanelReader
from tests.helpers import TempDataCase, event_job, browser_test_args

MOCK = (Path(__file__).with_name("mock_panel.html")).read_text(encoding="utf-8")

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None


def offline_route(route):
    request = route.request
    if request.url.startswith("https://panel.netmera.com/") and request.resource_type == "document":
        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=MOCK)
    elif request.url.startswith("https://panel.netmera.com/") and request.resource_type in ("xhr", "fetch"):
        route.fulfill(status=200, content_type="application/json", body="{}")  # sahte sayaç isteği
    else:
        route.abort()


@unittest.skipIf(sync_playwright is None, "playwright yok")
class MockPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(channel="chrome", headless=True,args=browser_test_args())
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
        self.reader = PanelReader(self.page, "Sentetik App", timeout_seconds=10, log=lambda _: None)
        self.reader.open_panel()
        today = datetime.now(ZoneInfo("Europe/Istanbul")).date()
        self.yesterday = today - timedelta(days=1)

    def tearDown(self):
        self.context.close()

    def fetch(self, raw, first=None, last=None):
        job = J.normalize_job(raw)
        return self.reader.fetch(job, first or self.yesterday, last or self.yesterday)

    def test_event_variants(self):
        plain, _ = self.fetch(event_job(event="Open App", metric="unique_user_count"))
        self.assertGreater(plain["All Total"], 0)
        contains, _ = self.fetch(event_job(event="View Content", filters=[{"property": "Content Id", "operator": "Contains", "values": ["DemoCampaign"]}]))
        self.assertNotEqual(contains["All Total"], plain["All Total"])
        multi, _ = self.fetch(event_job(event="view item list", columns=["All Total", "iOS"],
                                        filters=[{"property": "Vertical", "operator": "In", "values": ["DemoCategory", "DemoStore"]}]))
        self.assertEqual(set(multi), {"All Total", "iOS"})
        self.assertLess(multi["iOS"], multi["All Total"])  # platform sütunu ayrı okunur, toplanmaz
        week, _ = self.fetch(event_job(event="view item", period={"type": "last_n_days", "days": 7}),
                             self.yesterday - timedelta(days=6), self.yesterday)
        self.assertGreater(week["All Total"], 0)
        today = self.yesterday + timedelta(days=1)
        unique, _ = self.fetch(event_job(event="Open App", metric="unique_user_count", period={"type": "today"}), today, today)
        self.assertGreater(unique["All Total"], 0)
        exists, _ = self.fetch(event_job(event="view item", filters=[{"property": "item brand", "operator": "Exists"}]))
        self.assertGreater(exists["All Total"], 0)

    def test_session_uses_post_find_summary_and_requested_platforms(self):
        result, _ = self.fetch(event_job(event='View Content', metric='by_session', columns=['All Total','iOS'],
                                        filters=[{'property':'Content Id','operator':'Contains','values':['DemoCampaign']}]))
        d=self.yesterday.day
        self.assertEqual(result, {'All Total':328+d, 'iOS':127+d})  # sahte panel: taban + ayın günü
        radio = self.page.locator('input[value="SESSION"]')
        self.assertTrue(radio.is_checked())
        # Üst tablodaki toplam veya alt bölümdeki varsayılan TOTAL alınmamalı.
        self.assertNotEqual(result['All Total'],9001)
        self.assertEqual(self.page.locator('[class*="breakdownSection_"] table').count(),1)
        first=self.yesterday-timedelta(days=6)
        week, _=self.fetch(event_job(event='Open App',metric='by_session',period={'type':'custom','start':first.isoformat(),'end':self.yesterday.isoformat()}),first,self.yesterday)
        self.assertEqual(week['All Total'],sum(321+(first+timedelta(days=n)).day for n in range(7)))  # günler toplanır

    def test_event_tag_applies_with_all_metrics_and_survives_find(self):
        totals=[]
        for metric in ('total_event_count','unique_user_count','by_session'):
            values, _ = self.fetch(event_job(event='Open App',metric=metric,audience='tag',audience_name='Sentetik Tag'))
            totals.append(values['All Total'])
            # Seçili Tag kişi sayısıyla görünür; benzer adlı 'over_…' ve '…_eski' seçilmemeli.
            # Kartta event kutusu da var (prod gibi): seçililer event + sayılı Tag.
            self.assertEqual(self.reader._audience_root().locator('.ant-select-selection-item').all_inner_texts(),['Open App','Sentetik Tag(12.345)'])
        self.assertEqual(totals[-1],334+self.yesterday.day)
        self.assertEqual(len(set(totals)),3)
        all_users, _ = self.fetch(event_job(event='Open App',metric='by_session'))
        self.assertEqual(all_users['All Total'],321+self.yesterday.day)
        tagged_filter, _ = self.fetch(event_job(event='View Content',metric='by_session',audience='tag',audience_name='Sentetik Tag',
                                               filters=[{'property':'Content Id','operator':'Contains','values':['DemoCampaign']}]))
        self.assertEqual(tagged_filter['All Total'],341+self.yesterday.day)

    def test_days_of_month_reads_each_day_in_one_query_and_matches_single_days(self):
        first=self.yesterday-timedelta(days=2)
        job=J.normalize_job(event_job(event='View Content',columns=['All Total','iOS'],
                                      filters=[{'property':'Content Id','operator':'Contains','values':['DemoCampaign']}]))
        days=self.reader.fetch_days(job,first,self.yesterday)
        self.assertEqual(sorted(days),[first+timedelta(days=n) for n in range(3)])
        # Aynı günler tek tek okunduğunda üst tablo ile birebir aynı sayılar.
        for day,values in days.items():
            single,_=self.fetch(event_job(event='View Content',columns=['All Total','iOS'],
                                          filters=[{'property':'Content Id','operator':'Contains','values':['DemoCampaign']}]),day,day)
            self.assertEqual(values,single)
        unique=J.normalize_job(event_job(event='Open App',metric='unique_user_count'))
        self.assertEqual(len(self.reader.fetch_days(unique,first,self.yesterday)),3)
        with self.assertRaises(PanelError):
            self.reader.fetch_days(unique,self.yesterday-timedelta(days=10),self.yesterday)  # 10 günden uzun: tek sayfa değil

    def test_same_query_metrics_are_read_with_one_find(self):
        base=dict(event='View Content',filters=[{'property':'Content Id','operator':'Contains','values':['DemoCampaign']}])
        jobs=[J.normalize_job({**event_job(**base,metric=m,label=m),'id':m}) for m in ('total_event_count','unique_user_count','by_session')]
        clicks=[]
        original=self.reader._fresh_events
        self.reader._fresh_events=lambda:(clicks.append(1),original())[1]
        out=self.reader.fetch_many(jobs,self.yesterday,self.yesterday)
        self.assertEqual(len(clicks),1)
        self.assertEqual(out['by_session']['All Total'],328+self.yesterday.day)
        for metric in ('total_event_count','unique_user_count'):
            single,_=self.fetch(event_job(**base,metric=metric))
            self.assertEqual(out[metric],single)
        self.assertNotEqual(out['total_event_count'],out['unique_user_count'])

    def test_single_day_session_reads_summary_without_breakdown_and_checks_heading(self):
        result,_=self.fetch(event_job(event='Open App',metric='by_session'))
        self.assertEqual(result['All Total'],321+self.yesterday.day)
        section=self.page.locator('[class*="breakdownSection_"]')
        self.assertEqual(section.locator('button.ant-btn-primary').count(),0)  # kırılıma basılmadı: özet okundu
        section.locator('h4').evaluate("e => e.textContent = 'Başka Event – Event Analytics by Dimensions'")
        with self.assertRaises(PanelError) as ctx:
            self.reader._session_values(J.normalize_job(event_job(event='Open App',metric='by_session')),self.yesterday,self.yesterday)
        self.assertEqual(ctx.exception.code,'SESSION_SECTION_UNVERIFIED')
        self.assertIn('Başka Event',ctx.exception.detail)

    def test_breakdown_table_parsing(self):
        from netmera_automation.panel import extract_breakdown
        cols=[[c,1] for c in J.COLUMNS]
        days=[['Days of a Month',1],*cols]
        rows=[['15','835.312','835.312','528.531','306.781','0','0','0'],['16','725.412','725.412','474.911','250.501','0','0','0']]
        got=extract_breakdown([days],rows,['All Total','iOS'],date(2026,10,15),date(2026,10,16))
        self.assertEqual(got,{date(2026,10,15):{'All Total':835312,'iOS':528531},date(2026,10,16):{'All Total':725412,'iOS':474911}})
        self.assertEqual(extract_breakdown([days],rows[:1],['All Total'],date(2026,10,15),date(2026,10,16)),{date(2026,10,15):{'All Total':835312}})
        month_end=extract_breakdown([days],[['31',*'1234567'],['1',*'7654321']],['All Total'],date(2026,10,31),date(2026,11,1))
        self.assertEqual(sorted(month_end),[date(2026,10,31),date(2026,11,1)])
        for headers,bad in (([[['Month',1],*cols]],[['October',*'1234567']]),([days],[['17',*'1234567']]),
                            ([days],[rows[0],rows[0]]),([days],[['15','-','0','0','0','0','0','0']])):
            with self.subTest(bad=bad),self.assertRaises(PanelError):
                extract_breakdown(headers,bad,['All Total'],date(2026,10,15),date(2026,10,16))

    def test_missing_or_duplicate_tag_never_falls_back_to_all_users(self):
        for name in ('Yok Etiket','Çift Etiket'):
            with self.subTest(name=name), self.assertRaises(PanelError) as ctx:
                self.fetch(event_job(event='Open App',audience='tag',audience_name=name))
            self.assertEqual((ctx.exception.code,ctx.exception.step),('OPTION_MISSING_OR_AMBIGUOUS','AUDIENCE_CHECK'))

    def test_session_rejects_stale_table_and_ambiguous_section(self):
        raw=event_job(event='Open App',metric='by_session')
        self.fetch(event_job(event='Open App'))  # Find, alt özet ve TOTAL varsayılanı
        self.page.evaluate('window.mockFreezeSession = true')
        self.reader.timeout=1
        with self.assertRaises(PanelError) as ctx:
            self.reader._session_values(J.normalize_job(raw),self.yesterday,self.yesterday)
        self.assertEqual(ctx.exception.code,'COUNT_REFRESH_UNVERIFIED')
        self.page.locator('[class*="breakdownSection_"]').evaluate('e => e.after(e.cloneNode(true))')
        with self.assertRaises(PanelError) as ctx:
            self.reader._session_values(J.normalize_job(raw),self.yesterday,self.yesterday)
        self.assertEqual(ctx.exception.code,'SESSION_SECTION_UNVERIFIED')

    def test_session_rejects_breakdown_rows_wrong_header_and_changed_audience(self):
        from netmera_automation.panel import extract_session_count
        headers=[[[c,1] for c in J.COLUMNS]]
        for broken_headers, rows in ((headers,[['1']*7,['2']*7]),
                                     ([[['Month',1],*headers[0]]],[['March',*['1']*7]]),
                                     (headers,[['-']*7])):
            with self.subTest(rows=rows), self.assertRaises(PanelError):
                extract_session_count(broken_headers,rows,'All Total')
        self.fetch(event_job(event='Open App',audience='tag',audience_name='Sentetik Tag'))
        self.reader._button('All Users',self.reader._audience_root()).click()
        with self.assertRaises(PanelError) as ctx:
            self.reader._verify_event_audience(J.normalize_job(event_job(audience='tag',audience_name='Sentetik Tag')))
        self.assertEqual(ctx.exception.code,'AUDIENCE_UNVERIFIED')

    def test_no_record_is_error_unless_job_allows_zero(self):
        job = event_job(event="view item", filters=[{"property": "item category", "operator": "Equals", "values": ["demostore"]}])
        with self.assertRaises(PanelError) as ctx:
            self.fetch(job)
        self.assertEqual(ctx.exception.code, "NO_RECORD")
        self.page.goto("https://panel.netmera.com/")
        zero, _ = self.fetch({**job, "empty_is_zero": True})
        self.assertEqual(zero, {"All Total": 0})
        zero_session, _ = self.fetch({**job, 'metric':'by_session', 'empty_is_zero':True})
        self.assertEqual(zero_session, {'All Total':0})
        correct, _ = self.fetch({**job, "filters": [{"property": "item category", "operator": "Equals", "values": ["DemoStore"]}]})
        self.assertGreater(correct["All Total"], 0)

    def test_people_tags_segments(self):
        v = lambda value: {"kind": "rule", "field": "App Version", "value": value, "operator": "Greater Than"}  # noqa: E731
        installed = {"kind": "group", "connector": "AND", "rules": [{"field": "App Installation", "value": "Installed"}]}
        base = {"type": "people", "label": "Total User", "connector": "OR"}
        user_way, _ = self.fetch({**base, "items": [v("Android 1.2.3"), v("iOS 1.2.3"), installed]})
        flat_and, _ = self.fetch({**base, "connector": "AND", "items": [v("Android 1.2.3"), v("iOS 1.2.3"), {"kind": "rule", "field": "App Installation", "value": "Installed"}]})
        two_in_group, _ = self.fetch({**base, "items": [{"kind": "group", "connector": "OR", "rules": [
            {"field": "App Version", "value": "Android 1.2.3", "operator": "Greater Than"}, {"field": "Platform", "value": "iOS"}]}]})
        counter, _ = self.fetch({**base, "items": [v("Android 1.2.3"), v("iOS 1.2.3"), installed], "count_from": "people_match"})
        # Yapı sayıyı gerçekten değiştirir; alttaki toplam ile sayaç ayrı kaynaklardır.
        self.assertEqual(len({user_way[None], flat_and[None], two_in_group[None]}), 3)
        self.assertNotEqual(counter[None], user_way[None])
        tag, _ = self.fetch({"type": "tag", "label": "T", "name": "Sentetik Tag"})
        self.assertEqual(tag[None], 1204)
        segment, note = self.fetch({"type": "segment", "label": "S", "name": "Sentetik Segment"})
        self.assertEqual(segment[None], 45010)
        self.assertIn("09/03/2026", note)

    def test_errors_are_explicit(self):
        cases = [
            (event_job(event="Olmayan Event"), "OPTION_MISSING_OR_AMBIGUOUS", "EVENT_SELECT"),
            (event_job(event="View Content", filters=[{"property": "Olmayan", "operator": "Equals", "values": ["x"]}]), "OPTION_MISSING_OR_AMBIGUOUS", "FILTER_PROPERTY"),
            ({"type": "people", "label": "P", "items": [{"field": "App Version", "value": "iOS 9.9.9"}]}, "OPTION_MISSING_OR_AMBIGUOUS", "PEOPLE_VALUE"),
            ({"type": "people", "label": "P", "items": [{"field": "Platform", "value": "Chrome"}]}, "OPTION_MISSING_OR_AMBIGUOUS", "PEOPLE_VALUE"),
            ({"type": "tag", "label": "T", "name": "Sentetik"}, "NAME_NOT_FOUND_OR_AMBIGUOUS", "RESULT_READ"),
        ]
        for raw, code, step in cases:
            with self.subTest(code=code):
                self.page.goto("https://panel.netmera.com/")
                with self.assertRaises(PanelError) as ctx:
                    self.fetch(raw)
                self.assertEqual((ctx.exception.code, ctx.exception.step), (code, step))
        wrong = PanelReader(self.page, "Başka Uygulama", timeout_seconds=5, log=lambda _: None)
        with self.assertRaises(PanelError) as ctx:
            wrong.verify_app()
        self.assertEqual(ctx.exception.code, "APP_MISMATCH")

    def test_event_catalog_scrolls_virtual_list(self):
        names = self.reader.event_catalog()
        self.assertEqual(len(names), 45)
        self.assertIn("Sentetik Event 40", names)

    def test_behavior_steps_change_default_operator_and_date_mode(self):
        result, _ = self.fetch({"type": "people", "label": "Sentetik Davranış Sayımı", "items": [
            {"field": "Behavior", "value": "Sentetik Davranış", "steps": ["exactly", "in the last", "Week"], "numbers": [3, 9]}]})
        self.assertGreater(result[None], 0)
        row = self.page.locator('[class*="condition-wrapper"]').last
        buttons = row.locator('button').all_inner_texts()
        for selected in ("exactly", "in the last", "Week"):
            self.assertIn(selected, buttons)
        self.assertNotIn("more than", buttons)
        self.assertNotIn("between dates", buttons)
        self.assertEqual(row.locator('input[role="spinbutton"]').evaluate_all('es => es.map(e => e.value)'), ['3', '9'])

    def test_behavior_steps_only_change_latest_rule(self):
        job = {"type": "people", "label": "İki sentetik davranış", "items": [
            {"field": "Behavior", "value": "Sentetik Davranış", "steps": ["less than", "in the last", "Day"], "numbers": [1, 7]},
            {"field": "Behavior", "value": "Sentetik Davranış", "steps": ["exactly", "in the last", "Month"], "numbers": [3, 9]}]}
        self.fetch(job)
        rows = self.page.locator('[class*="condition-wrapper"]')
        self.assertIn("less than", rows.nth(0).locator('button').all_inner_texts())
        self.assertIn("Day", rows.nth(0).locator('button').all_inner_texts())
        self.assertIn("exactly", rows.nth(1).locator('button').all_inner_texts())
        self.assertIn("Month", rows.nth(1).locator('button').all_inner_texts())

    def test_behavior_unknown_step_does_not_guess_a_control(self):
        with self.assertRaises(PanelError) as ctx:
            self.fetch({"type": "people", "label": "Geçersiz adım", "items": [
                {"field": "Behavior", "value": "Sentetik Davranış", "steps": ["unsupported comparison"]}]})
        self.assertEqual((ctx.exception.code, ctx.exception.step), ("PEOPLE_STEP_CONTROL_MISSING", "PEOPLE_STEP"))

    def test_people_date_ranges_are_written_in_their_own_rules(self):
        def rule(start, end):
            return {"field":"Behavior", "value":"Sentetik Davranış", "steps":["Did", "exactly", "between dates"],
                    "numbers":[1], "date_range":{"start":start, "end":end}}
        self.fetch({"type":"people", "label":"İki tarih aralığı", "items":[
            rule("2026-03-01", "2026-03-03"), {"kind":"group", "rules":[rule("2026-03-04", "2026-03-04")]}]})
        rows = self.page.locator('[class*="condition-wrapper"]')
        self.assertEqual(rows.nth(0).get_by_placeholder('DD/MM/YYYY').evaluate_all('es => es.map(e => e.value)'),
                         ['01/03/2026', '03/03/2026'])
        self.assertEqual(rows.nth(1).get_by_placeholder('DD/MM/YYYY').evaluate_all('es => es.map(e => e.value)'),
                         ['04/03/2026', '04/03/2026'])

    def test_people_date_writes_fail_closed_and_final_tree_detects_changes(self):
        job = J.normalize_job({"type":"people", "label":"Tarih kontrolü", "items":[
            {"field":"Behavior", "value":"Sentetik Davranış", "numbers":[2],
             "date_range":{"start":"2026-03-01", "end":"2026-03-03"}}]})
        self.reader.fetch(job, self.yesterday, self.yesterday)
        root = self.page.get_by_role('tabpanel', name='Filter people')
        dates = root.get_by_placeholder('DD/MM/YYYY')
        dates.first.fill('02/03/2026')
        with self.assertRaises(PanelError) as ctx:
            self.reader._verify_people(job)
        self.assertEqual(ctx.exception.code, 'PEOPLE_STRUCTURE_MISMATCH')
        dates.first.evaluate("e => e.readOnly = true")
        with self.assertRaises(PanelError) as ctx:
            self.reader._enter_people_dates(root, job['items'][0]['date_range'], 'Sentetik')
        self.assertEqual(ctx.exception.code, 'PEOPLE_DATE_CONTROL_MISSING')
        dates.first.evaluate("e => {e.readOnly = false; e.onblur = () => e.value = ''}")
        with self.assertRaises(PanelError) as ctx:
            self.reader._enter_people_dates(root, job['items'][0]['date_range'], 'Sentetik')
        self.assertEqual(ctx.exception.code, 'PEOPLE_DATE_MISMATCH')
        dates.last.evaluate("e => e.remove()")
        with self.assertRaises(PanelError) as ctx:
            self.reader._enter_people_dates(root, job['items'][0]['date_range'], 'Sentetik')
        self.assertEqual(ctx.exception.code, 'PEOPLE_DATE_CONTROL_MISSING')

    def test_people_dates_support_native_date_input_without_format_guessing(self):
        job = J.normalize_job({"type":"people", "label":"Yerel tarih kutusu", "items":[
            {"field":"Behavior", "value":"Sentetik Davranış", "date_range":{"start":"2026-03-01", "end":"2026-03-02"}}]})
        self.reader.fetch(job, self.yesterday, self.yesterday)
        root = self.page.get_by_role('tabpanel', name='Filter people')
        root.get_by_placeholder('DD/MM/YYYY').evaluate_all("es => es.forEach(e => e.type = 'date')")
        self.reader._enter_people_dates(root, job['items'][0]['date_range'], 'Sentetik')
        self.reader._verify_people(job)
        self.assertEqual(root.locator('input[type=date]').evaluate_all('es => es.map(e => e.value)'), ['2026-03-01','2026-03-02'])

    def test_behavior_step_ignores_other_open_popup_and_is_idempotent(self):
        self.reader._goto('/targeting/people')
        root = self.page.get_by_role('tabpanel', name='Filter people')
        self.reader._add_people_rule(root, self.reader._text_button('Add Filter Rule', root),
                                     {"field": "Behavior", "value": "Sentetik Davranış"})
        # Unrelated popup contains the same desired label. The row's own trigger must open.
        self.page.evaluate("""() => {
            const menu = document.createElement('div'); menu.className = 'ant-popover';
            menu.innerHTML = '<button>exactly</button>';
            menu.firstChild.onclick = () => document.body.setAttribute('data-wrong-popup-clicked', 'yes');
            document.body.append(menu);
        }""")
        self.reader._choose_step(root, 'exactly', 'Sentetik kural', field='Behavior')
        self.assertIsNone(self.page.locator('body').get_attribute('data-wrong-popup-clicked'))
        self.assertEqual(root.get_by_role('button', name='exactly', exact=True).count(), 1)
        self.reader._choose_step(root, 'exactly', 'Sentetik kural', field='Behavior')
        self.assertEqual(self.page.locator('.ant-popover:visible').count(), 0)

    def test_people_verification_checks_behavior_steps_and_number_values(self):
        job = J.normalize_job({"type": "people", "label": "Denetlenen sentetik davranış", "items": [
            {"field": "Behavior", "value": "Sentetik Davranış", "steps": ["exactly", "in the last", "Week"], "numbers": [3, 9]}]})
        self.reader.fetch(job, self.yesterday, self.yesterday)
        root = self.page.get_by_role('tabpanel', name='Filter people')
        self.reader._choose_step(root, 'more than', 'Sentetik kural', field='Behavior')
        with self.assertRaises(PanelError) as ctx:
            self.reader._verify_people(job)
        self.assertEqual(ctx.exception.code, 'PEOPLE_STRUCTURE_MISMATCH')
        self.reader._choose_step(root, 'exactly', 'Sentetik kural', field='Behavior')
        root.locator('input[role="spinbutton"]').first.fill('4')
        with self.assertRaises(PanelError) as ctx:
            self.reader._verify_people(job)
        self.assertEqual(ctx.exception.code, 'PEOPLE_STRUCTURE_MISMATCH')


@unittest.skipIf(sync_playwright is None, "playwright yok")
class ProdRunnerOnMockTests(TempDataCase):
    def test_full_prod_run_on_mock_panel(self):
        class OfflineBrowser(runner.Browser):
            def __enter__(self):
                super().__enter__()
                self.context.route("**/*", offline_route)
                return self

        store.ensure_defaults()
        profile = next(p for p in store.list_profiles() if p["source"] == "netmera")
        seed_jobs = store.seed()["jobs"]
        profile.update(netmera_app="Sentetik App", output_file=str(self.tmp / "cikti" / "prod.xlsx"), headless=True,
                       timeout_seconds=10, jobs=seed_jobs + [{"type": "segment", "label": "Segment", "name": "Sentetik Segment"}])
        profile = store.save_profile(profile)
        try:
            with patch("netmera_automation.runner.Browser", OfflineBrowser):
                summary = runner.run_profile(profile, log=lambda _: None)
        except PanelError as exc:
            if exc.code == "BROWSER_NOT_FOUND":
                self.skipTest("Chrome yok")
            raise
        statuses = {r["job_label"]: r["status"] for r in summary["rows"]}
        # Varsayılan 'demostore' (küçük harf) prod'daki gibi kayıt bulmaz; diğerleri okunur.
        self.assertEqual(statuses.pop("View Item - DemoStore - Total"), "NO_RECORD")
        self.assertEqual(set(statuses.values()), {"OK"}, statuses)
        self.assertEqual(summary["excel"], "Excel güncellendi.")
        self.assertTrue((store.profile_dir(profile["id"]) / "browser-profile").is_dir())


if __name__ == "__main__":
    unittest.main()
