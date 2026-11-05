"""Sadece uydurma hesap, geçici dosya ve tüm ağı yakalanmış sahte panel.

Gerçek parola/oturum kullanılmaz. Windows'ta gerçek DPAPI de sınanır.
"""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import threading
from urllib.parse import urlparse
from unittest import skipUnless
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from netmera_automation import jobs as J, login_config as C, runner, store
from netmera_automation.login_session import AutoLogin, is_login_page
from netmera_automation.panel import PanelError, PanelReader
from tests.helpers import TempDataCase, event_job, browser_test_args
from tests.test_panel_browser import MOCK, sync_playwright

USER='synthetic@example.invalid'
PASSWORD='synthetic-Only-42-ç!'
LOGIN=Path(__file__).with_name('mock_login.html').read_text(encoding='utf-8')


def profile():
    store.ensure_defaults()
    p=next(p for p in store.list_profiles() if p['source']=='netmera')
    p.update(netmera_app='Sentetik App',timeout_seconds=10,headless=True,jobs=[event_job(event='Open App')])
    return store.save_profile(p)


@skipUnless(os.name=='nt','Windows DPAPI')
class LoginConfigTests(TempDataCase):
    def setUp(self):
        super().setUp(); self.profile=profile(); self.pid=self.profile['id']

    def test_native_dpapi_config_roundtrip_no_plaintext_or_password_response(self):
        state=C.status(self.pid)
        self.assertFalse(state['ready']); self.assertTrue(Path(state['path']).is_file())
        saved=C.save(self.pid,enabled=True,keep_logged_in=True,username=USER,password=PASSWORD)
        self.assertTrue(saved['ready']); self.assertEqual(saved['username'],USER)
        self.assertNotIn(PASSWORD,json.dumps(saved))
        disk=C.config_path(self.pid).read_text(encoding='utf-8')
        self.assertNotIn(PASSWORD,disk); self.assertNotIn(USER,disk)
        self.assertNotIn('protected_credentials',json.dumps(saved))
        config,secret=C.prepare(self.pid)
        self.assertEqual((secret['username'],secret['password']),(USER,PASSWORD))
        self.assertNotIn('protected_credentials',store.read_json(store.profile_dir(self.pid)/'profil.json'))
        C.save(self.pid,enabled=True,keep_logged_in=False,username=USER,password='')
        config,secret=C.prepare(self.pid)
        self.assertFalse(config['keep_logged_in']); self.assertEqual(secret['password'],PASSWORD)

    def test_blocked_attempt_requires_explicit_save_and_clear_disables(self):
        C.save(self.pid,enabled=True,keep_logged_in=True,username=USER,password=PASSWORD)
        config,_=C.prepare(self.pid); C.receipt(self.pid,config,'attempting')
        self.assertTrue(C.status(self.pid)['blocked'])
        with self.assertRaises(C.LoginConfigError): C.prepare(self.pid)
        C.save(self.pid,enabled=True,keep_logged_in=True,username=USER)
        self.assertTrue(C.status(self.pid)['ready'])
        result=C.clear(self.pid)
        self.assertFalse(result['enabled']); self.assertFalse(result['has_credentials'])

    def test_other_profile_cannot_use_copied_blob_and_copy_has_no_login(self):
        C.save(self.pid,enabled=True,keep_logged_in=True,username=USER,password=PASSWORD)
        other=store.create_profile('Başka Sentetik',source='netmera',copy_from=self.pid)
        self.assertFalse(C.status(other['id'])['has_credentials'])
        raw=store.read_json(C.config_path(self.pid))
        store.write_json(C.config_path(other['id']),raw)
        with self.assertRaises(C.LoginConfigError): C.prepare(other['id'])
        self.assertFalse(C.status(other['id'])['ready'])

    def test_invalid_config_account_change_and_secret_failures_never_echo_inputs(self):
        with self.assertRaises(C.LoginConfigError): C.save('../outside',enabled=True,keep_logged_in=True,username=USER,password=PASSWORD)
        with self.assertRaises(C.LoginConfigError): C.save(self.pid,enabled=True,keep_logged_in=True)
        C.save(self.pid,enabled=True,keep_logged_in=True,username=USER,password=PASSWORD)
        with self.assertRaises(C.LoginConfigError):
            C.save(self.pid,enabled=True,keep_logged_in=True,username='different@example.invalid')
        raw=store.read_json(C.config_path(self.pid)); raw['password']=PASSWORD
        store.write_json(C.config_path(self.pid),raw)
        self.assertNotIn(PASSWORD,json.dumps(C.status(self.pid)))
        with self.assertRaises(C.LoginConfigError) as ctx: C.prepare(self.pid)
        self.assertNotIn(PASSWORD,str(ctx.exception))
        C.clear(self.pid)
        with patch.object(C,'_dpapi',side_effect=C.LoginConfigError('Synthetic protection failed')):
            with self.assertRaises(C.LoginConfigError): C.save(self.pid,enabled=True,keep_logged_in=True,username=USER,password=PASSWORD)
        self.assertFalse(C.status(self.pid)['has_credentials'])


class AuthRoute:
    def __init__(self,*,authenticated=False,mode='success',expires=0,transient_path=None):
        self.authenticated=authenticated; self.mode=mode; self.expires=expires; self.posts=[]
        self.transient_path=transient_path

    def __call__(self,route):
        request=route.request; url=urlparse(request.url)
        if url.netloc!='panel.netmera.com': route.abort(); return
        if url.path=='/synthetic-login':
            data=json.loads(request.post_data); self.posts.append(data)
            ok=data['username']==USER and data['password']==PASSWORD and self.mode!='failure'
            if ok: self.authenticated=True
            route.fulfill(status=200,content_type='application/json',body=json.dumps({'ok':ok,'otp':self.mode=='otp'})); return
        if request.resource_type=='document':
            if url.path==self.transient_path:
                self.transient_path=None
                route.fulfill(status=200,content_type='text/html',body="""<body><script>
                  const target=location.pathname; history.replaceState(null,'','/login');
                  setTimeout(()=>location.href=target,250)</script></body>""")
                return
            if self.mode=='transient_login' and url.path=='/':
                self.mode='success'; self.authenticated=True
                route.fulfill(status=200,content_type='text/html',body="""<body>
                  <script>history.replaceState(null,'','/login');
                  setTimeout(()=>location.href='/',250)</script></body>""")
                return
            if url.path=='/analytics/event-insight' and self.expires:
                self.expires-=1; self.authenticated=False
            if url.path=='/login' or not self.authenticated:
                body=LOGIN
                if url.path!='/login':
                    body=body.replace('<body>',"<body><script>history.replaceState(null,'','/login')</script>")
                if self.mode=='foreign_action': body=body.replace('<form>','<form action="https://different.invalid/login">')
                route.fulfill(status=200,content_type='text/html; charset=utf-8',body=body); return
            body=MOCK.replace('Sentetik App','Başka App') if self.mode=='wrong_app' else MOCK
            route.fulfill(status=200,content_type='text/html; charset=utf-8',body=body); return
        route.fulfill(status=200,content_type='application/json',body='{}')


@skipUnless(os.name=='nt' and sync_playwright is not None,'Windows/Playwright')
class LoginBrowserTests(TempDataCase):
    @classmethod
    def setUpClass(cls):
        cls.pw=sync_playwright().start()
        cls.browser=cls.pw.chromium.launch(channel='chrome',headless=True,args=browser_test_args())

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop()

    def setUp(self):
        super().setUp(); self.profile=profile(); self.lines=[]
        self.context=self.browser.new_context(offline=True,service_workers='block'); self.page=self.context.new_page(); self.page.set_default_timeout(2000)
        self.reader=PanelReader(self.page,'Sentetik App',timeout_seconds=3,log=self.lines.append)
        self.auto=AutoLogin(self.profile,self.reader,self.lines.append,threading.Event())
        C.save(self.profile['id'],enabled=True,keep_logged_in=True,username=USER,password=PASSWORD)

    def tearDown(self):
        self.context.close(); super().tearDown()

    def setup_route(self,**kwargs):
        self.route=AuthRoute(**kwargs); self.context.route('**/*',self.route)

    def assert_no_secret_logs(self):
        self.assertNotIn(PASSWORD,'\n'.join(self.lines)); self.assertNotIn(USER,'\n'.join(self.lines))
        for path in store.profile_dir(self.profile['id']).rglob('*'):
            if path.is_file(): self.assertNotIn(PASSWORD,path.read_text(encoding='utf-8',errors='ignore'))

    def test_expired_initial_session_logs_in_once_and_validates_app(self):
        self.setup_route()
        runner._open_with_login(self.reader,self.auto)
        self.assertEqual(len(self.route.posts),1); self.assertTrue(self.route.posts[0]['keep'])
        self.reader.verify_app(); self.assertTrue(C.status(self.profile['id'])['ready'])
        with self.assertRaises(PanelError) as ctx: self.auto.recover()
        self.assertEqual(ctx.exception.code,'AUTO_LOGIN_LIMIT'); self.assert_no_secret_logs()

    def test_existing_session_never_loads_or_submits_password(self):
        self.setup_route(authenticated=True)
        with patch.object(C,'prepare',side_effect=AssertionError('Password must not be loaded')):
            runner._open_with_login(self.reader,self.auto)
        self.assertEqual(self.route.posts,[])

    def test_transient_login_route_resolves_without_loading_credentials(self):
        self.setup_route(mode='transient_login')
        with patch.object(C,'prepare',side_effect=AssertionError('Active session must not load credentials')):
            runner._open_with_login(self.reader,self.auto)
        self.reader.verify_app()
        self.assertEqual(self.route.posts,[])

    def test_redirect_to_dashboard_during_login_recovery_does_not_load_credentials(self):
        self.setup_route(mode='transient_login')
        self.page.goto('https://panel.netmera.com/')
        with patch.object(C,'prepare',side_effect=AssertionError('Active session must not load credentials')):
            self.auto.recover()
        self.reader.verify_app()
        self.assertEqual(self.route.posts,[])

    def test_event_and_people_navigation_wait_for_session_restoration(self):
        for path in ('/analytics/event-insight','/targeting/people'):
            with self.subTest(path=path):
                self.context.unroute('**/*')
                self.setup_route(authenticated=True,transient_path=path)
                runner._open_with_login(self.reader,self.auto)
                with patch.object(C,'prepare',side_effect=AssertionError('Active session must not load credentials')):
                    self.reader._goto(path)
                self.reader.verify_app()
                self.assertEqual(urlparse(self.page.url).path,path)
                self.assertEqual(self.route.posts,[])

    def test_missing_login_form_never_loads_credentials(self):
        self.context.route('**/*',lambda route:route.fulfill(status=200,body='<body>Loading</body>',content_type='text/html'))
        self.page.goto('https://panel.netmera.com/login')
        self.auto.profile={**self.profile,'timeout_seconds':1}
        with patch.object(C,'prepare',side_effect=AssertionError('No credentials without verified form')):
            with self.assertRaises(PanelError) as ctx:self.auto.recover()
        self.assertEqual(ctx.exception.code,'LOGIN_FORM_UNVERIFIED')

    def test_mid_job_expiry_retries_same_job_without_duplicate_rows(self):
        self.setup_route(authenticated=True,expires=1)
        runner._open_with_login(self.reader,self.auto)
        planned=datetime.now(ZoneInfo('Europe/Istanbul')); rows=[]
        runner._run_jobs(self.reader,self.profile,[J.normalize_job(event_job(event='Open App'))],planned,rows,self.lines.append,threading.Event(),auto_login=self.auto)
        self.assertEqual(len(rows),1); self.assertEqual(rows[0]['status'],'OK'); self.assertEqual(len(self.route.posts),1)
        self.assert_no_secret_logs()

    def test_second_expiry_stops_without_another_login_or_snapshot(self):
        self.setup_route(authenticated=True,expires=2)
        runner._open_with_login(self.reader,self.auto)
        rows=[]; planned=datetime.now(ZoneInfo('Europe/Istanbul'))
        jobs=[J.normalize_job(event_job(label=name,event='Open App')) for name in ('A','B')]
        runner._run_jobs(self.reader,self.profile,jobs,planned,rows,self.lines.append,threading.Event(),auto_login=self.auto)
        self.assertEqual([r['status'] for r in rows],['AUTO_LOGIN_LIMIT','AUTO_LOGIN_LIMIT'])
        self.assertEqual(len(self.route.posts),1)
        self.assertFalse((store.profile_dir(self.profile['id'])/'teshis').exists())

    def test_failure_blocks_future_runs_until_config_saved(self):
        self.setup_route(mode='failure')
        with self.assertRaises(PanelError) as ctx: runner._open_with_login(self.reader,self.auto)
        self.assertEqual(ctx.exception.code,'AUTO_LOGIN_FAILED')
        other=AutoLogin(self.profile,self.reader,self.lines.append)
        with self.assertRaises(PanelError) as ctx: other.recover()
        self.assertEqual(ctx.exception.code,'LOGIN_CONFIG_REQUIRED'); self.assertEqual(len(self.route.posts),1)
        self.assertEqual(self.page.locator('#password').input_value(),'')
        self.assert_no_secret_logs()

    def test_otp_and_foreign_action_do_not_get_automatic_fallback(self):
        for mode,code in (('otp','AUTO_LOGIN_ACTION_REQUIRED'),('foreign_action','LOGIN_FORM_UNVERIFIED')):
            with self.subTest(mode=mode):
                self.context.unroute('**/*'); self.setup_route(mode=mode)
                C.save(self.profile['id'],enabled=True,keep_logged_in=True,username=USER,password=PASSWORD)
                auto=AutoLogin(self.profile,self.reader,self.lines.append)
                with self.assertRaises(PanelError) as ctx: runner._open_with_login(self.reader,auto)
                self.assertEqual(ctx.exception.code,code)
                self.assertEqual(len(self.route.posts),1 if mode=='otp' else 0)
        self.assert_no_secret_logs()

    def test_wrong_app_is_fatal_after_login(self):
        self.setup_route(mode='wrong_app')
        with self.assertRaises(PanelError) as ctx: runner._open_with_login(self.reader,self.auto)
        self.assertEqual(ctx.exception.code,'APP_MISMATCH'); self.assertEqual(len(self.route.posts),1)

    def test_untrusted_origin_never_reads_credentials(self):
        self.context.route('**/*',lambda route:route.fulfill(status=200,body=LOGIN,content_type='text/html'))
        self.page.goto('https://different.invalid/login')
        self.assertFalse(is_login_page(self.page))
        with patch.object(C,'prepare',side_effect=AssertionError('No credentials at foreign origin')):
            with self.assertRaises(PanelError) as ctx: self.auto.recover()
        self.assertEqual(ctx.exception.code,'ORIGIN_MISMATCH')
        self.assertEqual(self.page.locator('#password').input_value(),'')

    def test_disabled_config_remains_manual_and_keep_login_can_be_off(self):
        self.setup_route()
        C.save(self.profile['id'],enabled=False,keep_logged_in=False,username=USER)
        with self.assertRaises(PanelError) as ctx: runner._open_with_login(self.reader,self.auto)
        self.assertEqual(ctx.exception.code,'LOGIN_REQUIRED'); self.assertEqual(self.route.posts,[])
        C.save(self.profile['id'],enabled=True,keep_logged_in=False,username=USER)
        runner._open_with_login(self.reader,AutoLogin(self.profile,self.reader,self.lines.append))
        self.assertFalse(self.route.posts[0]['keep'])

    def test_exception_call_log_with_password_is_redacted(self):
        self.setup_route()
        with self.assertRaises(PanelError): self.reader.open_panel()
        bad=Mock(); bad.fill.side_effect=RuntimeError(PASSWORD)
        controls={'username':Mock(),'password':bad,'remember':Mock(),'submit':Mock()}
        with patch.object(self.auto,'_form',return_value=controls):
            with self.assertRaises(PanelError) as ctx: self.auto.recover()
        self.assertEqual(ctx.exception.code,'AUTO_LOGIN_FAILED')
        self.assertNotIn(PASSWORD,ctx.exception.message()); self.assertTrue(ctx.exception.__suppress_context__)
        self.assert_no_secret_logs()

    def test_cancel_between_account_and_password_stops_submission(self):
        self.setup_route()
        with self.assertRaises(PanelError): self.reader.open_panel()
        controls={key:Mock() for key in ('username','password','remember','submit')}
        controls['username'].fill.side_effect=lambda _:self.auto.cancel.set()
        with patch.object(self.auto,'_form',return_value=controls):
            with self.assertRaises(PanelError) as ctx: self.auto.recover()
        self.assertEqual(ctx.exception.code,'CANCELLED')
        controls['password'].fill.assert_not_called(); controls['submit'].click.assert_not_called()
        self.assertEqual(self.route.posts,[])

    def test_navigation_during_fill_never_retargets_foreign_password_field(self):
        self.setup_route()
        original=self.route
        self.context.unroute('**/*')
        def route_handler(route):
            if urlparse(route.request.url).netloc=='different.invalid':
                route.fulfill(status=200,content_type='text/html',body=LOGIN)
            else:
                original(route)
        self.context.route('**/*',route_handler)
        with self.assertRaises(PanelError): self.reader.open_panel()
        self.page.locator('#username').evaluate("e => e.oninput = () => location.href='https://different.invalid/login'")
        with self.assertRaises(PanelError): self.auto.recover()
        self.page.wait_for_url('https://different.invalid/login')
        self.assertEqual(self.page.locator('#password').input_value(),'')
        self.assertEqual(self.route.posts,[])
