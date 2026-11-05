"""Kaçan günleri tamamlama: sentetik okuyucu ve yerel Excel; Netmera'ya bağlanmaz."""
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from openpyxl import Workbook, load_workbook
from openpyxl.utils import get_column_letter as L

from netmera_automation import runner, store, notify
from tests.helpers import TempDataCase, event_job
from tests.test_cell_mapping import mapped_profile, result

MONDAY = datetime(2026, 10, 19, 9, 0, tzinfo=ZoneInfo('Europe/Istanbul'))


class CatchUpTests(TempDataCase):
    def profile(self, **changes):
        p = mapped_profile(self.tmp, mode='header_lookup', header_row=13, value_row=14)
        p.update(changes)
        return store.save_profile(p)

    def seed(self, p, day):
        """Uygulamanın en son başarıyla çektiği gün (Perşembe)."""
        store.append_results(p['id'], [result(p, period_start=f'{day}T00:00:00+03:00',
                                              period_end=f'{date.fromisoformat(day).replace(day=date.fromisoformat(day).day + 1)}T00:00:00+03:00')])

    def report(self, p, prefill=None):
        path = Path(p['output_file']); path.parent.mkdir(parents=True, exist_ok=True)
        b = Workbook(); ws = b.active; ws.title = 'Rapor'
        for i in range(31):
            ws[f'{L(3 + i)}13'] = date(2026, 10, 1 + i)
        for cell, value in (prefill or {}).items():
            ws[cell] = value
        b.save(path); b.close(); return path

    def test_plan_picks_days_after_last_success_up_to_limit_and_skips_yesterday(self):
        p = self.profile(); self.seed(p, '2026-10-15')
        jobs = p['jobs']
        self.assertEqual(list(runner.catch_up_plan(p, jobs, MONDAY)), [date(2026, 10, 16), date(2026, 10, 17)])
        p['catch_up_days'] = 1
        self.assertEqual(list(runner.catch_up_plan(p, jobs, MONDAY)), [date(2026, 10, 17)])
        p['catch_up_days'] = 0
        self.assertEqual(runner.catch_up_plan(p, jobs, MONDAY), {})
        p['catch_up_days'] = 7; p['excel_mode'] = 'rebuild'
        self.assertEqual(runner.catch_up_plan(p, jobs, MONDAY), {})

    def test_never_fetched_or_range_jobs_are_not_backfilled(self):
        p = self.profile()
        self.assertEqual(runner.catch_up_plan(p, p['jobs'], MONDAY), {})  # hiç başarılı sonuç yok
        self.seed(p, '2026-10-15')
        ranged = [{**p['jobs'][0], 'period': {'type': 'last_n_days', 'days': 7}}]
        self.assertEqual(runner.catch_up_plan(p, ranged, MONDAY), {})

    def test_monday_run_fills_friday_saturday_only_into_empty_cells(self):
        p = self.profile(); self.seed(p, '2026-10-15')
        path = self.report(p, prefill={'R14': 999})  # Cuma (16 Ekim) elle girilmiş
        logs = []
        with patch.object(runner, 'now', return_value=MONDAY), patch('netmera_automation.synthetic.time.sleep'):
            summary = runner.run_profile(p, log=logs.append)
        self.assertTrue(summary['excel_written'], summary['excel'])
        self.assertEqual(summary['catch_up']['days'], ['2026-10-16', '2026-10-17'])
        ws = load_workbook(path)['Rapor']
        self.assertEqual(ws['R14'].value, 999)                       # dolu kaçan gün korunur
        self.assertIsInstance(ws['S14'].value, int)                  # Cumartesi tamamlandı
        self.assertIsInstance(ws['T14'].value, int)                  # Pazar: normal çalışma
        self.assertTrue(any('hücre dolu' in line for line in logs), logs)
        # Ertesi gün aynı günler yeniden çekilmez.
        self.assertEqual(list(runner.catch_up_plan(p, p['jobs'], MONDAY)), [])

    def test_catch_up_problem_does_not_block_todays_write_and_fixed_cell_is_skipped(self):
        p = self.profile(); self.seed(p, '2026-10-15')
        p['excel_mappings'].append(dict(job_id=p['jobs'][0]['id'], column='All Total', sheet='Rapor', mode='fixed_cell', cell='B4'))
        p = store.save_profile(p)
        path = Path(p['output_file']); path.parent.mkdir(parents=True, exist_ok=True)
        b = Workbook(); ws = b.active; ws.title = 'Rapor'
        for i in range(14):  # başlık yalnız 18 Ekim'den itibaren: kaçan 16-17 bulunamaz
            ws[f'{L(3 + i)}13'] = date(2026, 10, 18 + i)
        b.save(path); b.close()
        with patch.object(runner, 'now', return_value=MONDAY), patch('netmera_automation.synthetic.time.sleep'):
            summary = runner.run_profile(p, log=lambda _: None)
        self.assertTrue(summary['excel_written'], summary['excel'])
        ws = load_workbook(path)['Rapor']
        self.assertIsInstance(ws['C14'].value, int)   # 18 Ekim bugünün verisi yazıldı
        self.assertTrue(any('bulunamadı' in n for n in summary['catch_up']['notes']), summary['catch_up'])
        fixed = ws['B4'].value
        self.assertEqual(fixed, ws['C14'].value)      # sabit hücrede kaçan gün değil, bugünün değeri

    def test_notification_text(self):
        title, text = notify.run_text({'ok': 5, 'failed': 0, 'write': True, 'excel': 'Google Sheets güncellendi.', 'excel_written': True,
                                       'catch_up': {'days': ['2026-10-16', '2026-10-17']}}, None)
        self.assertIn('tamam', title); self.assertIn('kaçan 2 gün', text)
        title, text = notify.run_text(None, 'Oturum açılamadı')
        self.assertIn('hatalı', title); self.assertEqual(text, 'Oturum açılamadı')

    def test_split_days_fills_each_past_day_into_its_own_empty_column(self):
        p = self.profile()  # hiç geçmiş sonuç yok: ilk doldurma
        path = self.report(p, prefill={'M14': 555})  # 11 Ekim elle girilmiş
        with patch.object(runner, 'now', return_value=MONDAY), patch('netmera_automation.synthetic.time.sleep'):
            summary = runner.run_profile(p, dates=(date(2026, 10, 10), date(2026, 10, 12)), split_days=True, log=lambda _: None)
        self.assertTrue(summary['excel_written'], summary['excel'])
        self.assertEqual(summary['catch_up']['days'], ['2026-10-10', '2026-10-11', '2026-10-12'])
        ws = load_workbook(path)['Rapor']
        self.assertIsInstance(ws['L14'].value, int)       # 10 Ekim
        self.assertEqual(ws['M14'].value, 555)            # dolu gün korunur
        self.assertIsInstance(ws['N14'].value, int)       # 12 Ekim
        self.assertNotEqual(ws['L14'].value, ws['N14'].value)  # gün başına ayrı sorgu
        self.assertIsNone(ws['O14'].value)
        # Sonraki normal çalışma bu günlerden sonrasını kaçan gün olarak görür.
        self.assertEqual(list(runner.catch_up_plan(p, p['jobs'], MONDAY)), [date(2026, 10, d) for d in range(13, 18)])

    def test_split_days_validation(self):
        p = self.profile()
        for kwargs in ({'dates': None}, {'dates': (date(2026, 9, 1), date(2026, 10, 15))}):
            with self.subTest(kwargs=kwargs), self.assertRaises(runner.RunError):
                runner.run_profile(p, split_days=True, log=lambda _: None, **kwargs)
        p['excel_mode'] = 'rebuild'
        with self.assertRaises(runner.RunError):  # yazarak bölme yalnız hücre eşlemede
            runner.run_profile(p, dates=(date(2026, 10, 1), date(2026, 10, 2)), split_days=True, log=lambda _: None)

    def test_split_days_in_test_mode_shows_each_day_without_writing(self):
        p = self.profile(); p['excel_mode'] = 'rebuild'; p = store.save_profile({**p, 'excel_mappings': []})
        with patch.object(runner, 'now', return_value=MONDAY), patch('netmera_automation.synthetic.time.sleep'):
            summary = runner.run_profile(p, write=False, dates=(date(2026, 10, 14), date(2026, 10, 16)), split_days=True, log=lambda _: None)
        self.assertFalse(Path(p['output_file']).exists())
        self.assertEqual(summary['rows'], [])
        self.assertEqual([r['period_start'][:10] for r in summary['day_rows']], ['2026-10-14', '2026-10-15', '2026-10-16'])
        self.assertTrue(summary['split']); self.assertEqual(summary['catch_up']['ok'], 3)
        self.assertEqual((summary['ok'],summary['failed']),(3,0))
        saved = store.read_json(store.profile_dir(p['id']) / 'son-calisma.json')
        self.assertEqual(len(saved['day_rows']), 3)
        self.assertEqual(store.load_results(p['id']), {})  # test sonucu depoya yazılmaz

    def test_split_days_all_filled_reports_no_new_output_write(self):
        p=self.profile()
        targets={f'{L(3+n-1)}14':888 for n in (10,11,12)}
        path=self.report(p,targets)
        with patch.object(runner,'now',return_value=MONDAY),patch('netmera_automation.synthetic.time.sleep'):
            summary=runner.run_profile(p,dates=(date(2026,10,10),date(2026,10,12)),split_days=True,log=lambda _:None)
        self.assertFalse(summary['excel_written'])
        self.assertIn('Yeni sonuç yazılmadı',summary['excel'])
        book=load_workbook(path)
        try:
            for cell,value in targets.items():self.assertEqual(book['Rapor'][cell].value,value)
        finally:book.close()

    def test_daily_failure_respects_write_nothing_policy(self):
        from netmera_automation.panel import PanelError
        class Reader:
            def __init__(self,*args,**kwargs):pass
            def fetch_many(self,jobs,first,last,per_day=False):
                return {j['id']:(PanelError('STEP_FAILED') if first.day==11 else {first:{'All Total':7}}) for j in jobs}
        p=self.profile(on_error='write_nothing');path=self.report(p);before=path.read_bytes()
        with patch.object(runner,'now',return_value=MONDAY),patch.object(runner,'SyntheticReader',Reader):
            summary=runner.run_profile(p,dates=(date(2026,10,10),date(2026,10,11)),split_days=True,log=lambda _:None)
        self.assertEqual((summary['ok'],summary['failed']),(1,1))
        self.assertFalse(summary['excel_written']);self.assertIn('kaydedilmedi',summary['excel'])
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(store.excel_batches(p),[])

    def test_same_query_jobs_share_one_find_and_catch_up_reads_days_together(self):
        calls = []

        class ManyReader:
            def __init__(self, *a, **k): pass
            def fetch(self, job, first, last): raise AssertionError('Event işleri toplu okunmalı')
            def fetch_many(self, jobs, first, last, per_day=False):
                calls.append(([j['label'] for j in jobs], first, last, per_day))
                days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
                if per_day:
                    return {j['id']: {d: {'All Total': d.day} for d in days} for j in jobs}
                return {j['id']: {'All Total': 100 + len(calls)} for j in jobs}

        p = self.profile()
        base = dict(event='View Content', filters=[{'property': 'Content Id', 'operator': 'Contains', 'values': ['DemoCampaign']}])
        p['jobs'] = [event_job(label='Gör', **base), event_job(label='Tekil', metric='unique_user_count', **base),
                     event_job(label='Oturum', metric='by_session', **base), event_job(label='Açılış', event='Open App')]
        p['excel_mappings'] = []
        p = store.save_profile(p)
        p['excel_mappings'] = [dict(job_id=j['id'], column='All Total', sheet='Rapor', mode='header_lookup', header_row=13, value_row=14 + i,
                                    date_basis='data_day', month_cell=None) for i, j in enumerate(p['jobs'])]
        p = store.save_profile(p)
        for job in p['jobs']:
            store.append_results(p['id'], [result(p, job_id=job['id'], signature=None, period_start='2026-10-15T00:00:00+03:00',
                                                  period_end='2026-10-16T00:00:00+03:00')])
        self.report(p)
        with patch.object(runner, 'now', return_value=MONDAY), patch.object(runner, 'SyntheticReader', ManyReader):
            summary = runner.run_profile(p, log=lambda _: None)
        self.assertEqual(summary['ok'], 12)  # 4 ana sonuç ve iki geçmiş günün 8 sonucu
        main = [c for c in calls if not c[3]]
        days = [c for c in calls if c[3]]
        self.assertEqual(sorted(len(c[0]) for c in main), [1, 3])           # 4 iş → 2 Find
        self.assertEqual(sorted(len(c[0]) for c in days), [1, 3])           # kaçan 2 gün → yine 2 Find
        self.assertTrue(all((c[1], c[2]) == (date(2026, 10, 16), date(2026, 10, 17)) for c in days))
        self.assertEqual(summary['catch_up']['days'], ['2026-10-16', '2026-10-17'])
        ws = load_workbook(p['output_file'])['Rapor']
        self.assertEqual((ws['R14'].value, ws['S14'].value), (16, 17))     # günler kendi sütununda
