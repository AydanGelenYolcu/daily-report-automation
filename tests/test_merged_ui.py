"""Birleşik arayüz (Netmera + GA4): kenar çubuğu, proje eşlemesi, GA4 çekmecesi, hazır raporlar, dışa aktarma.
Gerçek yerel API, geçici/sentetik veri; harici adreslere istek engellenir."""
from http.server import ThreadingHTTPServer
import os
import threading
import unittest

from netmera_automation import server, store
from tests.helpers import TempDataCase, browser_test_args
from tests.test_panel_browser import sync_playwright


@unittest.skipIf(sync_playwright is None, "playwright yok")
class MergedUiTests(TempDataCase):
    def test_cell_formula_try_respects_run_date_and_preserves_US_date_order(self):
        from openpyxl import Workbook
        from playwright.sync_api import expect
        path=self.tmp/'sentetik.xlsx'; book=Workbook();book.active.title='Rapor'
        for day in range(1,32):book.active.cell(78,day+2,f'10/{day}/2026')
        book.active['W79']=17.25;book.save(path);book.close()
        p=store.load_profile('demo-sahte-veri')
        p['jobs']=[{'type':'formula','label':'Sentetik hücre','expression':'A','variables':[{'name':'A','kind':'cell',
            'source':'excel','file':str(path),'sheet':'Rapor','mode':'header_lookup','header_row':78,'value_row':79,
            'date_basis':'data_day','month_first':True}]}]
        store.save_profile(p);self.page.evaluate('window.NM.refresh()')
        self.page.locator('[data-edit="0"]').click()
        expect(self.page.locator('[data-vk=month_first]')).to_have_value('1')
        self.page.locator('[data-vk=month_first]').select_option('')
        self.page.locator('[data-vk=month_first]').select_option('1')
        self.page.locator('#jobSaveBtn').click()
        expect(self.page.locator('#jobDlg')).not_to_be_visible()
        self.assertTrue(store.load_profile(p['id'])['jobs'][0]['variables'][0]['month_first'])
        self.page.locator('[data-tab=run]').click()
        self.page.locator('#runStart').fill('2026-10-21');self.page.locator('#runEnd').fill('2026-10-21')
        self.page.locator('[data-tab=jobs]').click();self.page.locator('[data-try="0"]').click()
        expect(self.page.locator('#resBody')).to_contain_text('17.25',timeout=30000)
        result=store.read_json(store.profile_dir(p['id'])/'son-calisma.json')['rows'][0]
        self.assertEqual(result['status'],'OK');self.assertTrue(result['period_start'].startswith('2026-10-21'))
        self.assertIn('W79',result['note']);self.assertEqual(self.js_errors,[])

    def test_project_export_single_and_selected_bulk_are_real_json_downloads(self):
        import json
        from pathlib import Path
        from playwright.sync_api import expect
        self.page.locator('.nav-oge[href="#/projeler"]').click()
        with self.page.expect_download() as captured:
            self.page.locator('[data-export-project="demo-sahte-veri"]').click()
        download = captured.value
        bundle = json.loads(Path(download.path()).read_text(encoding='utf-8'))
        self.assertTrue(download.suggested_filename.endswith('.json'))
        self.assertEqual([p['id'] for p in bundle['projects']], ['demo-sahte-veri'])
        self.assertEqual(bundle['format'], 'netmera-projects')
        self.assertEqual(bundle['projects'][0]['jobs'], store.load_profile('demo-sahte-veri')['jobs'])
        self.page.locator('#exportProjectsBtn').click()
        expect(self.page.locator('#exportProjectsList input')).to_have_count(2)
        self.page.locator('#exportProjectsAll').uncheck()
        expect(self.page.locator('#exportProjectsSaveBtn')).to_be_disabled()
        self.page.locator('#exportProjectsList input[value="demo-sahte-veri"]').check()
        expect(self.page.locator('#exportProjectsCount')).to_have_text('1 proje seçili')
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.assertTrue(self.page.locator('#exportProjectsDlg').evaluate('e => e.scrollWidth <= e.clientWidth + 1'))
        with self.page.expect_download() as captured:
            self.page.locator('#exportProjectsSaveBtn').click()
        exported = json.loads(Path(captured.value.path()).read_text(encoding='utf-8'))
        self.assertEqual([p['id'] for p in exported['projects']], ['demo-sahte-veri'])
        expect(self.page.locator('#exportProjectsDlg')).not_to_be_visible()
        self.assertEqual(self.js_errors, [])

    def test_project_import_preview_subset_conflict_and_explicit_enabled_option(self):
        import json
        from pathlib import Path
        from unittest.mock import patch
        from playwright.sync_api import expect
        from netmera_automation import project_transfer as P
        from tests.helpers import event_job
        from netmera_automation import jobs as J
        source = store.load_profile('demo-sahte-veri')
        event = J.normalize_job(event_job(label='Sentetik aktarım olayı'))
        source.update(jobs=[event, {'type': 'formula', 'label': 'Sentetik aktarım hesabı', 'expression': 'A / 2',
            'variables': [{'name': 'A', 'kind': 'job', 'job_id': event['id'], 'column': 'All Total'}]}], excel_mode='mapped_cells',
            output_file=str(self.tmp / 'rapor.xlsx'), excel_mappings=[{'job_id': event['id'], 'column': 'All Total',
                'sheet': 'Rapor', 'mode': 'fixed_cell', 'cell': 'B4'}])
        source = store.save_profile(source)
        bundle = P.export_projects(['demo-sahte-veri', 'production'])['bundle']
        payload = {'name': 'sentetik-projeler.json', 'mimeType': 'application/json',
                   'buffer': json.dumps(bundle, ensure_ascii=False).encode('utf-8')}
        self.page.locator('.nav-oge[href="#/projeler"]').click()
        for keep, suffix in [(False, ' (2)'), (True, ' (3)')]:
            self.page.locator('#importProjectsBtn').click()
            self.page.locator('#projectImportFile').set_input_files(payload)
            expect(self.page.locator('#importProjectsList input')).to_have_count(2)
            self.assertEqual(len(store.list_profiles()), 2 if not keep else 3)  # önizleme yazmaz
            self.page.locator('#importProjectsList input[value="1"]').uncheck()
            self.page.locator('#importProjectsKeepEnabled').set_checked(keep)
            expect(self.page.locator('#importProjectsCount')).to_have_text('1 proje seçili')
            if not keep:
                self.page.set_viewport_size({'width': 390, 'height': 844})
                self.assertTrue(self.page.locator('#importProjectsDlg').evaluate('e => e.scrollWidth <= e.clientWidth + 1'))
                self.page.set_viewport_size({'width': 1280, 'height': 1000})
            with patch('netmera_automation.scheduler.install') as install:
                self.page.locator('#importProjectsSaveBtn').click()
                expect(self.page.locator('#importProjectsDlg')).not_to_be_visible()
                expect(self.page.locator('#projectCards')).to_contain_text(source['name'] + suffix)
            install.assert_not_called()
            new = next(p for p in store.list_profiles() if p['name'] == source['name'] + suffix)
            self.assertEqual(new['jobs'], [{**j, 'enabled': keep} for j in source['jobs']])
            self.assertEqual(new['excel_mappings'], source['excel_mappings'])
            self.assertEqual(new['output_file'], source['output_file'])
            self.assertEqual([p.name for p in store.profile_dir(new['id']).iterdir()], ['profil.json'])
        self.assertEqual(store.load_profile(source['id']), source)
        self.assertFalse(Path(source['output_file']).exists())
        self.assertEqual(self.js_errors, [])

    def test_project_import_bad_file_escaped_preview_and_cancel_do_not_write(self):
        import json
        from playwright.sync_api import expect
        from netmera_automation import project_transfer as P
        self.page.locator('.nav-oge[href="#/projeler"]').click()
        self.page.locator('#importProjectsBtn').click()
        for raw, expected in [(b'{bad json', 'JSON dosyası okunamadı'), (b'{"name":"\xff"}', 'JSON dosyası okunamadı'),
                              (b'{"format":"foreign"}', 'Desteklenmeyen proje dosyası')]:
            self.page.locator('#projectImportFile').set_input_files({'name': 'sentetik.json', 'mimeType': 'application/json', 'buffer': raw})
            expect(self.page.locator('#importProjectsErr')).to_contain_text(expected)
            expect(self.page.locator('#importProjectsSaveBtn')).to_be_disabled()
        bundle = P.export_projects(['demo-sahte-veri'])['bundle']
        name = '<img src=x onerror="window.__probe=1">'
        bundle['projects'][0]['name'] = name
        self.page.locator('#projectImportFile').set_input_files({'name': 'sentetik.json', 'mimeType': 'application/json',
            'buffer': b'\xef\xbb\xbf' + json.dumps(bundle).encode('utf-8')})
        expect(self.page.locator('#importProjectsList')).to_contain_text(name)
        expect(self.page.locator('#importProjectsList img')).to_have_count(0)
        self.assertEqual(self.page.evaluate('window.__probe || 0'), 0)
        self.page.locator('#importProjectsDlg .dlg-f [data-close]').click()
        self.assertEqual(len(store.list_profiles()), 2)
        self.page.locator('#importProjectsBtn').click()
        expect(self.page.locator('#importProjectsErr')).to_be_empty()
        expect(self.page.locator('#importProjectsSaveBtn')).to_be_disabled()
        expect(self.page.locator('#projectImportFile')).to_have_value('')
        self.assertEqual(self.js_errors, [])

    def test_project_import_busy_prevents_second_submit_and_escape(self):
        import json
        from unittest.mock import patch
        from playwright.sync_api import expect
        from netmera_automation import project_transfer as P
        bundle = P.export_projects(['demo-sahte-veri'])['bundle']
        self.page.locator('.nav-oge[href="#/projeler"]').click()
        self.page.locator('#importProjectsBtn').click()
        self.page.locator('#projectImportFile').set_input_files({'name': 'sentetik.json', 'mimeType': 'application/json',
            'buffer': json.dumps(bundle).encode('utf-8')})
        expect(self.page.locator('#importProjectsSaveBtn')).to_be_enabled()
        entered, release = threading.Event(), threading.Event()
        original = P.import_projects
        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise ValueError('synthetic timeout')
            return original(*args, **kwargs)
        with patch.object(P, 'import_projects', side_effect=delayed) as run:
            try:
                self.page.locator('#importProjectsSaveBtn').click()
                self.assertTrue(entered.wait(3))
                expect(self.page.locator('#projectImportFile')).to_be_disabled()
                expect(self.page.locator('#importProjectsSaveBtn')).to_be_disabled()
                self.page.locator('#importProjectsSaveBtn').evaluate('e => {e.click(); e.click();}')
                self.page.keyboard.press('Escape')
                expect(self.page.locator('#importProjectsDlg')).to_be_visible()
                self.assertEqual(run.call_count, 1)
            finally:
                release.set()
            expect(self.page.locator('#importProjectsDlg')).not_to_be_visible()
        self.assertEqual(len(store.list_profiles()), 3)
        self.assertEqual(self.js_errors, [])

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
        super().setUp()
        self._legacy = os.environ.get("NETMERA_GA4_LEGACY")
        os.environ["NETMERA_GA4_LEGACY"] = str(self.tmp / "eski-yok")
        store.ensure_defaults()
        p = store.load_profile("demo-sahte-veri")
        p["jobs"] = p["jobs"][:1] + [{"type": "ga4", "label": "Sentetik GA4", "ga4": {"property_ref": "PROP_PROD", "metrikler": ["activeUsers"],
                                                                                     "boyutlar": ["deviceCategory"], "tarih": {"tur": "dun"}}}]
        store.save_profile(p)
        self.app = server.App(server._free_port(0))
        self.app.httpd = ThreadingHTTPServer(("127.0.0.1", self.app.port), self.app.make_handler())
        self.thread = threading.Thread(target=self.app.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.app.port}"
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 1000}, color_scheme="light")
        self.context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.base + "/") else route.abort())
        self.page = self.context.new_page()
        self.page.set_default_timeout(20000 if os.environ.get("NETMERA_TEST_LOW_MEMORY") == "1" else 8000)
        self.js_errors = []
        self.page.on("pageerror", lambda err: self.js_errors.append(str(err)))
        self.page.goto(f"{self.base}/?t={self.app.token}")
        self.page.locator('[data-open-project="demo-sahte-veri"]').click()
        self.page.wait_for_function("location.hash === '#/isler'")

    def tearDown(self):
        self.context.close()
        self.app.httpd.shutdown()
        self.app.httpd.server_close()
        self.thread.join(3)
        if self._legacy is None:
            os.environ.pop("NETMERA_GA4_LEGACY", None)
        else:
            os.environ["NETMERA_GA4_LEGACY"] = self._legacy
        super().tearDown()

    def test_sidebar_routes_and_project_selection_stay_in_sync(self):
        from playwright.sync_api import expect
        expect(self.page.locator('.nav-oge[aria-current=page]')).to_have_text("İşler")
        expect(self.page.locator("#jobList .job .t")).to_have_text(["Total User", "Sentetik GA4"])
        self.page.locator('.nav-oge[href="#/ga4/isler"]').click()
        expect(self.page.locator("#ga-sayfa h1")).to_have_text("GA4 işleri")
        expect(self.page.locator("#nm")).to_be_hidden()
        self.page.locator("#proje-secim").select_option("production")
        self.page.wait_for_function("window.S && S.profile.id === 'production'")
        self.page.locator('.nav-oge[href="#/isler"]').click()
        expect(self.page.locator("#projTitle")).to_have_text("Production")
        self.page.locator('.nav-oge[href="#/projeler"]').click()
        expect(self.page.locator("#projectCards")).to_be_visible()
        self.assertEqual(self.js_errors, [])

    def test_new_ga4_job_from_netmera_jobs_opens_drawer_and_saves_into_project(self):
        from playwright.sync_api import expect
        self.page.locator("#addJobBtn").click()
        self.page.locator("#typeBtns [data-type=ga4]").click()
        expect(self.page.locator("#jobDlg")).not_to_be_visible()
        self.page.locator("#is-ad").fill("Yeni sentetik GA4")
        giris = self.page.locator(".cekmece .cs-giris").first
        giris.click()
        giris.fill("sessions")
        self.page.locator(".cs-secenek").first.click()
        self.page.keyboard.press("Escape") if self.page.locator(".cs-liste:visible").count() else None
        self.page.locator(".cekmece .btn-ana", has_text="Kaydet").click()
        expect(self.page.locator("#jobList .job .t")).to_have_text(["Total User", "Sentetik GA4", "Yeni sentetik GA4"])
        job = store.load_profile("demo-sahte-veri")["jobs"][-1]
        self.assertEqual((job["type"], job["ga4"]["metrikler"]), ("ga4", ["sessions"]))
        self.assertEqual(self.js_errors, [])

    def test_ready_report_group_is_pulled_into_project(self):
        from playwright.sync_api import expect
        before = len(store.load_profile("demo-sahte-veri")["jobs"])
        self.page.locator("#ga4LibraryBtn").click()
        expect(self.page.locator(".cekmece")).to_be_visible()
        group = self.page.locator('.cekmece input[type=checkbox][aria-label$="grubunun tamamı"]:not([disabled])').first
        group.check()
        self.page.locator(".cekmece .btn-ana", has_text="Çek").click()
        self.page.wait_for_function(f"window.S && S.profile.jobs.length > {before}")
        jobs = store.load_profile("demo-sahte-veri")["jobs"]
        self.assertTrue(all(j["type"] == "ga4" and j["ga4"].get("paket") for j in jobs[before:]))
        self.assertEqual(self.js_errors, [])

    def test_netmera_run_ga4_tables_open_in_ga4_page_with_export_preview(self):
        from playwright.sync_api import expect
        self.page.locator("[data-tab=run]").click()
        self.page.locator("#testBtn").click()
        expect(self.page.locator("#ga4RunCard")).to_be_visible(timeout=60000)
        self.page.locator("#ga4RunCard a").click()
        expect(self.page.locator(".sonuc-kart h3")).to_have_text(["Sentetik GA4"])
        self.page.locator(".btn", has_text="Dışa aktar").first.click()
        expect(self.page.locator("#modal-baslik")).to_have_text("Dışa aktar")
        expect(self.page.get_by_text("Önizleme", exact=True)).to_be_visible()
        self.assertEqual(self.js_errors, [])

    def test_ga4_copy_overview_and_import_note(self):
        from playwright.sync_api import expect
        self.page.locator('[data-dup="1"]').click()
        expect(self.page.locator(".cekmece")).to_be_visible()
        self.page.keyboard.press("Escape")
        self.assertEqual(len(store.load_profile("demo-sahte-veri")["jobs"]), 2)  # kopya çekmecede; kaydetmeden eklenmez
        self.page.locator('.nav-oge[href="#/ga4/genel"]').click()
        expect(self.page.locator("#ga-sayfa h1")).to_have_text("Genel bakış")
        from netmera_automation.ga4.paths import data_root
        import json
        data_root().mkdir(parents=True, exist_ok=True)
        (data_root() / "ice-aktarim.json").write_text(json.dumps({"zaman": "2026-10-28T10:00:00+03:00", "projeler": [{"ad": "Sentetik eski", "is": 3}]}), encoding="utf-8")
        self.page.reload()
        self.page.evaluate("location.hash = '#/projeler'")  # yenileme son açık sayfada kalır
        self.page.wait_for_selector(".pcard")
        expect(self.page.locator("#ga4ImportNote")).to_contain_text("Sentetik eski (3 GA4 işi)")
        self.page.locator("[data-import-ok]").click()
        expect(self.page.locator("#ga4ImportNote")).to_be_hidden()
        self.page.reload()
        self.page.evaluate("location.hash = '#/projeler'")  # yenileme son açık sayfada kalır
        self.page.wait_for_selector(".pcard")
        expect(self.page.locator("#ga4ImportNote")).to_be_hidden()  # bir kez gösterilir
        self.assertEqual(self.js_errors, [])

    def test_single_google_connection_updates_sheets_status_and_logout(self):
        import json
        from unittest.mock import patch
        from playwright.sync_api import expect
        from netmera_automation import google_auth, google_shared
        from netmera_automation.ga4 import google_hesap
        from tests.test_google_sheets import CLIENT

        self.page.locator('[data-tab=settings]').click()
        self.page.locator('#setSource').select_option('netmera')
        self.page.locator('#setApp').fill('Sentetik App')
        self.page.locator('#setOutputTarget').select_option('google_sheets')
        self.page.locator('#setSheets').fill('synthetic_sheet_12345')
        self.page.locator('#saveSettingsBtn').click()
        expect(self.page.locator('#setSheets')).to_have_value('synthetic_sheet_12345')
        expect(self.page.locator('#googleClientFile, #connectGoogleBtn')).to_have_count(0)
        self.page.locator('#manageGoogleLink').click()
        expect(self.page.locator('.nav-oge[href="#/ga4/baglanti"]')).to_have_count(1)
        self.page.locator('#ga-sayfa input[type=file]').set_input_files(
            {'name': 'sentetik.json', 'mimeType': 'application/json', 'buffer': json.dumps(CLIENT).encode()})
        self.page.get_by_role('button', name='İstemciyi kaydet', exact=True).click()
        expect(self.page.get_by_role('button', name='Google hesabımla bağlan', exact=True)).to_be_enabled()
        self.assertEqual(google_shared.shared_client()['client_id'], CLIENT['installed']['client_id'])

        def fake_login(account, iptal=None):
            account._kaydet({'client_id': CLIENT['installed']['client_id'], 'access_token': 'sentetik-access',
                             'refresh_token': 'sentetik-refresh', 'expires_at': 9999999999,
                             'eposta': 'sentetik@example.invalid', 'kapsam': list(google_hesap.KAPSAMLAR)})
            return 'sentetik@example.invalid'

        with patch.object(google_hesap.GoogleHesabi, 'baglan', fake_login):
            self.page.get_by_role('button', name='Google hesabımla bağlan', exact=True).click()
            expect(self.page.get_by_role('button', name='Çıkış yap', exact=True)).to_be_visible()
        self.assertTrue(google_auth.status('demo-sahte-veri')['shared'])
        self.page.locator('[data-tab=settings]').click()
        expect(self.page.locator('#googlePill')).to_have_text('Bağlı')
        meta = {'spreadsheetId': 'synthetic_sheet_12345', 'properties': {'title': 'Sentetik rapor'},
                'sheets': [{'properties': {'title': 'Rapor'}}]}
        with patch.object(google_auth, 'request_json', return_value=meta) as request:
            self.page.locator('#checkGoogleBtn').click()
            expect(self.page.locator('#googlePill')).to_have_text('Hazır')
            self.assertTrue(all(call.kwargs.get('method', 'GET') == 'GET' for call in request.call_args_list))
        self.page.locator('#manageGoogleLink').click()
        with patch.object(google_hesap.GoogleHesabi, 'cikis', lambda account: account.oturum_yolu.unlink(missing_ok=True)):
            self.page.get_by_role('button', name='Çıkış yap', exact=True).click()
            self.page.get_by_role('alertdialog').get_by_role('button', name='Çıkış yap', exact=True).click()
            expect(self.page.get_by_role('button', name='Google hesabımla bağlan', exact=True)).to_be_visible()
        self.page.locator('[data-tab=settings]').click()
        expect(self.page.locator('#googlePill')).to_have_text('Hesap bağlanmadı')
        self.assertFalse(google_auth.status('demo-sahte-veri')['connected'])
        self.assertEqual(self.js_errors, [])

    def test_segment_attribute_source_fields_save_reopen_and_switch_source(self):
        from copy import deepcopy
        from pathlib import Path
        from playwright.sync_api import expect
        from tests.test_segment_run import REBUILD
        p=store.load_profile('demo-sahte-veri'); job=deepcopy(REBUILD);job['label']='Sentetik kaynak filtre'
        p['jobs'].append(job);store.save_profile(p)
        self.page.reload()
        self.page.locator('[data-open-project="demo-sahte-veri"]').click()
        item=self.page.locator('#jobList .job').filter(has_text='Sentetik kaynak filtre')
        item.locator('[data-edit]').click()
        attribute=self.page.locator('[data-attr="0"]')
        attribute.locator('[data-as=source]').select_option('excel')
        attribute.locator('[data-as=file]').fill(str(self.tmp/'sentetik-kaynak.xlsx'))
        attribute.locator('[data-as=sheet]').fill('Kaynak')
        attribute.locator('[data-as=date_column]').fill('A')
        attribute.locator('[data-as=value_column]').fill('D')
        attribute.locator('[data-as=first_row]').fill('3')
        attribute.locator('[data-as=last_row]').fill('100')
        self.page.locator('#jSplitDays').check()
        self.page.locator('#jPoll').fill('20')
        self.page.locator('#jobSaveBtn').click()
        expect(self.page.locator('#jobDlg')).not_to_be_visible()
        saved=store.load_profile('demo-sahte-veri')['jobs'][-1]
        self.assertTrue(saved['split_days']);self.assertEqual(saved['items'][0]['attributes'][0]['value_source']['value_column'],'D')
        self.assertEqual(saved['poll_seconds'],20)
        item.locator('[data-edit]').click()
        expect(self.page.locator('#jPoll')).to_have_value('20')
        expect(attribute.locator('[data-as=first_row]')).to_have_value('3')
        expect(attribute.locator('[data-a=values]')).to_be_disabled()
        attribute.locator('[data-as=source]').select_option('google_sheets')
        attribute.locator('[data-as=sheets_id]').fill('synthetic_sheet_12345')
        for width in (390,1280):
            self.page.set_viewport_size({'width':width,'height':1000})
            self.assertTrue(self.page.locator('#jobDlg').evaluate('e=>e.scrollWidth<=e.clientWidth+1'))
        screenshot_dir=os.environ.get('NETMERA_UI_SCREENSHOT_DIR')
        if screenshot_dir:
            self.page.locator('#jobDlg').screenshot(path=str(Path(screenshot_dir)/'attribute-source.png'))
        self.page.locator('#jobSaveBtn').click()
        expect(self.page.locator('#jobDlg')).not_to_be_visible()
        saved=store.load_profile('demo-sahte-veri')['jobs'][-1]['items'][0]['attributes'][0]
        self.assertEqual(saved['value_source']['source'],'google_sheets')
        self.assertNotIn('file',saved['value_source'])
        self.assertEqual(saved['values'],[])
        self.assertEqual(self.js_errors,[])

    def test_selected_jobs_enable_disable_delete_and_cancel(self):
        from playwright.sync_api import expect
        p=store.load_profile('demo-sahte-veri');first,second=p['jobs']
        for button in ('enableSelectedJobsBtn','disableSelectedJobsBtn','deleteSelectedJobsBtn'):
            expect(self.page.locator('#'+button)).to_be_disabled()
        self.page.locator('[data-selectjob="'+first['id']+'"]').check()
        expect(self.page.locator('#selectAllJobs')).not_to_be_checked()
        self.page.locator('#disableSelectedJobsBtn').click()
        expect(self.page.locator('[data-toggle="0"]')).not_to_be_checked()
        self.assertEqual(store.load_profile(p['id'])['jobs'][1],second)
        expect(self.page.locator('#jobSelectionCount')).to_have_text('1 iş seçili')
        self.page.locator('#enableSelectedJobsBtn').click()
        expect(self.page.locator('[data-toggle="0"]')).to_be_checked()
        self.assertEqual(store.load_profile(p['id'])['jobs'],p['jobs'])
        self.page.once('dialog',lambda dialog:dialog.dismiss())
        self.page.locator('#deleteSelectedJobsBtn').click()
        expect(self.page.locator('#jobList .job')).to_have_count(2)
        self.page.once('dialog',lambda dialog:dialog.accept())
        self.page.locator('#deleteSelectedJobsBtn').click()
        expect(self.page.locator('#jobList .job')).to_have_count(1)
        expect(self.page.locator('#jobSelectionCount')).to_have_text('0 iş seçili')
        expect(self.page.locator('#deleteSelectedJobsBtn')).to_be_disabled()
        self.assertEqual(store.load_profile(p['id'])['jobs'],[second])
        self.page.locator('#selectAllJobs').check()
        self.page.locator('#disableSelectedJobsBtn').click()
        expect(self.page.locator('[data-toggle="0"]')).not_to_be_checked()
        self.page.locator('.nav-oge[href="#/ga4/isler"]').click()
        expect(self.page.locator('#ga-sayfa .kapali-satir')).to_have_count(1)
        self.page.locator('.nav-oge[href="#/isler"]').click()
        self.page.locator('#enableSelectedJobsBtn').click()
        expect(self.page.locator('[data-toggle="0"]')).to_be_checked()
        self.page.locator('.nav-oge[href="#/ga4/isler"]').click()
        expect(self.page.locator('#ga-sayfa .kapali-satir')).to_have_count(0)
        self.assertEqual(self.js_errors,[])

    def test_job_selection_copy_move_and_delete_all_with_cancel(self):
        from pathlib import Path
        from playwright.sync_api import expect
        target=store.create_profile('Sentetik hedef',source='demo');target['jobs']=[];target=store.save_profile(target)
        self.page.locator('#selectAllJobs').check()
        expect(self.page.locator('#jobSelectionCount')).to_have_text('2 iş seçili')
        self.page.locator('#transferJobsBtn').click()
        expect(self.page.locator('#transferJobsDlg')).to_be_visible()
        self.page.locator('#transferJobsTarget').select_option(target['id'])
        for width in (390,1280):
            self.page.set_viewport_size({'width':width,'height':1000})
            self.assertTrue(self.page.locator('#transferJobsDlg').evaluate('e=>e.scrollWidth<=e.clientWidth+1'))
        screenshot_dir=os.environ.get('NETMERA_UI_SCREENSHOT_DIR')
        if screenshot_dir:self.page.locator('#transferJobsDlg').screenshot(path=str(Path(screenshot_dir)/'job-transfer.png'))
        self.page.locator('#transferJobsSaveBtn').click()
        expect(self.page.locator('#transferJobsDlg')).not_to_be_visible()
        self.assertEqual(len(store.load_profile('demo-sahte-veri')['jobs']),2)
        self.assertEqual(len(store.load_profile(target['id'])['jobs']),2)
        self.assertTrue(all(not j['enabled'] for j in store.load_profile(target['id'])['jobs']))
        self.page.locator('#jobList [data-transfer="0"]').click()
        self.page.locator('#transferJobsTarget').select_option(target['id'])
        self.page.locator('#transferJobsMode').select_option('move')
        expect(self.page.locator('#transferJobsSaveBtn')).to_have_text('Taşı')
        self.page.locator('#transferJobsSaveBtn').click()
        expect(self.page.locator('#transferJobsDlg')).not_to_be_visible()
        expect(self.page.locator('#jobList .job')).to_have_count(1)
        self.assertEqual(len(store.load_profile(target['id'])['jobs']),3)
        self.page.once('dialog',lambda dialog:dialog.dismiss())
        self.page.locator('#deleteAllJobsBtn').click()
        expect(self.page.locator('#jobList .job')).to_have_count(1)
        self.page.once('dialog',lambda dialog:dialog.accept())
        self.page.locator('#deleteAllJobsBtn').click()
        expect(self.page.locator('#jobList .job')).to_have_count(0)
        expect(self.page.locator('#deleteAllJobsBtn')).to_be_disabled()
        self.assertEqual(store.load_profile('demo-sahte-veri')['jobs'],[])
        self.assertEqual(len(store.load_profile(target['id'])['jobs']),3)
        self.page.locator('.nav-oge[href="#/ga4/isler"]').click()
        expect(self.page.locator('#ga-sayfa')).not_to_contain_text('Sentetik GA4')
        self.assertEqual(self.js_errors,[])

    def test_saved_segment_date_is_used_unless_run_screen_overrides_it(self):
        import json
        from datetime import date
        from playwright.sync_api import expect
        from netmera_automation import jobs as J
        p=store.load_profile('demo-sahte-veri')
        p['jobs'].append({'type':'segment_run','label':'Sentetik tarih kontrolu','name':'Sentetik segment'})
        store.save_profile(p)
        self.page.reload();self.page.locator('[data-open-project="demo-sahte-veri"]').click()
        row=self.page.locator('#jobList .job').filter(has_text='Sentetik tarih kontrolu')
        row.locator('[data-edit]').click()
        self.page.locator('#jPeriod').select_option('custom')
        self.page.locator('#jStart').fill('2026-10-24');self.page.locator('#jEnd').fill('2026-10-24')
        self.page.locator('#jobSaveBtn').click()
        expect(self.page.locator('#jobDlg')).not_to_be_visible()
        saved=store.load_profile('demo-sahte-veri')['jobs'][-1]
        self.assertEqual(J.job_days(saved,server.runner.now()),(date(2026,10,24),date(2026,10,24)))
        row.locator('[data-edit]').click()
        expect(self.page.locator('#jStart')).to_have_value('2026-10-24')
        self.page.locator('#jobDlg [data-close]').first.click()
        requests=[]
        def run(route):
            requests.append(route.request.post_data_json)
            route.fulfill(status=200,content_type='application/json',body=json.dumps({'started':True}))
        self.page.route('**/api/run',run)
        row.locator('[data-try]').click()
        self.page.wait_for_function("location.hash === '#/calistir'")
        self.assertEqual(len(requests),1)
        self.assertNotIn('date_start',requests[-1])
        self.assertEqual(requests[-1]['job_ids'],[saved['id']])
        self.page.locator('#runStart').fill('2026-10-28');self.page.locator('#runEnd').fill('2026-10-28')
        self.page.locator('[data-tab=jobs]').click();row.locator('[data-try]').click()
        self.page.wait_for_function("location.hash === '#/calistir'")
        self.assertEqual((requests[-1]['date_start'],requests[-1]['date_end']),('2026-10-28','2026-10-28'))
        self.page.locator('#runDateClear').click()
        self.page.locator('[data-tab=jobs]').click();row.locator('[data-try]').click()
        self.page.wait_for_function("location.hash === '#/calistir'")
        self.assertNotIn('date_start',requests[-1])
        self.assertEqual(self.js_errors,[])

    def test_palette_theme_and_narrow_layout(self):
        from playwright.sync_api import expect
        self.page.keyboard.press("Control+K")
        expect(self.page.locator(".palet-giris")).to_be_focused()
        self.page.locator(".palet-giris").fill("zamanlama")
        self.page.keyboard.press("Enter")
        self.page.wait_for_function("location.hash === '#/zamanlama'")
        self.page.locator("#tema-dugme").click()
        self.assertIn(self.page.evaluate("document.documentElement.dataset.tema"), ("koyu", "acik"))
        for route in ("#/isler", "#/ayarlar", "#/google", "#/ga4/isler", "#/ga4/sheets"):
            self.page.evaluate(f"location.hash = '{route}'")
            self.page.set_viewport_size({"width": 390, "height": 900})
            self.page.wait_for_timeout(300)
            self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), route)
            self.page.set_viewport_size({"width": 1280, "height": 1000})
        self.assertEqual(self.js_errors, [])


if __name__ == "__main__":
    unittest.main()
