"""Kaynak dosyalar, Google yanıtları ve on günlük döngü yalnız sentetik verilerle sınanır."""
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs

from openpyxl import Workbook

from netmera_automation import filter_source as F, jobs as J, runner, store, server
from netmera_automation.panel import PanelError
from tests.helpers import TempDataCase
from tests.test_segment_run import REBUILD


def source(path, **changes):
    return {'source': 'excel', 'file': str(path), 'sheet': 'Kaynak', 'date_column': 'A', 'value_column': 'C',
            'first_row': 2, 'last_row': 100, 'month_first': False, **changes}


class FilterSourceTests(TempDataCase):
    def setUp(self):
        super().setUp()
        self.file = self.tmp / 'kaynak.xlsx'
        self.day = date(2026, 10, 1)

    def book(self, rows):
        book = Workbook(); sheet = book.active; sheet.title = 'Kaynak'
        sheet.append(['Tarih', 'Okunmayacak alan', 'Filtre'])
        for day, value in rows:
            sheet.append([day, 'sentetik-diger', value])
        book.save(self.file); book.close()

    def job(self, **changes):
        job = deepcopy(REBUILD)
        job['items'][0]['attributes'][0]['value_source'] = source(self.file, **changes)
        return J.normalize_job(job)

    def test_excel_multi_value_date_range_reloads_and_preserves_original_job(self):
        self.book([(self.day, '001'), (self.day, 'ABC'), (self.day, '001'), (self.day + timedelta(days=1), 'ikinci')])
        job = self.job(); before = deepcopy(job)
        self.assertEqual(F.resolve_job(job, self.day, self.day)['items'][0]['attributes'][0]['values'], ['001', 'ABC'])
        self.assertEqual(F.read(source(self.file), self.day, self.day + timedelta(days=1)), ['001', 'ABC', 'ikinci'])
        self.book([(self.day, 'yenilendi')])
        self.assertEqual(F.read(source(self.file), self.day, self.day), ['yenilendi'])
        self.assertEqual(job, before)

    def test_missing_blank_bad_date_precision_and_multiple_equals_fail(self):
        for rows in ([(self.day, None)], [(None, 'x')], [('01.10', 'x')], [('gecersiz', 'x')],
                     [(self.day, True)], [(self.day, 10**16)], [(self.day + timedelta(days=1), 'x')]):
            with self.subTest(rows=rows):
                self.book(rows)
                with self.assertRaises(F.FilterSourceError): F.read(source(self.file), self.day, self.day)
        self.book([(self.day, 'a'), (self.day, 'b')])
        job = self.job(); job['items'][0]['attributes'][0]['operator'] = 'Equals'
        with self.assertRaises(F.FilterSourceError): F.resolve_job(job, self.day, self.day)

    def test_more_than_one_hundred_values_are_all_returned_in_order(self):
        values=[f'synthetic-{n:04d}' for n in range(275)]
        self.book([(self.day,v) for v in values] + [(self.day,values[0])])
        self.assertEqual(F.read(source(self.file,last_row=1000),self.day,self.day),values)
        # Üst sınır kaynakta seçilen satır aralığıdır; 100'de veya başka bir sayıda kırpma yok.
        rows=[(n+2,self.day,f'synthetic-{n:05d}') for n in range(20000)]
        with patch.object(F,'_rows',return_value=rows):
            result=F.read(source(self.file,last_row=20001),self.day,self.day)
        self.assertEqual(len(result),20000)
        self.assertEqual(result[-1],'synthetic-19999')

    def test_date_in_a_and_numeric_ids_in_b_selects_only_requested_day(self):
        book=Workbook();sheet=book.active;sheet.title='Kaynak'
        sheet.append(['Date','Campaign Id'])
        ids=[str(700000+n) for n in range(476)]
        for value in ids:
            sheet.append([self.day,int(value)])
        sheet.append([self.day,700000])  # Aynı güne ait tekrar tekilleştirilir.
        sheet.append([self.day+timedelta(days=1),'00042'])
        book.save(self.file);book.close()
        src=source(self.file,value_column='B',last_row=1000)
        self.assertEqual(F.read(src,self.day,self.day),ids)
        self.assertEqual(F.read(src,self.day+timedelta(days=1),self.day+timedelta(days=1)),['00042'])

    def test_google_only_reads_selected_two_column_ranges_without_writing(self):
        from netmera_automation import google_auth
        src = J._attribute_source(source(self.file, source='google_sheets', sheets_id='synthetic_sheet_12345', sheet="Kaynak'ın"), 'Sentetik')
        serial = (self.day - date(1899, 12, 30)).days
        result = {'valueRanges': [{'values': [[serial], [serial + 1]]}, {'values': [['001'], ['002']]}]}
        with patch.object(google_auth, 'access_token', return_value='synthetic'), patch.object(google_auth, 'request_json', return_value=result) as request:
            self.assertEqual(F.read(src, self.day, self.day), ['001'])
        self.assertEqual(request.call_count, 1)
        args = parse_qs(urlsplit(request.call_args.args[0]).query)
        self.assertEqual(args['ranges'], ["'Kaynak''ın'!A2:A100", "'Kaynak''ın'!C2:C100"])
        self.assertNotIn('body', request.call_args.kwargs)

    def test_schema_rejects_unsupported_or_ambiguous_settings(self):
        for changes in ({'date_column':'C'}, {'source':'other'}, {'first_row':20,'last_row':2}, {'last_row':50000}, {'sheets_id':'','source':'google_sheets'}):
            with self.subTest(changes=changes), self.assertRaises(J.JobError): self.job(**changes)
        job=self.job();job['rebuild']=False
        with self.assertRaises(J.JobError): J.normalize_job(job)

    def test_ten_day_runner_loop_resolves_each_day_and_missing_day_keeps_no_result(self):
        self.book([(self.day + timedelta(days=n), f'val-{n}') for n in range(10) if n != 4])
        job=self.job(); job['split_days']=True
        profile=store.create_profile('Sentetik kaynak', source='demo')
        profile.update(jobs=[job],output_file=str(self.tmp/'output.xlsx'));profile=store.save_profile(profile)
        seen=[]
        class Reader:
            page=None
            def fetch(reader, original, first, last):
                try: resolved=F.resolve_job(original,first,last)
                except F.FilterSourceError as exc: raise PanelError('ATTRIBUTE_SOURCE',detail=str(exc))
                seen.append((first,resolved['items'][0]['attributes'][0]['values']))
                return {None:17},None
        with patch.object(runner, '_diagnose_and_reset'):
            batches=runner._run_catch_up(Reader(),profile,{self.day+timedelta(days=n):profile['jobs'] for n in range(10)},
                                        runner.now(),lambda _:None,threading.Event())
        rows=[r for group in batches.values() for r in group]
        self.assertEqual(len(rows),10); self.assertEqual(sum(r['status']=='OK' for r in rows),9)
        bad=next(r for r in rows if r['status']!='OK')
        self.assertEqual((bad['status'],bad['value']),('ATTRIBUTE_SOURCE',None))
        self.assertEqual(seen,[(self.day+timedelta(days=n),[f'val-{n}']) for n in range(10) if n!=4])

    def test_google_source_requires_login_even_with_local_excel_output(self):
        from netmera_automation import google_auth
        profile=store.create_profile('Sentetik kaynak',source='netmera')
        profile.update(netmera_app='Sentetik App',output_file=str(self.tmp/'out.xlsx'),jobs=[self.job(source='google_sheets',sheets_id='synthetic_sheet_12345')])
        profile=store.save_profile(profile)
        with patch.object(google_auth,'status',return_value={'connected':False}):
            self.assertTrue(any('segment filtresi' in message for message in server.readiness(profile)))


if __name__ == '__main__': unittest.main()
