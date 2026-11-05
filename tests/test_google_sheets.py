"""Google HTTP çağrıları sahte; yalnız OAuth geri dönüşü localhost'ta sınanır."""
from copy import deepcopy
import base64
import hashlib
import json
import threading
import urllib.request
from urllib.parse import parse_qs,urlsplit,urlencode
from unittest.mock import patch
from netmera_automation import google_auth as A, google_sheets as G, store, jobs as J, runner
from tests.helpers import TempDataCase
from tests.test_cell_mapping import mapped_profile,result,batch

CLIENT={'installed':{'client_id':'synthetic.apps.googleusercontent.com','client_secret':'synthetic-client-secret',
                     'auth_uri':'https://accounts.google.com/o/oauth2/auth','token_uri':A.TOKEN_URL,'redirect_uris':['http://localhost']}}
TOKENS={'access_token':'synthetic-access','refresh_token':'synthetic-refresh','expires_in':3600,'scope':A.SCOPE,'token_type':'Bearer'}


class FakeGoogle:
    def __init__(self,p):
        self.p=p;self.calls=[];self.posts=[];self.formula=False;self.bad_ack=False;self.fail=False
        self.metadata={'spreadsheetId':p['sheets_id'],'sheets':[{'properties':{'sheetId':17,'title':'Rapor','gridProperties':{'rowCount':100,'columnCount':30}}}]}
    def __call__(self,url,**kw):
        self.calls.append((url,kw));parsed=urlsplit(url)
        assert parsed.netloc=='sheets.googleapis.com'
        assert parsed.path.startswith('/v4/spreadsheets/'+self.p['sheets_id'])
        if kw.get('method')=='POST':
            self.posts.append(kw['body'])
            if self.fail:raise A.GoogleError('Sentetik ağ hatası; onay yok')
            return {'spreadsheetId':'wrong' if self.bad_ack else self.p['sheets_id'],'replies':[{} for _ in kw['body']['requests']]}
        if 'values:batchGet' not in parsed.path:return deepcopy(self.metadata)
        query=parse_qs(parsed.query)
        if query['valueRenderOption']==['UNFORMATTED_VALUE']:
            return {'valueRanges':[{'values':[['Tarih'],['2026-10-16']]} for _ in query['ranges']]}
        return {'valueRanges':[{'values':[['=1+1']]} if self.formula else {} for _ in query['ranges']]}


class GoogleSheetsTests(TempDataCase):
    def test_failed_history_is_skipped_then_new_successes_are_written_once(self):
        p=self.profile();fake=FakeGoogle(p)
        bad1=batch(p,'old-failed',[result(p,None,status='FORMULA_INPUT_FAILED'),result(p,job_id='synthetic_dependency')])
        bad2=batch(p,'old-invalid',[result(p,None,status='FORMULA_CELL_INVALID')])
        good1=batch(p,'good-1',[result(p,23)]);good2=batch(p,'good-2',[result(p,29)])
        notes=[]
        with patch.object(A,'access_token',return_value='synthetic'),patch.object(A,'request_json',side_effect=fake):
            self.assertEqual(G.write(p,[bad1,bad2,good1,good2],notes),2)
            self.assertEqual(G.write(p,[bad1,bad2,good1,good2]),0)
        self.assertEqual(len(fake.posts),2)
        values=[post['requests'][0]['updateCells']['rows'][0]['values'][0]['userEnteredValue']['numberValue'] for post in fake.posts]
        self.assertEqual(values,[23,29])
        receipt=store.read_json(store.profile_dir(p['id'])/'google-yazim-kaydi.json')
        self.assertEqual((len(receipt['written']),len(receipt['skipped'])),(2,2))
        self.assertTrue(any('FORMULA_INPUT_FAILED' in note for note in notes))
        self.assertTrue(any('FORMULA_CELL_INVALID' in note for note in notes))

    def test_skipping_failed_history_does_not_record_later_network_failure_as_written(self):
        p=self.profile();fake=FakeGoogle(p);fake.fail=True
        bad=batch(p,'failed',[result(p,None,status='FORMULA_INPUT_FAILED')])
        good=batch(p,'good',[result(p,23)])
        with patch.object(A,'access_token',return_value='synthetic'),patch.object(A,'request_json',side_effect=fake):
            with self.assertRaises(A.GoogleError):G.write(p,[bad,good])
            receipt=store.read_json(store.profile_dir(p['id'])/'google-yazim-kaydi.json')
            self.assertEqual(receipt['written'],[]);self.assertEqual(len(receipt['skipped']),1)
            fake.fail=False
            self.assertEqual(G.write(p,[bad,good]),1)
        self.assertEqual(fake.posts[0],fake.posts[1])

    def profile(self,**mapping):
        p=mapped_profile(self.tmp,source='netmera',**mapping)
        p.update(output_target='google_sheets',sheets_id='synthetic_sheet_12345')
        return store.save_profile(p)

    def test_preview_resolves_each_daily_header_without_any_google_write(self):
        p=self.profile(mode='header_lookup',header_row=1,value_row=2)
        fake=FakeGoogle(p)
        def request(url,**kw):
            query=parse_qs(urlsplit(url).query)
            if 'values:batchGet' in url and query.get('valueRenderOption')==['UNFORMATTED_VALUE']:
                fake.calls.append((url,kw))
                return {'valueRanges':[{'values':[[f'{n}.10.2026' for n in range(10,15)]]}]}
            return fake(url,**kw)
        daily=[result(p,n,catch_up=True,period_start=f'2026-10-{n:02d}T00:00:00+03:00',
                      period_end=f'2026-10-{n+1:02d}T00:00:00+03:00') for n in range(10,15)]
        with patch.object(A,'access_token',return_value='synthetic'),patch.object(A,'request_json',side_effect=request):
            preview=G.preview(p,[],day_rows=daily)
        self.assertEqual((preview['ready'],preview['errors'],preview['written']),(5,0,False))
        self.assertEqual([i['cell'] for i in preview['items']],['A2','B2','C2','D2','E2'])
        self.assertEqual(fake.posts,[])

    def test_desktop_config_protection_status_clear_and_clone(self):
        """Tek Google ayarı: seçilen JSON GA4 ile ortak dosyaya yazılır; en son seçilen geçerlidir, projeye kopyalanmaz."""
        from netmera_automation import google_shared
        from netmera_automation.ga4 import google_hesap
        p=self.profile();info=A.save_client(p['id'],CLIENT)
        self.assertEqual((info['configured'],info['connected'],info['shared_client']),(True,False,True))
        self.assertFalse(A._path(p['id']).exists())  # projeye ayrı kayıt yok
        self.assertEqual(google_hesap.GoogleHesabi().istemci()['client_id'],CLIENT['installed']['client_id'])  # GA4 aynı dosyayı görür
        self.assertNotIn('client_id',json.dumps(info))
        clone=store.create_profile('Sentetik kopya',copy_from=p['id'])
        self.assertEqual(clone['sheets_id'],'');self.assertTrue(A.status(clone['id'])['configured'])  # ortak JSON her projede hazır
        newer={'installed':{**CLIENT['installed'],'client_id':'newer.apps.googleusercontent.com'}}
        A.save_client(clone['id'],newer)
        self.assertEqual(google_shared.shared_client()['client_id'],'newer.apps.googleusercontent.com')  # en son seçilen
        A.clear(p['id']);self.assertTrue(A.status(p['id'])['configured'])  # temizleme girişi kapatır, JSON kalır
    def test_invalid_clients_tokens_and_foreign_urls_rejected(self):
        p=self.profile()
        for client in ({'web':CLIENT['installed']},{'installed':{}},{'installed':{**CLIENT['installed'],'token_uri':'https://foreign.invalid/'}}):
            with self.assertRaises(A.GoogleError):A.save_client(p['id'],client)
        for change in ({'scope':'other'},{'scope':[]},{'token_type':[]},{'expires_in':True},{'access_token':'bad\r\ntoken'}):
            with self.assertRaises(A.GoogleError):A._accept_tokens({},dict(TOKENS,**change))
        for url in ('http://sheets.googleapis.com/','https://foreign.invalid/','https://sheets.googleapis.com@foreign.invalid/'):
            with patch('urllib.request.build_opener') as opened:
                with self.assertRaises(A.GoogleError):A.request_json(url,token='synthetic')
                opened.assert_not_called()
        with self.assertRaises(A.GoogleError):A.NoRedirect().redirect_request(None,None,None,None,None,None)

    def test_token_access_always_uses_shared_ga4_session(self):
        from netmera_automation import google_shared
        p=self.profile()
        A._save(p['id'], A._accept_tokens(A._client(CLIENT), TOKENS))
        with patch.object(google_shared, 'status', return_value={'connected':True,'sheets':True}), \
                patch.object(google_shared, 'token', return_value='shared-renewed') as token:
            self.assertEqual(A.access_token(p['id']), 'shared-renewed')
            token.assert_called_once()

    def test_oauth_loopback_state_pkce_and_no_cloud_or_password(self):
        """Google ile bağlan ortak girişi açar (PKCE/state/geri dönüş GA4 akışında, tests/ga4/test_google.py). Sheets bu girişle yazar."""
        from netmera_automation import google_shared
        p=self.profile();A.save_client(p['id'],CLIENT)
        with patch.object(google_shared,'connect',return_value='sentetik@example.invalid') as login:
            self.assertTrue(A.connect(p['id'],log=lambda _:None)['google_connected'])
        login.assert_called_once()
        shared={'client':True,'connected':True,'sheets':True,'email':'sentetik@example.invalid','expired':False}
        with patch.object(google_shared,'status',return_value=shared),patch.object(google_shared,'token',return_value='shared-access'):
            info=A.status(p['id'])
            self.assertEqual((info['connected'],info['shared'],info['account']),(True,True,'sentetik@example.invalid'))
            self.assertEqual(A.access_token(p['id']),'shared-access')
        with patch.object(google_shared,'status',return_value={**shared,'sheets':False}):
            self.assertFalse(A.status(p['id'])['connected'])  # Sheets izni olmayan giriş yazamaz
        A._save(p['id'],A._accept_tokens(A._client(CLIENT),TOKENS))  # projede eski ayrı bağlantı da var
        with patch.object(google_shared,'status',return_value=shared),patch.object(google_shared,'token',return_value='shared-access'):
            self.assertEqual((A.access_token(p['id']),A.status(p['id'])['shared']),('shared-access',True))  # ortak giriş önce gelir
        cancel=threading.Event();cancel.set()
        with patch.object(google_shared,'connect') as login:
            with self.assertRaises(A.GoogleError):A.connect(p['id'],cancel=cancel)
            login.assert_not_called()
    def test_org_internal_callback_explains_audience_fix(self):
        from netmera_automation.ga4.istemci import ClientError
        from netmera_automation import google_shared
        p=self.profile();A.save_client(p['id'],CLIENT)
        error=ClientError('org_internal','OAuth uygulaması yalnız kendi kuruluşuna açık (Dahili). Google Auth Platform → Kitle bölümünde Harici seçip hesabını test kullanıcısı olarak ekle.')
        with patch.object(google_shared,'connect',side_effect=error):
            with self.assertRaisesRegex(A.GoogleError,'Kitle.*Harici'):A.connect(p['id'],log=lambda _:None)
    def test_daily_directions_preview_and_write_are_same_absolute_cell(self):
        for rs,cs,row,col in ((1,0,5,1),(0,1,3,3),(1,1,5,3)):
            p=self.profile(mode='daily_shift',base_cell='B4',base_date='2026-10-14',row_step=rs,column_step=cs)
            fake=FakeGoogle(p)
            with patch.object(A,'access_token',return_value='synthetic'),patch.object(A,'request_json',side_effect=fake):
                plan=G.preview(p,[result(p)]);self.assertFalse(plan['written']);self.assertEqual(fake.posts,[])
                self.assertEqual(G.write(p,[batch(p)]),1);self.assertEqual(G.write(p,[batch(p)]),0)
                self.assertEqual(G.write(p,[batch(p,'two',[result(p,19)])]),1)
            for post in fake.posts:
                update=post['requests'][0]['updateCells']
                self.assertEqual(update['range'],dict(sheetId=17,startRowIndex=row,endRowIndex=row+1,startColumnIndex=col,endColumnIndex=col+1))
                self.assertEqual(update['fields'],'userEnteredValue')
            self.assertEqual(fake.posts[-1]['requests'][0]['updateCells']['rows'][0]['values'][0]['userEnteredValue'],{'numberValue':19})

    def test_date_lookup_reads_only_date_column_and_targets(self):
        p=self.profile(mode='date_row',date_column='A',value_column='B');fake=FakeGoogle(p)
        with patch.object(A,'access_token',return_value='synthetic'),patch.object(A,'request_json',side_effect=fake):
            plan=G.preview(p,[result(p)]);self.assertEqual(plan['items'][0]['cell'],'B2');self.assertEqual(G.write(p,[batch(p)]),1)
        ranges=[r for url,_ in fake.calls for r in parse_qs(urlsplit(url).query).get('ranges',[])]
        self.assertEqual(set(ranges),{"'Rapor'!A1:A100","'Rapor'!B2"})

    def test_formula_protection_merge_grid_and_stale_query_block_posts(self):
        for kind in ('formula','protected','merged','grid','stale'):
            p=self.profile();fake=FakeGoogle(p);sheet=fake.metadata['sheets'][0]
            if kind=='formula':fake.formula=True
            elif kind=='protected':sheet['protectedRanges']=[{'range':{'sheetId':17}}]
            elif kind=='merged':sheet['merges']=[{'startRowIndex':3,'endRowIndex':4,'startColumnIndex':1,'endColumnIndex':3}]
            elif kind=='grid':sheet['properties']['gridProperties']['rowCount']=3
            rows=[result(p,signature='old')] if kind=='stale' else [result(p)]
            with patch.object(A,'access_token',return_value='synthetic'),patch.object(A,'request_json',side_effect=fake):
                with self.subTest(kind=kind),self.assertRaises(A.GoogleError):G.write(p,[batch(p,kind,rows)])
            self.assertEqual(fake.posts,[])

    def test_demo_never_connects_and_uncertain_write_never_records_success(self):
        p=self.profile();p['source']='demo'
        with patch.object(A,'access_token') as token,patch.object(A,'request_json') as request:
            with self.assertRaises(A.GoogleError):G.write(p,[batch(p)])
            token.assert_not_called();request.assert_not_called()
        p['source']='netmera';fake=FakeGoogle(p)
        for kind in ('fail','bad_ack'):
            fake.fail=kind=='fail';fake.bad_ack=kind=='bad_ack'
            with patch.object(A,'access_token',return_value='synthetic'),patch.object(A,'request_json',side_effect=fake):
                with self.assertRaises(A.GoogleError):G.write(p,[batch(p)])
            self.assertFalse((store.profile_dir(p['id'])/'google-yazim-kaydi.json').exists())

    def test_sheet_url_extraction_and_rejection(self):
        p=self.profile();p['sheets_id']='https://docs.google.com/spreadsheets/d/synthetic_sheet_12345/edit#gid=0'
        self.assertEqual(J.normalize_profile(p)['sheets_id'],'synthetic_sheet_12345')
        p['sheets_id']='https://foreign.invalid/spreadsheets/d/synthetic_sheet_12345/edit'
        with self.assertRaises(J.JobError):J.normalize_profile(p)

    def test_runner_failed_cloud_write_keeps_batch_for_same_cell_retry(self):
        p=self.profile();fake=FakeGoogle(p);fake.fail=True
        with patch.object(A,'status',return_value={'connected':True}),patch.object(A,'access_token',return_value='synthetic'),patch.object(A,'request_json',side_effect=fake),patch.object(runner,'Browser'),patch.object(runner,'PanelReader') as reader,patch.object(runner,'_open_with_login'):
            reader.return_value.fetch.return_value=({'All Total':23},None)
            reader.return_value.fetch_many.side_effect=lambda jobs,first,last,per_day=False:{j['id']:{'All Total':23} for j in jobs}
            summary=runner.run_profile(p,log=lambda _:None)
            self.assertEqual(summary['ok'],1);self.assertFalse(summary['excel_written'])
            self.assertEqual(len(store.excel_batches(p)),1)
            fake.fail=False
            self.assertEqual(runner.rebuild_excel(p),1)
            self.assertEqual(runner.rebuild_excel(p),0)
        self.assertEqual(len(fake.posts),2)
        self.assertEqual(fake.posts[0],fake.posts[1])

    def test_google_error_codes_become_actionable_messages_without_raw_body(self):
        import io,urllib.error
        cases=((403,{'error':{'status':'PERMISSION_DENIED','details':[{'reason':'SERVICE_DISABLED'}],'message':'synthetic raw text'}},'Sheets API'),
               (400,{'error':'invalid_grant','error_description':'synthetic raw text'},'süresi doldu'),
               (403,{'error':{'status':'PERMISSION_DENIED','message':'synthetic raw text'}},'Düzenleyen'),
               (404,b'not json','bulunamadı'))
        for code,body,expected in cases:
            raw=body if isinstance(body,bytes) else json.dumps(body).encode()
            error=urllib.error.HTTPError('https://sheets.googleapis.com/v4/spreadsheets/x',code,'x',{},io.BytesIO(raw))
            with self.subTest(code=code,expected=expected),patch('urllib.request.OpenerDirector.open',side_effect=error):
                with self.assertRaises(A.GoogleError) as caught:A.request_json('https://sheets.googleapis.com/v4/spreadsheets/x',token='synthetic')
                self.assertIn(expected,str(caught.exception));self.assertNotIn('synthetic raw text',str(caught.exception))

    def test_expired_shared_account_does_not_use_old_profile_account(self):
        from netmera_automation import google_shared
        p=self.profile();A.save_client(p['id'],CLIENT)
        A._save(p['id'], A._accept_tokens(A._client(CLIENT), TOKENS))
        state={'client':True,'connected':False,'sheets':False,'expired':True}
        with patch.object(google_shared,'status',return_value=state):
            with self.assertRaisesRegex(A.GoogleError,'süresi doldu'):A.access_token(p['id'])
            info=A.status(p['id'])
            self.assertTrue(info['configured']);self.assertFalse(info['connected']);self.assertTrue(info['expired'])

    def test_check_reads_title_and_reports_missing_mapped_sheet_without_writing(self):
        p=self.profile();p['excel_mappings'][0]['sheet']='Ozet';fake=FakeGoogle(p)
        fake.metadata['properties']={'title':'Sentetik rapor'}
        with patch.object(A,'access_token',return_value='synthetic'),patch.object(A,'request_json',side_effect=fake):
            info=G.check(p)
        self.assertEqual((info['title'],info['sheets'],info['missing_sheets']),('Sentetik rapor',['Rapor'],['Ozet']))
        self.assertEqual(fake.posts,[]);self.assertIn('fields=',fake.calls[0][0])
        p['source']='demo'
        with patch.object(A,'access_token') as token:
            with self.assertRaises(A.GoogleError):G.check(p)
            token.assert_not_called()

    def test_downloads_detection_picks_newest_client_file_only_by_name(self):
        import os,tempfile,time
        from pathlib import Path
        p=self.profile()
        with tempfile.TemporaryDirectory() as folder,patch.object(A,'downloads_dir',return_value=Path(folder)):
            self.assertIsNone(A.find_download())
            old=Path(folder)/'client_secret_old.apps.googleusercontent.com.json';new=Path(folder)/'client_secret_new.apps.googleusercontent.com (1).json'
            for path in (old,new):path.write_text(json.dumps(CLIENT),encoding='utf-8')
            (Path(folder)/'baska.json').write_text('{}');os.utime(old,(time.time()-3600,)*2)
            self.assertEqual(A.find_download()['name'],new.name)
            os.utime(new,(time.time()-40*86400,)*2);self.assertEqual(A.find_download()['name'],old.name)
            self.assertTrue(A.save_client_from_download(p['id'],old.name)['configured'])
            for bad in ('../client_secret_x.json','baska.json',None):
                with self.assertRaises(A.GoogleError):A.save_client_from_download(p['id'],bad)

    def test_team_client_lets_each_user_connect_own_account_without_json(self):
        from netmera_automation import google_shared
        owner=self.profile();A.save_client(owner['id'],CLIENT)
        self.assertFalse(A.status(owner['id'])['team'])
        A.save_team(owner['id'])
        saved=json.loads(A.TEAM_FILE.read_text(encoding='utf-8'))
        self.assertEqual(saved['installed']['client_id'],CLIENT['installed']['client_id'])
        self.assertNotIn('refresh',A.TEAM_FILE.read_text(encoding='utf-8'))
        # Başka bilgisayar: ortak JSON yok, ekip dosyası var → dosya seçmeden bağlanılır; ortak JSON ekip dosyasından oluşur.
        google_shared.client_path().unlink()
        colleague=store.create_profile('Sentetik ekip arkadaşı',source='netmera')
        info=A.status(colleague['id'])
        self.assertEqual((info['configured'],info['team'],info['own_client'],info['connected']),(True,True,False,False))
        with patch.object(google_shared,'connect',return_value=None):
            A.connect(colleague['id'],log=lambda _:None)
        self.assertEqual(google_shared.shared_client()['client_id'],CLIENT['installed']['client_id'])
        A.TEAM_FILE.write_text('{"web":{}}',encoding='utf-8')
        google_shared.client_path().unlink()
        self.assertIn('ekip istemcisi',A.status(store.create_profile('Sentetik üçüncü',source='netmera')['id'])['message'].lower())
    def test_header_lookup_reads_computed_header_row_from_google(self):
        from datetime import date as D
        serial=lambda d:(d-D(1899,12,30)).days
        p=self.profile(mode='header_lookup',header_row=13,value_row=14,date_basis='previous_day',month_cell='B13')
        fake=FakeGoogle(p);seen=[]
        header=['', 'Ekim DemoCampaign']+[serial(D(2026,10,1+i)) for i in range(31)]
        def google(url,**kw):
            parsed=urlsplit(url);query=parse_qs(parsed.query)
            if 'values:batchGet' in parsed.path and query.get('valueRenderOption')==['UNFORMATTED_VALUE']:
                seen.extend(query['ranges'])
                return {'valueRanges':[{'values':[header]} if r.endswith('13:13') else {'values':[['Ekim DemoCampaign']]} for r in query['ranges']]}
            return fake(url,**kw)
        people=result(p,job_type='people',period_start=None,period_end=None,planned_at='2026-10-18T09:00:00+03:00')
        with patch.object(A,'access_token',return_value='synthetic'),patch.object(A,'request_json',side_effect=google):
            plan=G.preview(p,[people])
            self.assertEqual((plan['items'][0]['cell'],plan['ready']),('S14',1))  # C=1 Ekim → S=17 Ekim
            self.assertEqual(G.write(p,[batch(p,'h',[people])]),1)
        self.assertIn("'Rapor'!13:13",seen)
        update=fake.posts[-1]['requests'][0]['updateCells']['range']
        self.assertEqual((update['startRowIndex'],update['startColumnIndex']),(13,18))

    def test_missing_google_connection_stops_before_netmera_browser(self):
        p=self.profile()
        with patch.object(A,'status',return_value={'connected':False}),patch.object(runner,'Browser') as browser:
            with self.assertRaises(runner.RunError):runner.run_profile(p,log=lambda _:None)
            browser.assert_not_called()
