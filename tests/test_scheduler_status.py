import base64
import json
from types import SimpleNamespace
from unittest.mock import patch

from netmera_automation import scheduler, store
from netmera_automation.__main__ import main
from netmera_automation.runner import RunError
from tests.helpers import TempDataCase


class SchedulerStatusTests(TempDataCase):
    def setUp(self):
        super().setUp(); store.ensure_defaults()
        self.profile=store.load_profile('demo-sahte-veri')

    def info(self,**changes):
        return {'name':scheduler.task_name(self.profile),'owner_matches':True,
                'xml':scheduler.build_task_xml(self.profile,scheduler.python_for_tasks()),
                'state':3,'enabled':True,'last_run':'2026-03-10T09:00:00+03:00',
                'next_run':'2026-03-11T09:00:00+03:00','result_code':0,
                'command':str(scheduler.python_for_tasks()),'working_directory':str(scheduler.APP_ROOT),
                'arguments':f'-m netmera_automation run --profile {self.profile["id"]} --zamanli',**changes}

    def test_status_uses_structured_windows_values_and_checks_paths(self):
        output=SimpleNamespace(returncode=0,stdout=json.dumps({'tasks':[self.info()]}))
        with patch('netmera_automation.scheduler.subprocess.run',return_value=output) as run:
            info=scheduler.status(self.profile)
            script=base64.b64decode(run.call_args.args[0][-1]).decode('utf-16-le')
        self.assertTrue(info['path_matches']); self.assertEqual(info['result_hex'],'0x00000000')
        self.assertIn('Schedule.Service',script); self.assertIn('OutputEncoding',script)
        output.stdout=json.dumps({'tasks':[self.info(command='C:/old/pythonw.exe')]})
        with patch('netmera_automation.scheduler.subprocess.run',return_value=output):
            self.assertFalse(scheduler.status(self.profile)['path_matches'])

    def test_query_errors_are_unknown_and_not_reported_as_uninstalled(self):
        for result in (SimpleNamespace(returncode=2,stdout=''),SimpleNamespace(returncode=0,stdout='bad')):
            with patch('netmera_automation.scheduler.subprocess.run',return_value=result):
                self.assertIsNone(scheduler.status(self.profile)['installed'])
        with patch('netmera_automation.scheduler.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='{"tasks":[]}')):
            self.assertFalse(scheduler.status(self.profile)['installed'])

    def test_manual_task_start_is_explicit_and_refuses_stale_or_running_task(self):
        for change in ({'installed':False},{'path_matches':False},{'state':4},{'enabled':False}):
            with patch('netmera_automation.scheduler.status',return_value={'installed':True,'enabled':True,'path_matches':True,'state':3,**change}), \
                 patch('netmera_automation.scheduler._schtasks') as launch:
                with self.assertRaises(ValueError): scheduler.run_now(self.profile)
                launch.assert_not_called()
        with patch('netmera_automation.scheduler.status',return_value={'installed':True,'enabled':True,'path_matches':True,'state':3}), \
             patch('netmera_automation.scheduler._schtasks',return_value=SimpleNamespace(returncode=0)) as launch:
            scheduler.run_now(self.profile)
            self.assertEqual(launch.call_args.args,('/Run','/TN',scheduler.task_name(self.profile)))

    def test_scheduled_receipts_distinguish_success_partial_and_excel_failure(self):
        for failed,written,expected,status in ((0,True,0,'success'),(1,True,5,'partial'),(0,False,6,'excel_error')):
            with self.subTest(status=status):
                summary={'run_id':'test','write':True,'failed':failed,'ok':2-failed,'excel_written':written,'excel':'sentetik çıktı durumu'}
                def run(*args,**kwargs):
                    record=store.read_json(store.profile_dir(self.profile['id'])/'zamanli-son-durum.json')
                    self.assertEqual(record['status'],'running')
                    return summary
                with patch('netmera_automation.runner.run_profile',side_effect=run):
                    self.assertEqual(main(['run','--profile',self.profile['id'],'--zamanli']),expected)
                record=store.read_json(store.profile_dir(self.profile['id'])/'zamanli-son-durum.json')
                self.assertEqual(record['status'],status)
                self.assertTrue(record['completed_at']); self.assertEqual(record['exit_code'],expected)
                self.assertNotIn('rows',record)

    def test_early_failure_still_writes_scheduled_error_receipt(self):
        with patch('netmera_automation.runner.run_profile',side_effect=RunError('Sentetik giriş hatası')):
            self.assertEqual(main(['run','--profile',self.profile['id'],'--zamanli']),2)
        record=store.read_json(store.profile_dir(self.profile['id'])/'zamanli-son-durum.json')
        self.assertEqual(record['status'],'error'); self.assertEqual(record['error'],'Sentetik giriş hatası')

    def test_manual_run_does_not_replace_scheduled_receipt(self):
        path=store.profile_dir(self.profile['id'])/'zamanli-son-durum.json'
        store.write_json(path,{'status':'success','started_at':'önceki zamanlanmış'})
        summary={'write':False,'failed':0,'excel_written':None}
        with patch('netmera_automation.runner.run_profile',return_value=summary):
            self.assertEqual(main(['run','--profile',self.profile['id'],'--test']),0)
        self.assertEqual(store.read_json(path)['started_at'],'önceki zamanlanmış')
