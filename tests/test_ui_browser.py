"""Yerel arayüz + gerçek yerel API, yalnız geçici/sentetik profil verisiyle.

Netmera veya başka bir harici adrese yapılan tüm istekler engellenir.
"""
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import threading
import unittest
from unittest.mock import patch

from netmera_automation import server, store
from tests.helpers import TempDataCase, browser_test_args
from tests.test_panel_browser import sync_playwright


@unittest.skipIf(sync_playwright is None, "playwright yok")
class UiBrowserTests(TempDataCase):
    def test_daily_preview_shows_each_date_and_cell_without_writing(self):
        from datetime import date
        from openpyxl import Workbook
        from playwright.sync_api import expect
        from tests.test_cell_mapping import mapped_profile, result
        p=mapped_profile(self.tmp,mode='header_lookup',header_row=1,value_row=2)
        path=Path(p['output_file']);path.parent.mkdir(parents=True,exist_ok=True)
        book=Workbook();book.active.title='Rapor'
        book.active['A1']=date(2026,10,16);book.active['B1']=date(2026,10,17)
        book.save(path);book.close();before=path.read_bytes()
        daily=[result(p,catch_up=True),result(p,11,catch_up=True,
               period_start='2026-10-17T00:00:00+03:00',period_end='2026-10-18T00:00:00+03:00')]
        store.write_json(store.profile_dir(p['id'])/'son-calisma.json',{'rows':[],'day_rows':daily,'write':False})
        self.page.reload()
        self.page.locator('[data-open-project="demo-sahte-veri"]').click()
        self.page.wait_for_function("location.hash === '#/isler'")
        self.page.locator('[data-tab=settings]').click()
        self.page.locator('#setExcelMode').select_option('mapped_cells')
        self.page.locator('#previewMappingBtn').click()
        preview=self.page.locator('#mappingPreview')
        for text in ('Hazır: 2','16.10.2026','17.10.2026','Rapor!A2','Rapor!B2'):
            expect(preview).to_contain_text(text)
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(self.js_errors,[])
        self.page.locator('[data-tab=run]').click()
        self.page.locator('#rebuildBtn').click()
        expect(self.page.locator('#writeRetryInfo')).to_contain_text('Yeni çıktı yazılmadı')
        expect(self.page.locator('#writeRetryInfo')).to_contain_text('Son TEST')
        self.assertEqual(path.read_bytes(),before)

    def test_output_mapping_choices_save_preview_help_and_google_config(self):
        from playwright.sync_api import expect
        from tests.test_cell_mapping import result
        self.page.locator('#jobDlg .dlg-h [data-close]').click()
        self.page.locator('[data-tab=settings]').click()
        self.page.locator('#setExcelMode').select_option('mapped_cells')
        self.page.locator('#addMappingBtn').click()
        row=self.page.locator('.mapping-row')
        row.locator('[data-m=sheet]').fill('Rapor')
        row.locator('[data-m=mode]').select_option('daily_shift')
        row.locator('[data-m=base_cell]').fill('B4')
        row.locator('[data-m=base_date]').fill('2026-10-16')
        for direction in ('down','right','both'):
            row.locator('[data-m=direction]').select_option(direction)
            self.page.locator('#saveSettingsBtn').click()
            self.page.wait_for_function('d=>{const m=S.profile.excel_mappings?.[0];return document.querySelector("#settingsErr").textContent||(m&&m.row_step===(d==="right"?0:1)&&m.column_step===(d==="down"?0:1))}',arg=direction)
            expect(self.page.locator('#settingsErr')).to_be_empty()
            expect(row.locator('[data-m=direction]')).to_have_value(direction)
            saved=store.load_profile('demo-sahte-veri')['excel_mappings'][0]
            self.assertEqual((saved['row_step'],saved['column_step']),{'down':(1,0),'right':(0,1),'both':(1,1)}[direction])
        self.page.locator('[data-info=mapping]').click()
        expect(self.page.locator('#infoBody')).to_contain_text('Aynı günü tekrar')
        self.page.keyboard.press('Escape')
        for width in (1280,390):
            self.page.set_viewport_size({'width':width,'height':1000})
            self.assertTrue(row.evaluate('e=>e.scrollWidth<=e.clientWidth+1'))
        self.page.set_viewport_size({'width':1280,'height':1000})
        self.page.locator('#setOutputTarget').select_option('google_sheets')
        expect(self.page.locator('#googleCard')).to_be_visible()
        expect(self.page.locator('#setOutputWrap')).not_to_be_visible()
        expect(self.page.locator('#connectGoogleBtn, #googleClientFile')).to_have_count(0)
        self.page.locator('#setSource').select_option('netmera')
        self.page.locator('#setApp').fill('Sentetik App')
        self.page.locator('#setSheets').fill('synthetic_sheet_12345')
        self.page.locator('#saveSettingsBtn').click()
        self.page.wait_for_function("S.profile.source === 'netmera' && S.profile.sheets_id === 'synthetic_sheet_12345'")
        expect(self.page.locator('#setSheets')).to_have_value('synthetic_sheet_12345')
        expect(self.page.locator('#checkGoogleBtn')).to_be_disabled()
        from netmera_automation import google_auth
        from netmera_automation.ga4 import google_hesap
        from tests.test_google_sheets import CLIENT
        meta={'spreadsheetId':'synthetic_sheet_12345','properties':{'title':'Sentetik rapor'},'sheets':[{'properties':{'sheetId':17,'title':'Rapor'}}]}
        # Kaydetme sonrası otomatik kontrol dahil tüm Sheets istekleri sentetiktir.
        google_requests = patch.object(google_auth, 'request_json', return_value=meta)
        request = google_requests.start(); self.addCleanup(google_requests.stop)
        google_auth.save_client('demo-sahte-veri', CLIENT)
        google_hesap.GoogleHesabi()._kaydet({'client_id': CLIENT['installed']['client_id'],
            'refresh_token': 'synthetic-refresh', 'access_token': 'synthetic-access', 'expires_at': 9999999999,
            'kapsam': list(google_hesap.KAPSAMLAR)})
        self.page.locator('[data-tab=run]').click()
        self.page.locator('[data-tab=settings]').click()
        expect(self.page.locator('#checkGoogleBtn')).to_be_enabled()
        self.page.locator('#checkGoogleBtn').click()
        expect(self.page.locator('#googlePill')).to_have_text('Hazır')
        expect(self.page.locator('#googleStatus')).to_contain_text('Sentetik rapor')
        self.assertTrue(all(c.kwargs.get('method','GET')=='GET' for c in request.call_args_list))
        self.page.locator('#openGoogleGuideBtn').click()
        expect(self.page.locator('[data-gstate=client]')).to_have_class(re.compile(r'\bdone\b'))
        expect(self.page.locator('[data-gstate=check]')).to_have_class(re.compile(r'\bdone\b'))
        for width in (1280,390):
            self.page.set_viewport_size({'width':width,'height':1000})
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
            if os.environ.get('NETMERA_UI_SCREENSHOT_DIR'):
                self.page.screenshot(path=str(Path(os.environ['NETMERA_UI_SCREENSHOT_DIR'])/f'google-guide-{width}.png'),full_page=True)
        self.page.set_viewport_size({'width':1280,'height':1000})
        self.page.locator('#goGoogleSettingsBtn').click()
        self.page.locator('[data-tab=settings]').click()
        for width in (1280,390):
            self.page.set_viewport_size({'width':width,'height':1000})
            self.assertTrue(self.page.locator('#googleCard').evaluate('e=>e.scrollWidth<=e.clientWidth+1'))
        self.page.set_viewport_size({'width':1280,'height':1000})
        self.page.locator('[data-info=google]').click()
        expect(self.page.locator('#infoBody')).to_contain_text('OAuth')
        self.page.keyboard.press('Escape')
        screenshot_dir=os.environ.get('NETMERA_UI_SCREENSHOT_DIR')
        if screenshot_dir:
            folder=Path(screenshot_dir);folder.mkdir(parents=True,exist_ok=True)
            self.page.locator('#cellMappingCard').screenshot(path=str(folder/'cell-mapping.png'))
            self.page.locator('#googleCard').screenshot(path=str(folder/'google-config.png'))
        self.page.reload()
        self.page.locator('[data-open-project="demo-sahte-veri"]').click()
        self.page.locator('[data-tab=settings]').click()
        expect(row.locator('[data-m=direction]')).to_have_value('both')
        expect(self.page.locator('#setOutputTarget')).to_have_value('google_sheets')
        self.assertEqual(self.js_errors,[])

    def test_header_lookup_mapping_fields_save_with_previous_day(self):
        from playwright.sync_api import expect
        self.page.locator('#jobDlg .dlg-h [data-close]').click()
        self.page.locator('[data-tab=settings]').click()
        self.page.locator('#setExcelMode').select_option('mapped_cells')
        self.page.locator('#addMappingBtn').click()
        row=self.page.locator('.mapping-row')
        row.locator('[data-m=mode]').select_option('header_lookup')
        for hidden in ('cell','date_column','base_cell'):
            expect(row.locator(f'[data-wrap={hidden}]')).to_be_hidden()
        row.locator('[data-m=header_row]').fill('80')
        row.locator('[data-m=value_row]').fill('82')
        row.locator('[data-m=month_cell]').fill('a82')
        row.locator('[data-m=date_basis]').select_option('previous_day')
        self.page.locator('#saveSettingsBtn').click()
        expect(self.page.locator('#toast')).to_contain_text('Ayarlar kaydedildi')
        saved=store.load_profile('demo-sahte-veri')['excel_mappings'][0]
        self.assertEqual({k:saved[k] for k in ('mode','header_row','value_row','month_cell','date_basis')},
                         {'mode':'header_lookup','header_row':80,'value_row':82,'month_cell':'A82','date_basis':'previous_day'})
        expect(row.locator('[data-m=month_cell]')).to_have_value('A82')
        for width in (1280,390):
            self.page.set_viewport_size({'width':width,'height':1000})
            self.assertTrue(row.evaluate('e=>e.scrollWidth<=e.clientWidth+1'))
        self.assertEqual(self.js_errors,[])

    def test_catch_up_days_and_notification_settings_save_and_test_button(self):
        from playwright.sync_api import expect
        self.page.locator('#jobDlg .dlg-h [data-close]').click()
        self.page.locator('[data-tab=settings]').click()
        expect(self.page.locator('#setCatchUp')).to_have_value('7')
        expect(self.page.locator('#setNotify')).not_to_be_checked()
        self.page.locator('#setCatchUp').fill('3')
        self.page.locator('#setNotify').check()
        self.page.locator('#saveSettingsBtn').click()
        expect(self.page.locator('#toast')).to_contain_text('Ayarlar kaydedildi')
        saved=store.load_profile('demo-sahte-veri')
        self.assertEqual((saved['catch_up_days'],saved['notify']),(3,True))
        with patch('netmera_automation.notify.show',return_value=False) as shown:
            self.page.locator('#notifyTestBtn').click()
            expect(self.page.locator('#toast')).to_contain_text('gösterilemedi')
            self.assertEqual(shown.call_count,1)
        self.page.locator('#setCatchUp').fill('20')
        self.page.locator('#saveSettingsBtn').click()
        expect(self.page.locator('#settingsErr')).to_contain_text('0–14')
        self.assertEqual(self.js_errors,[])

    def test_help_tab_guide_and_faq_open_and_fit(self):
        from playwright.sync_api import expect
        self.page.locator('#jobDlg .dlg-h [data-close]').click()
        self.page.locator('[data-tab=help]').click()
        panel=self.page.locator('section[data-panel=help]')
        expect(panel).to_contain_text('Kullanım kılavuzu')
        faq=panel.locator('details').filter(has_text='Hangi güne göre')
        expect(faq.locator('p').first).to_be_hidden()
        faq.locator('summary').click()
        expect(faq).to_contain_text('Verinin günü')
        self.assertGreaterEqual(panel.locator('details').count(),20)
        expect(self.page.locator('#errorTable tr').first).to_be_visible()
        for width in (1280,390):
            self.page.set_viewport_size({'width':width,'height':1000})
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
            if os.environ.get('NETMERA_UI_SCREENSHOT_DIR'):
                self.page.screenshot(path=str(Path(os.environ['NETMERA_UI_SCREENSHOT_DIR'])/f'help-{width}.png'),full_page=True)
        self.assertEqual(self.js_errors,[])

    def test_last_results_include_split_day_rows(self):
        from playwright.sync_api import expect
        from tests.test_cell_mapping import result
        self.page.locator('#jobDlg .dlg-h [data-close]').click()
        p=store.load_profile('demo-sahte-veri')
        rows=[result(p,value=100+d,period_start=f'2026-10-{d}T00:00:00+03:00',period_end=f'2026-10-{d+1}T00:00:00+03:00') for d in (14,15,16)]
        store.write_json(store.profile_dir(p['id'])/'son-calisma.json',{'planned_at':'2026-10-19T09:00:00+03:00','write':False,'rows':[],'day_rows':rows,
            'ok':0,'failed':0,'split':True,'catch_up':{'days':['2026-10-14','2026-10-15','2026-10-16'],'ok':3,'failed':0}})
        self.page.reload();self.page.locator('[data-open-project="demo-sahte-veri"]').click();self.page.locator('[data-tab=run]').click()
        expect(self.page.locator('#resBody tr')).to_have_count(3)
        expect(self.page.locator('#resBody')).to_contain_text('14.10.2026')
        expect(self.page.locator('#resBody')).to_contain_text('116')
        expect(self.page.locator('#lastRunInfo')).to_contain_text('günlere bölünmüş')
        expect(self.page.locator('#lastRunInfo')).to_contain_text('3 başarılı, 0 hatalı')
        self.assertEqual(self.js_errors,[])

    def test_team_client_skips_json_step_and_only_asks_to_connect(self):
        from playwright.sync_api import expect
        from netmera_automation import google_auth
        from tests.test_google_sheets import CLIENT
        google_auth.TEAM_FILE.parent.mkdir(parents=True,exist_ok=True)
        google_auth.TEAM_FILE.write_text(json.dumps(CLIENT),encoding='utf-8')
        self.page.locator('#jobDlg .dlg-h [data-close]').click()
        self.page.locator('[data-tab=settings]').click()
        self.page.locator('#setSource').select_option('netmera')
        self.page.locator('#setApp').fill('Sentetik App')
        self.page.locator('#setOutputTarget').select_option('google_sheets')
        self.page.locator('#setSheets').fill('synthetic_sheet_12345')
        self.page.locator('#saveSettingsBtn').click()
        expect(self.page.locator('#googleStatus')).to_contain_text('Ekip istemcisi')
        expect(self.page.locator('#googleClientFile, #connectGoogleBtn')).to_have_count(0)
        expect(self.page.locator('#manageGoogleLink')).to_be_visible()
        self.page.locator('#manageGoogleLink').click()
        expect(self.page.get_by_role('button', name='Google hesabımla bağlan', exact=True)).to_be_enabled()
        self.assertEqual(self.js_errors,[])

    @unittest.skipUnless(os.name=='nt','Windows DPAPI')
    def test_login_form_saves_protected_config_clears_password_and_can_disable_remove(self):
        from playwright.sync_api import expect
        from netmera_automation import login_config
        self.page.locator('#jobDlg .dlg-h [data-close]').click()
        self.page.locator('[data-tab=settings]').click()
        expect(self.page.locator('#loginConfigCard')).not_to_be_visible()
        self.page.locator('#setSource').select_option('netmera')
        self.page.locator('#setApp').fill('Sentetik App')
        self.page.locator('#saveSettingsBtn').click()
        expect(self.page.locator('#loginConfigCard')).to_be_visible()
        self.page.locator('#loginUsername').fill('synthetic@example.invalid')
        self.page.locator('#loginPassword').fill('Synthetic-ui-only-42!')
        self.page.locator('#loginEnabled').check()
        self.page.locator('#saveLoginConfigBtn').click()
        expect(self.page.locator('#loginConfigStatus')).to_contain_text('hazır')
        expect(self.page.locator('#loginPassword')).to_have_value('')
        expect(self.page.locator('#loginPasswordHint')).to_contain_text('Kayıtlı şifre var')
        path=login_config.config_path('demo-sahte-veri')
        self.assertNotIn('Synthetic-ui-only-42!',path.read_text(encoding='utf-8'))
        self.page.locator('[data-info=login]').click()
        expect(self.page.locator('#infoBody')).to_contain_text('Windows DPAPI')
        self.page.keyboard.press('Escape')
        for width in (1280,390):
            self.page.set_viewport_size({'width':width,'height':1000})
            self.assertTrue(self.page.locator('#loginConfigCard').evaluate('e => e.scrollWidth <= e.clientWidth + 1'))
        self.page.set_viewport_size({'width':1280,'height':1000})
        screenshot_dir=os.environ.get('NETMERA_UI_SCREENSHOT_DIR')
        if screenshot_dir:
            folder=Path(screenshot_dir); folder.mkdir(parents=True,exist_ok=True)
            self.page.locator('#loginConfigCard').screenshot(path=str(folder/'login-config.png'))
        self.page.locator('#loginUsername').fill('different@example.invalid')
        self.page.locator('#saveLoginConfigBtn').click()
        expect(self.page.locator('#loginConfigErr')).to_contain_text('yeni şifreyi de gir')
        self.page.locator('#loginUsername').fill('synthetic@example.invalid')
        self.page.locator('#loginEnabled').uncheck()
        self.page.locator('#saveLoginConfigBtn').click()
        expect(self.page.locator('#loginConfigStatus')).to_contain_text('kapalı')
        self.assertTrue(login_config.status('demo-sahte-veri')['has_credentials'])
        self.page.once('dialog',lambda d:d.accept())
        self.page.locator('#clearLoginConfigBtn').click()
        expect(self.page.locator('#loginUsername')).to_have_value('')
        self.assertFalse(login_config.status('demo-sahte-veri')['has_credentials'])
        self.assertEqual(self.js_errors,[])

    def test_event_session_tag_save_reopen_and_return_to_all_users(self):
        from playwright.sync_api import expect
        self.page.locator('#typeBtns [data-type=event]').click()
        self.page.locator('#jLabel').fill('Sentetik tag oturum')
        self.page.locator('#jEvent').fill('Sentetik Event')
        self.page.locator('#jMetric').select_option('by_session')
        self.page.locator('#jAudience').select_option('tag')
        expect(self.page.locator('#jAudienceNameWrap')).to_be_visible()
        self.page.locator('#jobSaveBtn').click()
        expect(self.page.locator('#jErr')).to_contain_text('hedef Tag adı')
        self.page.locator('#jAudienceName').fill('Sentetik Tag')
        expect(self.page.locator('#jPreview')).to_contain_text('Tag: Sentetik Tag')
        expect(self.page.locator('#jPreview')).to_contain_text('By Session')
        for width in (1280,390):
            self.page.set_viewport_size({'width':width,'height':1000})
            self.assertTrue(self.page.locator('#jobDlg').evaluate('e => e.scrollWidth <= e.clientWidth + 1'))
        self.page.set_viewport_size({'width':1280,'height':1000})
        self.page.locator('[data-info=metric]').click()
        expect(self.page.locator('#infoBody')).to_contain_text('Dimensions')
        self.page.keyboard.press('Escape')
        screenshot_dir=os.environ.get('NETMERA_UI_SCREENSHOT_DIR')
        if screenshot_dir:
            folder=Path(screenshot_dir); folder.mkdir(parents=True,exist_ok=True)
            self.page.screenshot(path=str(folder/'event-session-tag.png'))
        self.page.locator('#jobSaveBtn').click()
        expect(self.page.locator('#jobDlg')).not_to_be_visible()
        item=self.page.locator('.job').filter(has_text='Sentetik tag oturum')
        expect(item).to_contain_text('Tag: Sentetik Tag')
        item.locator('[data-edit]').click()
        expect(self.page.locator('#jMetric')).to_have_value('by_session')
        expect(self.page.locator('#jAudience')).to_have_value('tag')
        expect(self.page.locator('#jAudienceName')).to_have_value('Sentetik Tag')
        self.page.locator('#jAudience').select_option('all_users')
        expect(self.page.locator('#jAudienceNameWrap')).not_to_be_visible()
        self.page.locator('#jobSaveBtn').click()
        expect(self.page.locator('#jobDlg')).not_to_be_visible()
        profile=store.load_profile('demo-sahte-veri')
        self.assertEqual(profile['jobs'][-1]['audience'],'all_users')
        self.assertNotIn('audience_name',profile['jobs'][-1])
        self.assertEqual(self.js_errors,[])

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
        super().setUp()
        # Gerçek İndirilenler klasörü okunmaz.
        import tempfile
        self._downloads=tempfile.TemporaryDirectory();self.downloads=Path(self._downloads.name)
        self._downloads_patch=patch('netmera_automation.google_auth.downloads_dir',return_value=self.downloads);self._downloads_patch.start()
        self.addCleanup(self._downloads.cleanup);self.addCleanup(self._downloads_patch.stop)
        store.ensure_defaults()
        self.app = server.App(server._free_port(0))
        self.app.httpd = ThreadingHTTPServer(("127.0.0.1", self.app.port), self.app.make_handler())
        self.thread = threading.Thread(target=self.app.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.app.port}"
        self.context = self.browser.new_context(viewport={"width":1280, "height":1000}, color_scheme="light")
        self.context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.base + "/") else route.abort())
        self.page = self.context.new_page()
        self.page.set_default_timeout(20000 if os.environ.get('NETMERA_TEST_LOW_MEMORY')=='1' else 6000)
        self.js_errors = []
        self.page.on('pageerror', lambda err: self.js_errors.append(str(err)))
        self.page.goto(f"{self.base}/?t={self.app.token}")
        self.page.locator('[data-open-project="demo-sahte-veri"]').click()
        self.page.locator('#addJobBtn').click()
        self.page.locator('#typeBtns [data-type=people]').click()
        self.page.locator('#jLabel').fill('Sentetik tarih işi')
        self.page.locator('#addPRule').click()
        self.row = self.page.locator('[data-p="0"]')
        self.row.locator('[data-k=field]').fill('Behavior')
        self.row.locator('[data-k=field]').press('Tab')
        self.row.locator('[data-k=value]').fill('Sentetik Davranış')
        self.row.locator('[data-family="1"]').select_option('exactly')
        self.row.locator('[data-family="2"]').select_option('between dates')

    def tearDown(self):
        self.context.close()
        self.app.httpd.shutdown()
        self.app.httpd.server_close()
        self.thread.join(3)
        super().tearDown()

    def dates(self, start='2026-03-01', end='2026-03-03'):
        self.row.locator('[data-date=start]').fill(start)
        self.row.locator('[data-date=end]').fill(end)

    def test_save_reopen_and_change_time_mode_preserves_query(self):
        from playwright.sync_api import expect
        self.dates()
        self.row.locator('[data-number="0"]').fill('3')
        self.page.locator('#jobSaveBtn').click()
        expect(self.page.locator('#jobDlg')).not_to_be_visible()
        job = self.page.locator('.job').filter(has_text='Sentetik tarih işi')
        expect(job).to_contain_text('01.03.2026–03.03.2026')
        job.locator('[data-edit]').click()
        expect(self.row.locator('[data-date=start]')).to_have_value('2026-03-01')
        expect(self.row.locator('[data-date=end]')).to_have_value('2026-03-03')
        expect(self.row.locator('[data-family="1"]')).to_have_value('exactly')
        self.row.locator('[data-family="2"]').select_option('in the last')
        expect(self.row.locator('[data-date]')).to_have_count(0)
        expect(self.row.locator('[data-family="3"]')).to_have_value('Day')
        expect(self.row.locator('[data-number="0"]')).to_have_value('3')
        expect(self.row.locator('[data-number="1"]')).to_have_value('7')
        self.page.locator('#jobSaveBtn').click()
        expect(self.page.locator('#jobDlg')).not_to_be_visible()
        profile = store.load_profile('demo-sahte-veri')
        rule = profile['jobs'][-1]['items'][0]
        self.assertNotIn('date_range', rule)
        self.assertEqual(rule['numbers'], [3,7])
        self.assertNotIn('between dates', rule['steps'])
        self.assertEqual(self.js_errors, [])

    def test_missing_and_reversed_dates_cannot_save(self):
        from playwright.sync_api import expect
        self.page.locator('#jobSaveBtn').click()
        expect(self.page.locator('#jErr')).to_contain_text('başlangıç ve bitiş')
        expect(self.page.locator('#jobDlg')).to_be_visible()
        self.dates('2026-03-03', '2026-03-01')
        self.page.locator('#jobSaveBtn').click()
        expect(self.page.locator('#jErr')).to_contain_text('başlangıç tarihi bitişten sonra')
        self.assertEqual(len(store.load_profile('demo-sahte-veri')['jobs']), 6)

    def test_help_modal_keyboard_focus_and_responsive_layout(self):
        from playwright.sync_api import expect
        self.dates()
        help_button = self.row.locator('[data-info=dates]')
        help_button.focus()
        self.page.keyboard.press('Enter')
        expect(self.page.locator('#infoDlg')).to_be_visible()
        expect(self.page.locator('#infoTitle')).to_have_text('between dates nasıl çalışır?')
        self.page.keyboard.press('Escape')
        expect(self.page.locator('#infoDlg')).not_to_be_visible()
        expect(help_button).to_be_focused()
        # Bilgi açmak, düzenlenmekte olan tarihleri veya kuralı sıfırlamamalı.
        expect(self.row.locator('[data-date=start]')).to_have_value('2026-03-01')
        screenshot_dir = os.environ.get('NETMERA_UI_SCREENSHOT_DIR')
        for width in (1280,390):
            self.page.set_viewport_size({'width':width, 'height':1000})
            self.page.locator('#jobDlg .dlg-b').evaluate('e => e.scrollTop = 0')
            self.assertTrue(self.page.locator('#jobDlg').evaluate('e => e.scrollWidth <= e.clientWidth + 1'))
            self.assertTrue(self.row.evaluate('e => e.scrollWidth <= e.clientWidth + 1'))
            if screenshot_dir:
                folder = Path(screenshot_dir); folder.mkdir(parents=True, exist_ok=True)
                self.page.screenshot(path=str(folder / f'people-{width}.png'))
        self.assertEqual(self.js_errors, [])

    def test_excel_modes_and_target_sheet_are_saved_from_settings(self):
        from playwright.sync_api import expect
        self.page.locator('#jobDlg .dlg-h [data-close]').click()
        self.page.locator('[data-tab=settings]').click()
        self.page.locator('#setExcelMode').select_option('append_rows')
        expect(self.page.locator('#setExcelSheetWrap')).to_be_visible()
        self.page.locator('#setExcelSheet').fill('Mevcut Rapor')
        self.page.locator('#saveSettingsBtn').click()
        expect(self.page.locator('#toast')).to_contain_text('Ayarlar kaydedildi')
        profile=store.load_profile('demo-sahte-veri')
        self.assertEqual(profile['excel_mode'],'append_rows'); self.assertEqual(profile['excel_sheet'],'Mevcut Rapor')
        self.page.locator('#setExcelMode').select_option('new_sheet')
        expect(self.page.locator('#excelSheetLabel')).to_have_text('Yeni sayfa adı başlangıcı')
        self.page.locator('#setExcelMode').select_option('rebuild')
        expect(self.page.locator('#setExcelSheetWrap')).not_to_be_visible()
        self.assertEqual(self.js_errors,[])

    def test_schedule_shows_windows_state_excel_failure_and_explicit_start(self):
        from playwright.sync_api import expect
        self.page.locator('#jobDlg .dlg-h [data-close]').click()
        record={'status':'excel_error','started_at':'2026-03-10T09:00:00+03:00','completed_at':'2026-03-10T09:00:30+03:00',
                'ok':6,'failed':0,'excel_written':False,'excel':'Dosya kilitli; sonuçlar kaydedildi.'}
        store.write_json(store.profile_dir('demo-sahte-veri')/'zamanli-son-durum.json',record)
        info={'installed':True,'enabled':True,'path_matches':True,'state':3,'task':'Sentetik görev',
              'last_run':'2026-03-10T09:00:00+03:00','next_run':'2026-03-11T09:00:00+03:00','result_hex':'0x00000006'}
        with patch('netmera_automation.scheduler.status',return_value=info), patch('netmera_automation.scheduler.run_now') as launch:
            self.page.locator('[data-tab=schedule]').click()
            expect(self.page.locator('#taskDetails')).to_contain_text('0x00000006')
            expect(self.page.locator('#scheduledReceipt')).to_contain_text('Çıktıya yazılamadı')
            expect(self.page.locator('#scheduledReceipt')).to_contain_text('6 başarılı, 0 hatalı')
            expect(self.page.locator('#runTaskBtn')).to_be_enabled()
            self.page.locator('#runTaskBtn').click()
            expect(self.page.locator('#toast')).to_contain_text('başlatma isteği')
            self.assertEqual(launch.call_count,1)
        info['path_matches']=False
        with patch('netmera_automation.scheduler.status',return_value=info):
            self.page.locator('#refreshTaskBtn').click()
            expect(self.page.locator('#taskPill')).to_contain_text('Eski klasör')
            expect(self.page.locator('#runTaskBtn')).to_be_disabled()
        self.assertEqual(self.js_errors,[])

    def test_schedule_friendly_name_cleanup_and_remove_use_scoped_backend(self):
        from playwright.sync_api import expect
        from tests.test_scheduler_cleanup import FakeWindows, task
        from netmera_automation import scheduler
        self.page.locator('#jobDlg .dlg-h [data-close]').click()
        profile=store.load_profile('demo-sahte-veri')
        old_name='Netmera Otomasyonu - demo-sahte-veri'
        fake=FakeWindows([task(profile,old_name)])
        prompts=[]
        def accept(dialog): prompts.append(dialog.message); dialog.accept()
        with patch('netmera_automation.scheduler._inventory',side_effect=fake.inventory),patch('netmera_automation.scheduler._schtasks',side_effect=fake.schtasks):
            self.page.locator('[data-tab=schedule]').click()
            expect(self.page.locator('#relatedTasks')).to_contain_text(old_name)
            self.page.locator('#taskNameInput').fill('Sabah kişi ve olay raporu')
            expect(self.page.locator('#taskNamePreview')).to_contain_text('Sabah kişi ve olay raporu')
            self.page.locator('#addTimeBtn').click()
            expect(self.page.locator('#timeChips')).to_contain_text('09:00')
            self.page.once('dialog',accept)
            self.page.locator('#resetTaskBtn').click()
            expect(self.page.locator('#toast')).to_contain_text('eski kayıt temizlendi')
            self.assertIn(old_name,prompts[0])
            saved=store.load_profile('demo-sahte-veri')
            self.assertEqual(saved['schedule']['name'],'Sabah kişi ve olay raporu')
            self.assertEqual(fake.tasks[0]['name'],scheduler.task_name(saved))
            expect(self.page.locator('#relatedTasks')).to_contain_text('Sabah kişi ve olay raporu')
            self.page.once('dialog',accept)
            self.page.locator('#removeTaskBtn').click()
            expect(self.page.locator('#taskPill')).to_have_text('Kurulu değil')
            self.assertEqual(fake.tasks,[])
            self.assertEqual(store.load_profile('demo-sahte-veri')['schedule']['times'],['09:00'])
            self.assertEqual(len(store.load_profile('demo-sahte-veri')['jobs']),6)
        self.assertEqual(self.js_errors,[])

    def test_cancelled_cleanup_does_not_delete_tasks(self):
        from playwright.sync_api import expect
        from tests.test_scheduler_cleanup import FakeWindows, task
        self.page.locator('#jobDlg .dlg-h [data-close]').click()
        fake=FakeWindows([task(store.load_profile('demo-sahte-veri'),'Eski rapor')])
        with patch('netmera_automation.scheduler._inventory',side_effect=fake.inventory),patch('netmera_automation.scheduler._schtasks',side_effect=fake.schtasks):
            self.page.locator('[data-tab=schedule]').click()
            expect(self.page.locator('#relatedTasks')).to_contain_text('Eski rapor')
            self.page.once('dialog',lambda d:d.dismiss())
            self.page.locator('#removeTaskBtn').click()
            expect(self.page.locator('#removeTaskBtn')).to_be_enabled()
            self.assertEqual(fake.calls,[])
            self.assertEqual(fake.tasks[0]['name'],'Eski rapor')
