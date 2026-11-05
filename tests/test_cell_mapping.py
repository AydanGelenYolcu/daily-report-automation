"""Sentetik mevcut dosyalar; şirket raporu veya ağa erişim yok."""
from copy import deepcopy
from datetime import date, timedelta
from openpyxl import Workbook, load_workbook
from openpyxl.styles import PatternFill
from netmera_automation import jobs as J, store, cell_mapping as M
from tests.helpers import TempDataCase, event_job
from tests.test_run_and_excel import demo_profile


def mapped_profile(tmp, source='demo', **mapping):
    store.ensure_defaults()
    p=store.load_profile('demo-sahte-veri')
    p.update(source='demo',jobs=[event_job()],excel_mode='mapped_cells',excel_mappings=[],output_target='excel',output_file=str(tmp/'cikti'/'demo.xlsx'))
    p=store.save_profile(p)
    if source!='demo':p['source']=source;p['netmera_app']='Sentetik'
    p['excel_mappings']=[dict(job_id=p['jobs'][0]['id'],column='All Total',sheet='Rapor',mode='fixed_cell',cell='B4')]
    p['excel_mappings'][0].update(mapping)
    # Remove fields belonging to a different mode for a realistic saved UI shape.
    if p['excel_mappings'][0]['mode']!='fixed_cell':p['excel_mappings'][0].pop('cell')
    return store.save_profile(p)


def result(p, value=7, **changes):
    job=p['jobs'][0]
    row=dict(source=p['source'],job_id=job['id'],job_type='event',job_label=job['label'],
             column='All Total',signature=J.signature(job),value=value,status='OK',
             period_start='2026-10-16T00:00:00+03:00',period_end='2026-10-17T00:00:00+03:00',
             planned_at='2026-10-17T09:00:00+03:00',captured_at='2026-10-17T09:01:00+03:00')
    row.update(changes);return row


def batch(p, key='one', rows=None):
    return dict(id=key,source=p['source'],planned_at='2026-10-17T09:00:00+03:00',
                mapping_signature=M.mapping_signature(p),rows=rows if rows is not None else [result(p)])


class CellMappingTests(TempDataCase):
    def book(self,p,prepare=None):
        from pathlib import Path
        path=Path(p['output_file']);path.parent.mkdir(parents=True,exist_ok=True)
        b=Workbook();b.active.title='Rapor';b.active['A1']='El ile not'
        b.active['D4']='=SUM(D1:D3)';b.active['B4'].fill=PatternFill('solid',fgColor='FF0000')
        if prepare:prepare(b.active)
        b.save(path);b.close();return path

    def test_old_profile_defaults_and_mapping_validation(self):
        p=demo_profile(self.tmp,jobs=[event_job()])
        self.assertEqual(p['output_target'],'excel');self.assertEqual(p['excel_mappings'],[])
        p=mapped_profile(self.tmp)
        for change in ({'cell':'XFE1'},{'cell':'A0'},{'sheet':'_NetmeraKayit'},
                       {'mode':'daily_shift','base_cell':'B4','base_date':'bad','row_step':1,'column_step':0}):
            bad=deepcopy(p);bad['excel_mappings'][0].update(change)
            with self.subTest(change=change),self.assertRaises(J.JobError):J.normalize_profile(bad)

    def test_daily_directions_gaps_basis_and_retry_have_absolute_targets(self):
        for rs,cs,want in ((1,0,'B6'),(0,1,'D4'),(1,1,'D6')):
            p=mapped_profile(self.tmp,mode='daily_shift',base_cell='B4',base_date='2026-10-14',row_step=rs,column_step=cs)
            m=p['excel_mappings'][0];row=result(p)
            self.assertEqual(M.shifted_cell(m,row),want)
            self.assertEqual(M.shifted_cell(m,row),want)
            m['date_basis']='run_day'
            self.assertEqual(M.shifted_cell(m,row),{(1,0):'B7',(0,1):'E4',(1,1):'E7'}[(rs,cs)])

    def test_shift_rejects_negative_bounds_and_multiday_data(self):
        p=mapped_profile(self.tmp,mode='daily_shift',base_cell='XFD1048576',base_date='2026-10-15',row_step=1,column_step=1)
        with self.assertRaises(ValueError):M.shifted_cell(p['excel_mappings'][0],result(p))
        p['excel_mappings'][0].update(base_cell='B4',base_date='2026-10-18')
        with self.assertRaises(ValueError):M.shifted_cell(p['excel_mappings'][0],result(p))
        p['excel_mappings'][0]['base_date']='2026-10-14'
        with self.assertRaises(ValueError):M.shifted_cell(p['excel_mappings'][0],result(p,period_end='2026-10-18T00:00:00+03:00'))

    def test_preview_does_not_write_and_actual_write_preserves_style_backup_ledger(self):
        p=mapped_profile(self.tmp);path=self.book(p);original=path.read_bytes()
        plan=M.preview(p,[result(p)])
        self.assertEqual((plan['ready'],plan['written']),(1,False));self.assertEqual(path.read_bytes(),original)
        self.assertEqual(M.write_mapped_workbook(p,[batch(p)]),1)
        self.assertEqual(M.write_mapped_workbook(p,[batch(p)]),0)
        b=load_workbook(path);self.assertEqual(b['Rapor']['B4'].value,7)
        self.assertEqual(b['Rapor']['D4'].value,'=SUM(D1:D3)')
        self.assertEqual(b['Rapor']['B4'].fill.fgColor.rgb,'00FF0000');b.close()
        self.assertEqual(path.with_name(path.stem+'.netmera-yedek.xlsx').read_bytes(),original)

    def test_new_run_same_report_day_updates_same_cell(self):
        p=mapped_profile(self.tmp,mode='daily_shift',base_cell='B4',base_date='2026-10-16',row_step=0,column_step=1)
        path=self.book(p)
        M.write_mapped_workbook(p,[batch(p),batch(p,'two',[result(p,19)])])
        b=load_workbook(path);self.assertEqual(b['Rapor']['B4'].value,19);self.assertIsNone(b['Rapor']['C4'].value);b.close()

    def test_old_failed_mapping_does_not_block_new_successful_batch(self):
        p=mapped_profile(self.tmp);path=self.book(p)
        bad=batch(p,'old-failed',[result(p,None,status='FORMULA_INPUT_FAILED'),result(p,job_id='synthetic_dependency')])
        good=batch(p,'new-success',[result(p,23)])
        notes=[]
        self.assertEqual(M.write_mapped_workbook(p,[bad,good],notes),1)
        book=load_workbook(path)
        try:
            self.assertEqual(book['Rapor']['B4'].value,23)
            keys=[r[0] for r in book[M.LEDGER_SHEET].iter_rows(min_row=3,max_col=1,values_only=True)]
            self.assertEqual(sum(k.startswith('s:') for k in keys),1)
        finally:book.close()
        self.assertTrue(any('FORMULA_INPUT_FAILED' in note for note in notes))
        self.assertEqual(M.write_mapped_workbook(p,[bad,good]),0)

    def test_failed_only_mapping_is_skipped_without_writing_zero(self):
        p=mapped_profile(self.tmp);path=self.book(p)
        self.assertEqual(M.write_mapped_workbook(p,[batch(p,'failed',[result(p,None,status='FORMULA_CELL_INVALID')])]),0)
        book=load_workbook(path)
        try:self.assertIsNone(book['Rapor']['B4'].value)
        finally:book.close()

    def test_preview_includes_five_daily_results_without_changing_workbook(self):
        p=mapped_profile(self.tmp,mode='header_lookup',header_row=1,value_row=2)
        first=date(2026,10,10)
        def prepare(sheet):
            for n in range(6):sheet.cell(1,n+1,first+timedelta(days=n))
        path=self.book(p,prepare);original=path.read_bytes()
        daily=[result(p,n+1,catch_up=True,period_start=(first+timedelta(days=n)).isoformat()+'T00:00:00+03:00',
                      period_end=(first+timedelta(days=n+1)).isoformat()+'T00:00:00+03:00') for n in range(5)]
        plan=M.preview(p,[],day_rows=daily)
        self.assertEqual((plan['ready'],plan['errors'],plan['written']),(5,0,False))
        self.assertEqual([i['cell'] for i in plan['items']],['A2','B2','C2','D2','E2'])
        self.assertEqual([i['period'] for i in plan['items']],[(first+timedelta(days=n)).isoformat() for n in range(5)])
        self.assertEqual(path.read_bytes(),original)
        main=result(p,9,period_start='2026-10-15T00:00:00+03:00',period_end='2026-10-16T00:00:00+03:00')
        self.assertEqual(M.preview(p,[main],day_rows=daily)['ready'],6)
        self.assertEqual(path.read_bytes(),original)

    def test_daily_preview_preserves_filled_cells_errors_and_duplicate_result_checks(self):
        p=mapped_profile(self.tmp,mode='date_row',date_column='A',value_column='B')
        def prepare(sheet):
            sheet['A2']=date(2026,10,16);sheet['B2']=12
            sheet['A3']=date(2026,10,17)
        path=self.book(p,prepare);original=path.read_bytes()
        row=result(p,catch_up=True)
        failed=result(p,catch_up=True,status='ERROR',period_start='2026-10-17T00:00:00+03:00',period_end='2026-10-18T00:00:00+03:00')
        plan=M.preview(p,[],day_rows=[row,failed])
        self.assertEqual(plan['ready'],0)
        self.assertEqual([i['status'] for i in plan['items']],['skipped','skipped'])
        self.assertIn('hücre dolu',plan['items'][0]['message'])
        self.assertIn('Sorgu hatalı',plan['items'][1]['message'])
        duplicate=M.preview(p,[],day_rows=[row,row])
        self.assertEqual(duplicate['errors'],1)
        self.assertIn('tekil değil',duplicate['items'][0]['message'])
        self.assertEqual(path.read_bytes(),original)

    def test_date_row_find_and_ambiguous_date_do_not_guess(self):
        p=mapped_profile(self.tmp,mode='date_row',date_column='A',value_column='B')
        path=self.book(p,lambda s:setattr(s['A5'],'value',date(2026,10,16)))
        self.assertEqual(M.preview(p,[result(p)])['items'][0]['cell'],'B5')
        b=load_workbook(path);b['Rapor']['A7']='16.10.2026';b.save(path);b.close();original=path.read_bytes()
        with self.assertRaises(ValueError):M.write_mapped_workbook(p,[batch(p)])
        self.assertEqual(path.read_bytes(),original)

    def test_formula_merged_protected_missing_and_query_mismatch_preserve_file(self):
        p=mapped_profile(self.tmp)
        preparations=[lambda s:setattr(s['B4'],'value','=1+1'),lambda s:s.merge_cells('B4:C4'),
                      lambda s:setattr(s.protection,'sheet',True),lambda s:setattr(s,'title','Başka')]
        for prepare in preparations:
            path=self.book(p,prepare);original=path.read_bytes()
            with self.assertRaises(ValueError):M.write_mapped_workbook(p,[batch(p)])
            self.assertEqual(path.read_bytes(),original)
        path=self.book(p);original=path.read_bytes()
        for changes in ({'signature':'old'},{'source':'netmera'},{'value':True},{'value':9007199254740992}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):M.write_mapped_workbook(p,[batch(p,rows=[result(p,**changes)])])
            self.assertEqual(path.read_bytes(),original)

    def test_failed_result_is_skipped_and_whole_local_batch_stages_before_commit(self):
        p=mapped_profile(self.tmp);path=self.book(p);original=path.read_bytes()
        self.assertEqual(M.preview(p,[result(p,status='ERROR')])['items'][0]['status'],'skipped')
        with self.assertRaises(ValueError):M.write_mapped_workbook(p,[batch(p),batch(p,'bad',[result(p,signature='old')])])
        self.assertEqual(path.read_bytes(),original)

    def test_pending_queue_is_separate_by_provider_document_mapping_and_query(self):
        p=mapped_profile(self.tmp);store.append_excel_batch(p,batch(p))
        self.assertEqual(len(store.excel_batches(p)),1)
        for change in ('provider','path','mapping','query'):
            other=deepcopy(p)
            if change=='provider':other.update(output_target='google_sheets',sheets_id='synthetic_sheet_12345')
            elif change=='path':other['output_file']+='new'
            elif change=='mapping':other['excel_mappings'][0]['cell']='C5'
            else:other['jobs'][0]['event']='Another Synthetic Event'
            self.assertEqual(store.excel_batches(other),[])
        p.update(output_target='google_sheets',sheets_id='synthetic_sheet_12345')
        store.append_excel_batch(p,batch(p,'google'))
        self.assertEqual(len(store.excel_batches(p)),1)
        p['sheets_id']='another_synthetic_12345';self.assertEqual(store.excel_batches(p),[])

    def test_resolved_dynamic_collisions_and_date_column_write_are_blocked(self):
        p=mapped_profile(self.tmp,mode='daily_shift',base_cell='B4',base_date='2026-10-16',row_step=1,column_step=0)
        job=J.normalize_job(event_job(label='İkinci sentetik',event='Başka sentetik'))
        p['jobs'].append(job)
        p['excel_mappings'].append(dict(job_id=job['id'],column='All Total',sheet='Rapor',mode='fixed_cell',cell='B4'))
        p=J.normalize_profile(p);self.book(p)
        second={**result(p),'job_id':job['id'],'signature':J.signature(job)}
        with self.assertRaisesRegex(ValueError,'çakışıyor'):M.preview(p,[result(p),second])
        p['excel_mappings'][1]=dict(job_id=job['id'],column='All Total',sheet='Rapor',mode='date_row',date_column='B',value_column='C',date_basis='data_day')
        p=J.normalize_profile(p)
        with self.assertRaisesRegex(ValueError,'tarih arama'):M.preview(p,[result(p)])


class HeaderLookupTests(TempDataCase):
    """Yatay tarih başlıklı blok raporu (sarı giriş satırları) — sentetik düzen."""
    def report(self,p,headers,extra=None):
        from pathlib import Path
        path=Path(p['output_file']);path.parent.mkdir(parents=True,exist_ok=True)
        b=Workbook();ws=b.active;ws.title='Rapor'
        for col,value in headers.items():ws[col]=value
        ws['B15']='=SUM(C14:AG14)'
        if extra:extra(ws)
        b.save(path);b.close();return path

    def october(self,first_col=3,days=31):
        from openpyxl.utils import get_column_letter as L
        return {f'{L(first_col+i)}13':date(2026,10,1+i) for i in range(days)}

    def test_header_date_finds_yesterday_column_and_writes_only_there(self):
        p=mapped_profile(self.tmp,mode='header_lookup',header_row=13,value_row=14)
        self.assertEqual(p['excel_mappings'][0]['month_cell'],None)
        path=self.report(p,self.october())
        plan=M.preview(p,[result(p)])
        self.assertEqual((plan['items'][0]['cell'],plan['ready']),('R14',1))  # C=1 Ekim → R=16 Ekim
        self.assertEqual(M.write_mapped_workbook(p,[batch(p)]),1)
        b=load_workbook(path);self.assertEqual(b['Rapor']['R14'].value,7);self.assertEqual(b['Rapor']['B15'].value,'=SUM(C14:AG14)')

    def test_previous_day_basis_puts_morning_snapshot_in_yesterdays_column(self):
        p=mapped_profile(self.tmp,mode='header_lookup',header_row=13,value_row=14,date_basis='previous_day')
        self.report(p,self.october())
        people=result(p,job_type='people',period_start=None,period_end=None,
                      planned_at='2026-10-18T09:00:00+03:00',captured_at='2026-10-18T09:02:00+03:00')
        self.assertEqual(M.preview(p,[people])['items'][0]['cell'],'S14')  # 17 Ekim
        p['excel_mappings'][0]['date_basis']='data_day'
        self.assertEqual(M.preview(store.save_profile(p),[people])['items'][0]['cell'],'T14')  # çekim günü 18 Ekim

    def test_overflow_day_of_next_month_and_stale_header_are_not_written(self):
        p=mapped_profile(self.tmp,mode='header_lookup',header_row=13,value_row=14)
        nov={**{f'{chr(67+i) if i<24 else "A"+chr(67+i-26)}13':date(2026,11,1+i) for i in range(30)},'AG13':date(2026,12,1)}
        self.report(p,nov)
        dec=result(p,period_start='2026-12-01T00:00:00+03:00',period_end='2026-12-02T00:00:00+03:00',planned_at='2026-12-02T09:00:00+03:00')
        item=M.preview(p,[dec])['items'][0]
        self.assertEqual(item['status'],'error');self.assertIn('taşan',item['message'])
        item=M.preview(p,[result(p)])['items'][0]  # başlık Kasım'da, veri 16 Ekim → bulunamaz
        self.assertEqual(item['status'],'error');self.assertIn('başlık tarihini güncelleyin',item['message'])

    def test_day_number_header_with_month_label_check(self):
        from openpyxl.utils import get_column_letter as L
        p=mapped_profile(self.tmp,mode='header_lookup',header_row=80,value_row=82,month_cell='a82')
        self.assertEqual(p['excel_mappings'][0]['month_cell'],'A82')
        headers={f'{L(1+d)}80':d for d in range(1,32)}
        self.report(p,{**headers,'A81':'Eylül','A82':'Ekim'})
        self.assertEqual(M.preview(p,[result(p)])['items'][0]['cell'],'Q82')  # B=1 → Q=16
        self.report(p,{**headers,'A81':'Ekim','A82':'Kasım'})
        item=M.preview(p,[result(p)])['items'][0]
        self.assertEqual(item['status'],'error');self.assertIn('Ekim',item['message'])

    def test_formula_headers_need_saved_values_in_local_excel(self):
        from openpyxl.utils import get_column_letter as L
        p=mapped_profile(self.tmp,mode='header_lookup',header_row=13,value_row=14)
        headers={'C13':date(2026,10,1),**{f'{L(c)}13':f'={L(c-1)}13+1' for c in range(4,34)}}
        self.report(p,headers)
        item=M.preview(p,[result(p)])['items'][0]
        self.assertEqual(item['status'],'error');self.assertIn('Excel’de açıp kaydedin',item['message'])

    def test_same_result_to_two_targets_and_header_row_protection(self):
        p=mapped_profile(self.tmp,mode='header_lookup',header_row=13,value_row=14)
        job=p['jobs'][0]['id']
        p['excel_mappings'].append(dict(job_id=job,column='All Total',sheet='Rapor',mode='fixed_cell',cell='B4'))
        p=store.save_profile(p);path=self.report(p,self.october())
        self.assertEqual(M.write_mapped_workbook(p,[batch(p)]),1)
        b=load_workbook(path);self.assertEqual((b['Rapor']['R14'].value,b['Rapor']['B4'].value),(7,7))
        for extra in (dict(job_id=job,column='All Total',sheet='Rapor',mode='header_lookup',header_row=14,value_row=20,date_basis='data_day',month_cell=None),
                      dict(p['excel_mappings'][0])):
            bad=deepcopy(p);bad['excel_mappings'].append(extra)
            with self.subTest(extra=extra),self.assertRaises(J.JobError):J.normalize_profile(bad)
        for change in ({'header_row':14},{'header_row':'13'},{'month_cell':'82'},{'date_basis':'tomorrow'}):
            bad=deepcopy(p);bad['excel_mappings'][0].update(change)
            with self.subTest(change=change),self.assertRaises(J.JobError):J.normalize_profile(bad)

    def test_text_headers_american_option_and_date_row_text_dates(self):
        from openpyxl.utils import get_column_letter as L
        p=mapped_profile(self.tmp,mode='header_lookup',header_row=13,value_row=14)
        self.report(p,{f'{L(2+d)}13':f'{d} Ekim' for d in range(1,32)})          # metin, yılsız
        self.assertEqual(M.preview(p,[result(p)])['items'][0]['cell'],'R14')
        self.report(p,{f'{L(2+d)}13':str(d) for d in range(1,32)})               # metin gün numarası
        self.assertEqual(M.preview(p,[result(p)])['items'][0]['cell'],'R14')
        self.report(p,{f'{L(2+d)}13':f'10/{d:02d}/2026' for d in range(1,13)})   # yalnız belirsiz Amerikan
        self.assertEqual(M.preview(p,[result(p)])['items'][0]['status'],'error')  # 16 Ekim yok (gün/ay sanılır)
        p['excel_mappings'][0]['month_first']=True;p=store.save_profile(p)
        self.assertTrue(p['excel_mappings'][0]['month_first'])
        self.report(p,{f'{L(2+d)}13':f'10/{d:02d}/2026' for d in range(1,32)})
        self.assertEqual(M.preview(p,[result(p)])['items'][0]['cell'],'R14')
        q=mapped_profile(self.tmp,mode='date_row',date_column='A',value_column='B')
        self.report(q,{f'A{r}':f'2026/10/{r:02d}' for r in range(1,32)})
        self.assertEqual(M.preview(q,[result(q)])['items'][0]['cell'],'B16')
