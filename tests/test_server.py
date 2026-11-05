from http.server import ThreadingHTTPServer
from http.client import HTTPConnection
import json
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import patch

from netmera_automation import server, store
from tests.helpers import TempDataCase


class ServerTests(TempDataCase):
    def test_project_export_import_requires_token_validates_and_keeps_existing_profiles(self):
        pid = 'demo-sahte-veri'
        before = store.load_profile(pid)
        request = {'profile_ids': [pid]}
        self.assertEqual(self.call('/api/projects/export', request, token='wrong')[0], 403)
        code, exported = self.call('/api/projects/export', request)
        self.assertEqual(code, 200, exported)
        body = {'bundle': exported['bundle'], 'indices': [0]}
        self.assertEqual(self.call('/api/projects/import', body, token='wrong')[0], 403)
        self.assertEqual(self.call('/api/projects/import-preview', {'bundle': {}})[0], 400)
        code, preview = self.call('/api/projects/import-preview', body)
        self.assertEqual(code, 200, preview)
        self.assertEqual(len(store.list_profiles()), 2)
        code, result = self.call('/api/projects/import', body)
        self.assertEqual((code, result['count']), (200, 1), result)
        self.assertEqual(store.load_profile(pid), before)
        new = store.load_profile(result['projects'][0]['id'])
        self.assertTrue(all(not j['enabled'] for j in new['jobs']))
        # Yalnız içe alma uçları daha büyük, sınırlı gövde kabul eder.
        exported['bundle']['projects'][0]['ignored_field'] = 'x' * 1_050_000
        self.assertEqual(self.call('/api/projects/import-preview', {'bundle': exported['bundle']})[0], 200)
        self.assertEqual(self.call('/api/projects/export', {**request, 'padding': 'x' * 1_050_000})[0], 413)

    def test_invalid_json_body_and_content_length_return_bounded_errors(self):
        for raw, length, want in [(b'[]', '2', 400), (b'null', '4', 400), (b'1', '1', 400),
                                  (b'{}', '-1', 400), (b'{}', 'invalid', 400), (b'', '1000001', 413)]:
            with self.subTest(raw=raw, length=length):
                connection = HTTPConnection('127.0.0.1', self.app.port, timeout=2)
                try:
                    connection.putrequest('POST', '/api/profile/create')
                    for name, value in {'X-Token': self.app.token, 'Content-Type': 'application/json', 'Content-Length': length}.items():
                        connection.putheader(name, value)
                    connection.endheaders(raw)
                    response = connection.getresponse()
                    self.assertEqual(response.status, want)
                    self.assertIn('error', json.loads(response.read()))
                finally:
                    connection.close()
        self.assertEqual(self.call('/api/state')[0], 200)

    def test_retry_without_pending_results_reports_no_write_and_explains_test_mode(self):
        p=store.load_profile('demo-sahte-veri')
        store.write_json(store.profile_dir(p['id'])/'son-calisma.json',{'write':False,'rows':[],'day_rows':[]})
        code,response=self.call('/api/excel/rebuild',{'profile_id':p['id']})
        self.assertEqual(code,200,response)
        self.assertFalse(response['written']);self.assertEqual(response['written_batches'],0)
        self.assertIn('Yeni çıktı yazılmadı',response['message'])
        self.assertIn('Son TEST',response['message'])
        self.assertIn('Çalıştır ve yaz',response['message'])
        with patch.object(server.runner,'rebuild_excel',return_value=2):
            code,response=self.call('/api/excel/rebuild',{'profile_id':p['id']})
        self.assertTrue(response['written']);self.assertEqual(response['written_batches'],2)
        self.assertNotIn('Son TEST',response['message'])

    def test_preview_api_passes_daily_results_to_both_output_providers(self):
        from tests.test_cell_mapping import mapped_profile, result
        from netmera_automation import cell_mapping, google_sheets
        p=mapped_profile(self.tmp)
        daily=[result(p,catch_up=True)]
        store.write_json(store.profile_dir(p['id'])/'son-calisma.json',{'rows':[],'day_rows':daily,'write':False})
        with patch.object(cell_mapping,'preview',return_value={'ready':1,'errors':0,'written':False}) as preview:
            code,response=self.call('/api/excel/preview',{'profile':p})
            self.assertEqual((code,response['ready']),(200,1))
            self.assertEqual(preview.call_args.kwargs['day_rows'],daily)
            self.assertEqual(preview.call_args.args[1],[])
        p.update(source='netmera',netmera_app='Sentetik',output_target='google_sheets',sheets_id='synthetic_sheet_12345')
        with patch.object(google_sheets,'preview',return_value={'ready':1,'errors':0,'written':False}) as preview:
            code,response=self.call('/api/excel/preview',{'profile':p})
            self.assertEqual((code,response['ready']),(200,1))
            self.assertEqual(preview.call_args.kwargs['day_rows'],daily)

    def test_google_api_no_secrets_demo_rejected_and_preview_readonly(self):
        from pathlib import Path
        from openpyxl import Workbook
        from netmera_automation import google_auth
        from tests.test_cell_mapping import mapped_profile,result
        prod=next(p for p in self.call('/api/state')[1]['profiles'] if p['source']=='netmera')
        body={'profile_id':prod['id'],'client':{'installed':{'client_id':'synthetic.apps.googleusercontent.com','client_secret':'synthetic-server-secret'}}}
        self.assertEqual(self.call('/api/google/client',body,token='wrong')[0],403)
        code,response=self.call('/api/google/client',body)
        self.assertEqual(code,200,response);self.assertTrue(response['google']['configured'])
        self.assertNotIn('synthetic-server-secret',json.dumps(response))
        self.assertNotIn('protected',json.dumps(self.call('/api/state?profile='+prod['id'])[1]))
        self.assertEqual(self.call('/api/google/client',dict(body,profile_id='demo-sahte-veri'))[0],400)
        self.assertEqual(self.call('/api/google/clear',{'profile_id':prod['id']})[0],200)
        self.assertFalse(google_auth.status(prod['id'])['connected'])  # tek Google ayarı: giriş kapanır, JSON kalır
        self.assertTrue(google_auth.status(prod['id'])['configured'])
        p=mapped_profile(self.tmp);path=Path(p['output_file']);path.parent.mkdir(parents=True,exist_ok=True)
        b=Workbook();b.active.title='Rapor';b.save(path);b.close();original=path.read_bytes()
        store.write_json(store.profile_dir(p['id'])/'son-calisma.json',{'rows':[result(p)]})
        code,preview=self.call('/api/excel/preview',{'profile':p})
        self.assertEqual(code,200,preview);self.assertEqual(preview['ready'],1);self.assertFalse(preview['written'])
        self.assertEqual(path.read_bytes(),original)

    def test_login_config_api_never_returns_password_or_ciphertext_and_clone_is_empty(self):
        from netmera_automation import login_config
        profiles=self.call('/api/state')[1]['profiles']
        prod=next(p for p in profiles if p['source']=='netmera'); demo=next(p for p in profiles if p['source']=='demo')
        password='only-synthetic-api-secret'
        data={'profile_id':prod['id'],'enabled':True,'keep_logged_in':True,'username':'synthetic@example.invalid','password':password}
        code,result=self.call('/api/login-config/save',data,token='wrong')
        self.assertEqual(code,403)
        code,result=self.call('/api/login-config/save',data)
        self.assertEqual(code,200,result); self.assertTrue(result['login_config']['ready'])
        for obj in (result,self.call('/api/state?profile='+prod['id'])[1],self.call('/api/login-config/status?profile='+prod['id'])[1]):
            self.assertNotIn(password,json.dumps(obj)); self.assertNotIn('protected_credentials',json.dumps(obj))
        self.assertEqual(self.call('/api/login-config/save',{**data,'profile_id':demo['id']})[0],400)
        copied=self.call('/api/profile/create',{'name':'Sentetik Kopya','source':'netmera','copy_from':prod['id']})[1]['profile_id']
        self.assertFalse(login_config.status(copied)['has_credentials'])
        self.assertNotIn(password,login_config.config_path(prod['id']).read_text(encoding='utf-8'))
        with patch('netmera_automation.server.os.startfile',create=True) as opened:
            self.assertEqual(self.call('/api/open',{'profile_id':prod['id'],'what':'login_config'})[0],200)
            opened.assert_called_once_with(login_config.config_path(prod['id']))
        code,result=self.call('/api/login-config/clear',{'profile_id':prod['id']})
        self.assertEqual(code,200); self.assertFalse(result['login_config']['enabled']); self.assertFalse(result['login_config']['has_credentials'])

    def setUp(self):
        super().setUp()
        store.ensure_defaults()
        self.app = server.App(server._free_port(0))
        self.app.httpd = ThreadingHTTPServer(("127.0.0.1", self.app.port), self.app.make_handler())
        threading.Thread(target=self.app.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.app.port}"

    def tearDown(self):
        self.app.httpd.shutdown()
        self.app.httpd.server_close()
        super().tearDown()

    def call(self, path, body=None, token=None, headers=None):
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"X-Token": token or self.app.token, "Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}") if "json" in exc.headers.get("Content-Type", "") else {}

    def test_token_and_host_are_required(self):
        self.assertEqual(self.call("/api/state", token="yanlis")[0], 403)
        self.assertEqual(self.call("/api/state", headers={"Host": "evil.example"})[0], 403)
        with urllib.request.urlopen(f"{self.base}/?t={self.app.token}") as page:
            html = page.read().decode()
        self.assertIn(self.app.token, html)
        self.assertNotIn("__TOKEN__", html)
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(f"{self.base}/")

    def test_edit_and_run_demo_profile_through_api(self):
        code, state = self.call("/api/state")
        self.assertEqual(code, 200)
        demo = next(p for p in state["profiles"] if p["source"] == "demo")
        code, state = self.call(f"/api/state?profile={demo['id']}")
        profile = state["profile"]
        profile["jobs"].append({"type": "event", "label": "Yeni İş", "event": "Sentetik", "metric": "unique_user_count",
                                "filters": [{"property": "Alan", "operator": "In", "values": ["a", "b"]}],
                                "period": {"type": "last_n_days", "days": 3}, "columns": ["All Total", "Android"]})
        code, saved = self.call("/api/profile/save", {"profile": profile})
        self.assertEqual(code, 200, saved)
        bad = dict(profile, jobs=profile["jobs"] + [{"type": "event", "label": "Yeni İş"}])
        code, error = self.call("/api/profile/save", {"profile": bad})
        self.assertEqual(code, 400)
        self.assertIn("Event adı", error["error"])

        with patch("netmera_automation.synthetic.time.sleep"):
            self.assertEqual(self.call("/api/run", {"profile_id": demo["id"], "write": True})[0], 200)
            for _ in range(100):
                status = self.call("/api/status")[1]
                if not status["busy"]:
                    break
                time.sleep(0.1)
        self.assertIsNone(status["error"], status["lines"])
        self.assertEqual(status["result"]["ok"], 8)
        state = self.call(f"/api/state?profile={demo['id']}")[1]
        self.assertEqual(state["last_run"]["ok"], 8)

    def test_prod_profile_needs_settings_before_schedule(self):
        prod = next(p for p in self.call("/api/state")[1]["profiles"] if p["source"] == "netmera")
        code, body = self.call("/api/schedule/install", {"profile_id": prod["id"]})
        self.assertEqual(code, 400)
        self.assertIn("Netmera", body["error"])
        with patch("netmera_automation.scheduler.status", return_value={'installed':False}):
            code, body = self.call(f"/api/schedule/status?profile={prod['id']}")
        self.assertEqual((code, body["installed"]), (200, False))

    def test_reset_requires_ready_profile_and_calls_only_scoped_scheduler(self):
        profiles=self.call('/api/state')[1]['profiles']
        prod=next(p for p in profiles if p['source']=='netmera')
        demo=next(p for p in profiles if p['source']=='demo')
        with patch('netmera_automation.scheduler.reset',return_value={'task':'Sentetik görev','removed':2,'installed':True}) as reset:
            self.assertEqual(self.call('/api/schedule/reset',{'profile_id':prod['id']})[0],400)
            reset.assert_not_called()
            code,body=self.call('/api/schedule/reset',{'profile_id':demo['id']})
            self.assertEqual(code,200); self.assertEqual(body['removed'],2)
            self.assertEqual(reset.call_args.args[0]['id'],demo['id'])
