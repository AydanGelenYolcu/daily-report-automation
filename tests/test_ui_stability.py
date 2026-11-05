"""Yavaş istekler ve büyümüş sonuçlarla gezinme; yalnız geçici/sentetik veriler."""
import json
from http.server import ThreadingHTTPServer
import threading
import time
import unittest
from unittest.mock import patch

from netmera_automation import server, store
from tests.helpers import TempDataCase, browser_test_args
from tests.test_panel_browser import sync_playwright


class StateStabilityTests(TempDataCase):
    def setUp(self):
        super().setUp()
        store.ensure_defaults()
        self.app = server.App(0)
        self.pid = 'demo-sahte-veri'

    def test_large_reports_stay_out_of_navigation_and_status(self):
        row = {'status': 'OK', 'note': 'sentetik ' * 80, 'value': 7}
        run = {'run_id': 'synthetic', 'profile_id': self.pid, 'ok': 20000, 'failed': 0,
               'rows': [row] * 20000, 'day_rows': [], 'ga4': [{'satirlar': [row] * 20000}]}
        store.write_last_run(self.pid, run)
        self.app.worker.task = 'run'
        self.app.worker.result = run
        original = store.read_json

        def read(path, *args):
            self.assertNotEqual(path.name, 'son-calisma.json', 'Gezinme tam raporu okudu')
            return original(path, *args)

        with patch.object(store, 'read_json', side_effect=read):
            state = self.app.state(self.pid)
            status = self.app.worker.status()
        self.assertEqual(len(state['last_run']['rows']), 200)
        self.assertEqual(state['last_run']['ga4_count'], 1)
        self.assertLess(len(json.dumps(status)), 2000)
        self.assertLess(len(json.dumps(state)), 300000)
        self.assertEqual(len(original(store.profile_dir(self.pid) / 'son-calisma.json')['rows']), 20000)

    def test_old_large_file_is_deferred_without_parsing(self):
        path = store.profile_dir(self.pid) / 'son-calisma.json'
        path.write_text(' ' * 600000, encoding='utf-8')
        state = self.app.state(self.pid)
        self.assertTrue(state['last_run']['history_deferred'])

    def test_stale_settings_cannot_restore_disabled_job(self):
        stale = self.app.state(self.pid)
        job = stale['profile']['jobs'][0]
        self.app.handle('POST', '/api/jobs/enabled', {'profile_id': self.pid, 'job_id': job['id'], 'enabled': False}, {})
        with self.assertRaisesRegex(ValueError, 'başka bir işlemde değişti'):
            self.app.handle('POST', '/api/profile/save', {'profile': stale['profile'], 'revision': stale['revision']}, {})
        self.assertFalse(store.load_profile(self.pid)['jobs'][0]['enabled'])

    def test_background_package_sync_never_requests_network_metadata(self):
        with patch.object(self.app.ga4, 'meta_veya_yok', side_effect=AssertionError('Ağ çağrısı')):
            response = self.app.handle('POST', '/api/ga4/paketler/esitle',
                                       {'profile_id': 'production', 'cached_only': True}, {})
        self.assertEqual(response['eklenen'], [])

    def test_metrics_are_bounded_and_do_not_contain_query_or_payload(self):
        for _ in range(120):
            self.app.metrics.record('GET', '/api/state', 1.2, 200, 12)
        self.assertEqual(len(self.app.metrics.snapshot()), 100)
        path = server.data_root() / 'arayuz-performans.jsonl'
        self.assertTrue(path.is_file())
        text = path.read_text(encoding='utf-8')
        self.assertNotIn(self.app.token, text)
        self.assertNotIn('profile', text)


@unittest.skipIf(sync_playwright is None, 'playwright yok')
class NavigationRaceTests(TempDataCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(channel='chrome', headless=True, args=browser_test_args())
        except Exception:
            cls.pw.stop()
            raise unittest.SkipTest('Chrome yok')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        super().setUp()
        store.ensure_defaults()
        self.other = store.create_profile('Daily 2 sentetik', source='demo')
        self.app = server.App(server._free_port(0))
        self.app.httpd = ThreadingHTTPServer(('127.0.0.1', self.app.port), self.app.make_handler())
        self.thread = threading.Thread(target=self.app.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.app.port}'
        self.context = self.browser.new_context()
        self.context.route('**/*', lambda r: r.continue_() if r.request.url.startswith(self.base + '/') else r.abort())
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda err: self.errors.append(str(err)))
        self.page.goto(f'{self.base}/?t={self.app.token}')
        self.page.locator('[data-open-project="demo-sahte-veri"]').click()
        self.page.wait_for_function("S?.profile.id === 'demo-sahte-veri' && window.NM && window.GA")

    def tearDown(self):
        self.context.close()
        self.app.httpd.shutdown()
        self.app.httpd.server_close()
        self.thread.join(5)
        super().tearDown()

    def test_delayed_project_response_cannot_replace_newer_selection(self):
        held = []
        self.page.route('**/api/state?profile=production', lambda r: held.append(r))
        self.page.evaluate("void NM.select('production')")
        self.page.wait_for_function("document.querySelector('#nm').getAttribute('aria-busy') === 'true'")
        self.page.evaluate('(id) => NM.select(id)', self.other['id'])
        self.assertEqual(self.page.locator('#projTitle').inner_text(), self.other['name'])
        # İptal edilmiş eski yanıtı daha sonra teslim etmeyi dene.
        self.assertTrue(held)
        held[0].fulfill(json=self.app.state('production'))
        self.page.wait_for_timeout(200)
        self.assertEqual(self.page.evaluate('S.profile.id'), self.other['id'])
        self.assertEqual(self.errors, [])

    def test_hundreds_of_jobs_are_paged_searchable_and_keep_original_action_index(self):
        from tests.test_ga4_merge import ga4_job
        from playwright.sync_api import expect
        p=store.load_profile('demo-sahte-veri')
        p['jobs']=[ga4_job(f'Sentetik rapor {i:03}') for i in range(300)]
        store.save_profile(p)
        self.page.evaluate('NM.refresh()')
        expect(self.page.locator('#jobList .job')).to_have_count(50)
        self.page.locator('#jobsNext').click()
        expect(self.page.locator('#jobList .job .t').first).to_have_text('Sentetik rapor 050')
        self.page.locator('#jobSearch').fill('Sentetik rapor 299')
        expect(self.page.locator('#jobList .job')).to_have_count(1)
        self.page.locator('.switch span').click()
        expect(self.page.locator('[data-toggle="299"]')).to_be_enabled()
        self.assertFalse(store.load_profile(p['id'])['jobs'][299]['enabled'])
        self.assertTrue(store.load_profile(p['id'])['jobs'][0]['enabled'])
        self.page.locator('#jobSearch').fill('')
        self.page.locator('#selectAllJobs').check()
        expect(self.page.locator('#jobSelectionCount')).to_have_text('300 iş seçili')
        self.page.evaluate('GA.jobsChanged()')
        self.page.locator('.nav-oge[href="#/ga4/isler"]').click()
        expect(self.page.locator('#ga-sayfa tbody tr')).to_have_count(50)
        self.page.get_by_role('button',name='Sonraki GA4 işleri',exact=True).click()
        expect(self.page.locator('#ga-sayfa tbody tr')).to_have_count(50)
        self.assertEqual(self.errors,[])

    def test_google_linked_long_named_jobs_are_downloaded_with_all_bindings(self):
        from tests.test_ga4_merge import ga4_job
        from netmera_automation.ga4 import ayarlar as settings
        from pathlib import Path
        p=store.load_profile('demo-sahte-veri')
        p.update(jobs=[ga4_job('Sentetik aynı uzun rapor başlığı '+ 'x'*35+str(i),
                sheets={'tablo':'synthetic_per_job_12345','sekme':f'Sayfa {i}'}) for i in range(3)],
            output_target='google_sheets',sheets_id='synthetic_output_12345',excel_mode='mapped_cells')
        p['ga4']['sheets']['tablo']='synthetic_ga4_default_12345'
        p=store.save_profile(p)
        settings._yaz({'propertyler':{'PROP_PROD':{'property_id':'123456789'}}})
        self.page.locator('.nav-oge[href="#/projeler"]').click()
        with self.page.expect_download() as captured:
            self.page.locator('[data-export-project="demo-sahte-veri"]').click()
        bundle=json.loads(Path(captured.value.path()).read_text(encoding='utf-8'))
        self.assertEqual(bundle['projects'][0],p)
        self.assertEqual(bundle['connections']['ga4_properties']['PROP_PROD']['property_id'],'123456789')
        self.assertEqual(self.errors,[])

    def test_library_jobs_are_added_only_by_explicit_update(self):
        from tests.test_ga4_merge import ga4_job
        from playwright.sync_api import expect
        p=store.load_profile('demo-sahte-veri')
        p['jobs']=[ga4_job()]
        p['ga4']['paketler']=[{'kimlik':'kutuphane:yasam-dongusu/edinme','ad':'Sentetik paket','haric':[]}]
        store.save_profile(p)
        calls=[]
        def sync(route):
            calls.append(route.request.url); route.continue_()
        self.page.route('**/api/ga4/paketler/esitle',sync)
        self.page.reload()
        self.page.locator('[data-open-project="demo-sahte-veri"]').click()
        self.page.locator('.nav-oge[href="#/ga4/isler"]').click()
        expect(self.page.locator('#ga-sayfa h1')).to_have_text('GA4 işleri')
        self.assertEqual(calls,[])
        self.assertEqual(len(store.load_profile(p['id'])['jobs']),1)
        with self.page.expect_request('**/api/ga4/paketler/esitle'):
            self.page.get_by_role('button',name='Hazır raporları güncelle',exact=True).click()
        self.assertEqual(len(calls),1)
        expect(self.page.get_by_role('button',name='Hazır raporları güncelle',exact=True)).to_be_enabled()
        self.assertEqual(self.errors,[])

    def test_two_job_toggles_save_to_original_project_during_navigation(self):
        held = []
        self.page.route('**/api/jobs/enabled', lambda r: held.append(r) if not held else r.continue_())
        self.page.locator('.switch span').nth(0).click()
        self.page.locator('.switch span').nth(1).click()
        self.page.evaluate('(id) => NM.select(id)', self.other['id'])
        self.assertTrue(held)
        held[0].continue_()
        self.page.wait_for_function("""async () => {
          const r = await fetch('/api/state?profile=demo-sahte-veri', {headers:{'X-Token':document.querySelector('meta[name=token]').content}});
          const s = await r.json(); return s.profile.jobs.slice(0,2).every(j => !j.enabled);
        }""")
        self.assertEqual(self.page.evaluate('S.profile.id'), self.other['id'])
        self.assertFalse(store.load_profile('demo-sahte-veri')['jobs'][0]['enabled'])
        self.assertEqual(self.errors, [])

    def test_read_timeout_is_visible_and_retry_recovers(self):
        held = []
        self.page.route('**/api/state?profile=production', lambda r: held.append(r))
        self.page.evaluate("void NM.select('production').catch(() => {})")
        from playwright.sync_api import expect
        expect(self.page.locator('#navFeedback')).to_contain_text('15 saniyede', timeout=20000)
        for route in held:
            route.abort()
        self.page.unroute('**/api/state?profile=production')
        self.page.get_by_role('button', name='Tekrar dene', exact=True).click()
        expect(self.page.locator('#projTitle')).to_have_text('Production')
        self.assertEqual(self.errors, [])

    def test_ga4_connection_wait_does_not_block_startup_or_project_selection(self):
        from playwright.sync_api import expect
        held = []
        self.page.route('**/api/ga4/durum**', lambda r: held.append(r))
        self.page.reload(wait_until='domcontentloaded')
        expect(self.page.locator('#projectCards')).to_be_visible(timeout=5000)
        self.page.locator('[data-open-project="demo-sahte-veri"]').click()
        expect(self.page.locator('#proje-secim')).to_be_visible(timeout=5000)
        self.page.locator('#proje-secim').select_option(self.other['id'])
        expect(self.page.locator('#projTitle')).to_have_text(self.other['name'], timeout=5000)
        for route in held:
            route.abort()
        self.assertEqual(self.errors, [])

    def test_run_completion_preserves_settings_draft_and_refreshes_results_later(self):
        from playwright.sync_api import expect
        self.page.locator('.nav-oge[href="#/ayarlar"]').click()
        self.page.locator('#setName').fill('Kaydedilmemiş sentetik taslak')
        done = threading.Event()
        self.addCleanup(done.set)
        self.app.worker.start('run', 'demo-sahte-veri', lambda log, cancel: done.wait(20))
        expect(self.page.locator('#statusPill')).to_contain_text('sürüyor', timeout=10000)
        run = {'profile_id':'demo-sahte-veri', 'run_id':'new-result', 'planned_at':'2026-11-03T09:00:00+03:00',
               'ok':1, 'failed':0, 'write':False, 'rows':[{'job_label':'Yeni sentetik sonuç', 'value':42, 'status':'OK'}]}
        store.write_last_run('demo-sahte-veri', run)
        done.set()
        expect(self.page.locator('#statusPill')).to_have_text('Hazır', timeout=10000)
        expect(self.page.locator('#setName')).to_have_value('Kaydedilmemiş sentetik taslak')
        self.page.locator('.nav-oge[href="#/calistir"]').click()
        expect(self.page.locator('#resBody')).to_contain_text('Yeni sentetik sonuç')
        expect(self.page.locator('#setName')).to_have_value('Kaydedilmemiş sentetik taslak')
        self.assertEqual(self.errors, [])

    def test_large_ga4_result_is_loaded_only_on_results_page_and_paginated(self):
        from netmera_automation.ga4 import isler, rapor
        from netmera_automation.ga4.sentetik import SyntheticClient
        from playwright.sync_api import expect
        job = isler.normalize_job({'ad': 'Sentetik tablo', 'property_ref': 'PROP_PROD',
                                   'metrikler': ['activeUsers'], 'boyutlar': ['deviceCategory'], 'tarih': {'tur':'dun'}})
        tables = rapor.run_jobs([job], SyntheticClient())['sonuclar']
        tables[0]['satirlar'] = [{'boyutlar':{'deviceCategory':f'sentetik-{n}'}, 'metrikler':{'activeUsers':n}} for n in range(650)]
        run = {'profile_id':'demo-sahte-veri', 'run_id':'synthetic-page-test', 'planned_at':'2026-11-03T09:00:00+03:00',
               'seconds':1, 'ok':1, 'failed':0, 'write':False, 'rows':[], 'ga4':tables}
        store.write_last_run('demo-sahte-veri', run)
        self.page.evaluate('NM.refresh()')
        self.page.locator('.nav-oge[href="#/calistir"]').click()
        self.page.locator('#ga4RunCard a').click()
        expect(self.page.locator('.sonuc-tablo tbody tr')).to_have_count(101)
        self.page.get_by_role('button', name='Sonraki sayfa', exact=True).click()
        expect(self.page.locator('.sonuc-tablo tbody tr')).to_have_count(101)
        expect(self.page.locator('.tablo-arac .ipucu')).to_contain_text('101–200 / 650')
        self.assertEqual(len(store.read_json(store.profile_dir('demo-sahte-veri')/'son-calisma.json')['ga4'][0]['satirlar']), 650)
        self.assertEqual(self.errors, [])
