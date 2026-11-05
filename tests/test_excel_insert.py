from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook, load_workbook
from openpyxl.styles import PatternFill

from netmera_automation import excel, runner, store
from tests.helpers import TempDataCase, event_job
from tests.test_run_and_excel import demo_profile


def batch(key='one', value=7):
    stamp='2026-03-10T09:00:00+03:00'
    row={'job_label':'Sentetik', 'column_label':'Sentetik', 'column':None, 'job_type':'people',
         'metric':'total_users','value':value,'status':'OK','period_start':None,'period_end':None,
         'captured_at':stamp,'planned_at':stamp,'description':'Sentetik sorgu','signature':'x','source':'demo'}
    return {'id':key,'source':'demo','planned_at':stamp,'rows':[row],'column_order':['Sentetik']}


class ExcelInsertTests(TempDataCase):
    def test_simultaneous_writes_to_the_same_file_are_blocked(self):
        path=self.tmp/'same.xlsx'
        with excel._excel_lock(path):
            with self.assertRaises(excel.ExcelLockedError):
                excel.write_incremental_workbook(path,[batch()],'demo','append_rows','Netmera')
        self.assertFalse(path.exists())

    def test_append_is_per_run_and_retry_is_idempotent(self):
        path=self.tmp/'out.xlsx'
        self.assertEqual(excel.write_incremental_workbook(path,[batch()], 'demo','append_rows','Netmera'),1)
        self.assertEqual(excel.write_incremental_workbook(path,[batch(),batch('two',9)],'demo','append_rows','Netmera'),1)
        self.assertEqual(excel.write_incremental_workbook(path,[batch(),batch('two',9)],'demo','append_rows','Netmera'),0)
        book=load_workbook(path)
        self.assertEqual(book['Netmera'].max_row,3)
        self.assertEqual([book['Netmera'].cell(r,4).value for r in (2,3)],[7,9])
        self.assertEqual(book[excel.LEDGER_SHEET].sheet_state,'veryHidden')
        book.close()
        backup=load_workbook(self.tmp/'out.netmera-yedek.xlsx')
        self.assertEqual(backup['Netmera'].max_row,2)
        backup.close()

    def test_foreign_sheet_columns_formulas_styles_and_backup_are_preserved(self):
        path=self.tmp/'company.xlsx'
        book=Workbook(); sheet=book.active; sheet.title='Rapor'
        sheet.append(['Notlar','Sentetik','Tarih']); sheet.append(['önceki',42,'eski tarih'])
        other=book.create_sheet('El ile'); other['B2']='=SUM(A1:A2)'
        other['B2'].fill=PatternFill('solid',fgColor='FF0000')
        other.merge_cells('A4:B4'); other['A4']='dokunma'
        book.properties.identifier='company-book'; book.save(path); book.close()
        original=path.read_bytes()
        excel.write_incremental_workbook(path,[batch()],'demo','append_rows','Rapor')
        self.assertEqual((self.tmp/'company.netmera-yedek.xlsx').read_bytes(),original)
        book=load_workbook(path,data_only=False)
        self.assertEqual(book['Rapor']['B3'].value,7)
        self.assertIsNone(book['Rapor']['A3'].value)
        self.assertEqual(book['Rapor']['A2'].value,'önceki')
        self.assertEqual(book['Rapor']['C3'].value.strftime('%Y-%m-%d'),'2026-03-10')
        self.assertEqual(book['El ile']['B2'].value,'=SUM(A1:A2)')
        self.assertEqual(book['El ile']['B2'].fill.fgColor.rgb,'00FF0000')
        self.assertIn('A4:B4',str(book['El ile'].merged_cells))
        self.assertEqual(book.properties.identifier,'company-book'); book.close()

    def test_new_sheet_unique_names_and_replay(self):
        path=self.tmp/'sheets.xlsx'
        book=Workbook(); book.active.title='Önceki'; book.active['A1']='koru'; book.save(path); book.close()
        self.assertEqual(excel.write_incremental_workbook(path,[batch(),batch('two')],'demo','new_sheet','Günlük Rapor'),2)
        self.assertEqual(excel.write_incremental_workbook(path,[batch(),batch('two')],'demo','new_sheet','Günlük Rapor'),0)
        book=load_workbook(path)
        visible=[s for s in book.worksheets if s.sheet_state=='visible']
        self.assertEqual(len(visible),3)
        self.assertEqual(book['Önceki']['A1'].value,'koru')
        self.assertTrue(any('(2)' in s.title for s in visible)); book.close()

    def test_bad_header_source_or_reserved_ledger_never_modifies_file(self):
        for kind in ('header','source','ledger','protected'):
            with self.subTest(kind=kind):
                path=self.tmp/f'{kind}.xlsx'
                book=Workbook(); book.active.title='Netmera'; book.active.append(['Yanlış'])
                if kind=='source': book.properties.identifier=excel.MARKERS['netmera']
                if kind=='ledger': book.create_sheet(excel.LEDGER_SHEET)['A1']='benim verim'
                if kind=='protected': book.active.protection.sheet=True
                book.save(path); book.close(); original=path.read_bytes()
                with self.assertRaises(ValueError):
                    excel.write_incremental_workbook(path,[batch()],'demo','append_rows','Netmera')
                self.assertEqual(path.read_bytes(),original)

    def test_formula_like_job_header_is_written_as_text(self):
        item=batch(); item['column_order']=['=2+2']; item['rows'][0]['column_label']='=2+2'
        path=self.tmp/'text.xlsx'
        excel.write_incremental_workbook(path,[item],'demo','append_rows','Netmera')
        book=load_workbook(path)
        self.assertEqual(book['Netmera']['D1'].data_type,'s'); book.close()

    def test_pending_rows_recover_after_failed_write_without_duplicates(self):
        profile=demo_profile(self.tmp,excel_mode='append_rows',excel_sheet='Netmera',jobs=[event_job()])
        with patch('netmera_automation.synthetic.time.sleep'):
            first=runner.run_profile(profile,log=lambda _:None)
            self.assertTrue(first['excel_written'])
            with patch('netmera_automation.runner.write_incremental_workbook',side_effect=excel.ExcelLockedError()):
                second=runner.run_profile(profile,log=lambda _:None)
            self.assertFalse(second['excel_written'])
        self.assertEqual(runner.rebuild_excel(profile),1)
        self.assertEqual(runner.rebuild_excel(profile),0)
        book=load_workbook(profile['output_file'])
        self.assertEqual(book['Netmera'].max_row,3); book.close()
        other={**profile,'output_file':str(self.tmp/'elsewhere.xlsx')}
        self.assertEqual(store.excel_batches(other),[])

    def test_failed_atomic_replace_keeps_original_and_can_retry(self):
        path=self.tmp/'atomic.xlsx'
        excel.write_incremental_workbook(path,[batch()],'demo','append_rows','Netmera')
        original=path.read_bytes(); replace=excel.os.replace
        def locked(src,dst):
            if Path(dst)==path: raise PermissionError('locked')
            return replace(src,dst)
        with patch('netmera_automation.excel.os.replace',side_effect=locked), self.assertRaises(excel.ExcelLockedError):
            excel.write_incremental_workbook(path,[batch(),batch('two')],'demo','append_rows','Netmera')
        self.assertEqual(path.read_bytes(),original)
        self.assertEqual(excel.write_incremental_workbook(path,[batch(),batch('two')],'demo','append_rows','Netmera'),1)
