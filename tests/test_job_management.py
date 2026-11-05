"""İş kopyalama/taşıma/toplu silme: yalnız geçici profiller ve sentetik işler."""
from copy import deepcopy
from unittest.mock import patch

from netmera_automation import job_management as M, jobs as J, store
from netmera_automation.server import App
from tests.helpers import TempDataCase, event_job
from tests.test_ga4_merge import ga4_job


class JobManagementTests(TempDataCase):
    def profile(self, name, jobs):
        p=store.create_profile(name,source='demo');p['jobs']=jobs
        return store.save_profile(p)

    def pair(self):
        job=J.normalize_job(event_job(label='Sentetik olay'))
        formula={'type':'formula','label':'Sentetik hesap','expression':'A * 2',
                 'variables':[{'name':'A','kind':'job','job_id':job['id'],'column':'All Total'}]}
        source=self.profile('Kaynak',[job,formula]); target=self.profile('Hedef',[job])
        return source,target

    def test_copy_remaps_formula_dependencies_renames_collision_and_preserves_source(self):
        source,target=self.pair();original=deepcopy(source);oldtarget=deepcopy(target)
        result=M.transfer(source['id'],target['id'],[j['id'] for j in source['jobs']],'copy')
        self.assertEqual(store.load_profile(source['id']),original)
        copied=store.load_profile(target['id'])['jobs'][1:]
        self.assertEqual([j['enabled'] for j in copied],[False,False])
        self.assertEqual(copied[0]['label'],'Sentetik olay (2)')
        self.assertEqual(copied[1]['variables'][0]['job_id'],copied[0]['id'])
        self.assertNotEqual(copied[0]['id'],source['jobs'][0]['id'])
        self.assertEqual(store.load_profile(target['id'])['output_file'],oldtarget['output_file'])
        self.assertEqual(result['count'],2)

    def test_move_requires_dependencies_and_removes_only_moved_mappings(self):
        source,target=self.pair();ids=[j['id'] for j in source['jobs']]
        for selected in ([ids[0]],[ids[1]]):
            with self.assertRaises(J.JobError):M.transfer(source['id'],target['id'],selected,'move')
        source['excel_mappings']=[{'job_id':ids[0],'column':'All Total','sheet':'Rapor','mode':'fixed_cell','cell':'B2'}]
        source=store.save_profile(source)
        M.transfer(source['id'],target['id'],ids,'move')
        left=store.load_profile(source['id'])
        self.assertEqual((left['jobs'],left['excel_mappings']),([],[]))
        self.assertEqual(len(store.load_profile(target['id'])['jobs']),3)

    def test_long_name_collisions_get_distinct_names(self):
        job=event_job(label='Sentetik ' + 'a'*60)
        source=self.profile('Uzun kaynak',[job]);target=self.profile('Uzun hedef',[job])
        for _ in range(2):M.transfer(source['id'],target['id'],[source['jobs'][0]['id']],'copy')
        names=[j['label'] for j in store.load_profile(target['id'])['jobs']]
        self.assertEqual(len({J.slug(name) for name in names}),3)

    def test_ga4_transfer_drops_output_binding_and_package_and_move_marks_exclusion(self):
        source=self.profile('GA4 kaynak',[ga4_job('Sentetik GA4',sheets={'sekme':'Sentetik'},paket={'kimlik':'kutuphane:sentetik','rapor':'r1'})])
        source['ga4']['paketler']=[{'kimlik':'kutuphane:sentetik','ad':'Sentetik','kaynak':'kutuphane','haric':[],'surum':1,'cekildi':'','tarih':None}]
        source=store.save_profile(source);target=self.profile('GA4 hedef',[])
        M.transfer(source['id'],target['id'],[source['jobs'][0]['id']],'move')
        copied=store.load_profile(target['id'])['jobs'][0]
        self.assertIsNone(copied['ga4'].get('sheets'));self.assertNotIn('paket',copied['ga4'])
        self.assertEqual(store.load_profile(source['id'])['ga4']['paketler'][0]['haric'],['r1'])

    def test_source_write_failure_rolls_back_target_and_keeps_source(self):
        source,target=self.pair();save=store.write_json
        def fail(path,data):
            if path==store.profile_dir(source['id'])/'profil.json':raise OSError('synthetic')
            return save(path,data)
        with patch.object(store,'write_json',side_effect=fail):
            with self.assertRaisesRegex(J.JobError,'geri alındı'):
                M.transfer(source['id'],target['id'],[j['id'] for j in source['jobs']],'move')
        self.assertEqual(store.load_profile(source['id']),source)
        self.assertEqual(store.load_profile(target['id']),target)

    def test_locked_target_invalid_selection_and_same_profile_do_not_write(self):
        source,target=self.pair();ids=[j['id'] for j in source['jobs']]
        with store.profile_lock(target['id']):
            with self.assertRaises(store.BusyError):M.transfer(source['id'],target['id'],ids,'copy')
        for args in ((source['id'],source['id'],ids,'copy'),(source['id'],target['id'],['missing'],'copy'),
                     ('../foreign',target['id'],ids,'move'),(source['id'],target['id'],ids,'invalid')):
            with self.assertRaises(J.JobError):M.transfer(*args)
        self.assertEqual(store.load_profile(source['id']),source)
        self.assertEqual(store.load_profile(target['id']),target)

    def test_delete_all_checks_snapshot_and_keeps_reports_history_and_other_project(self):
        source,target=self.pair();ids=[j['id'] for j in source['jobs']]
        history=store.profile_dir(source['id'])/'sonuclar.jsonl';history.write_text('synthetic history',encoding='utf-8')
        with self.assertRaises(J.JobError):M.delete_all(source['id'],ids[:1])
        self.assertEqual(M.delete_all(source['id'],ids)['count'],2)
        cleaned=store.load_profile(source['id'])
        self.assertEqual((cleaned['jobs'],cleaned['excel_mappings'],cleaned['ga4']['paketler']),([],[],[]))
        self.assertEqual(history.read_text(encoding='utf-8'),'synthetic history')
        self.assertEqual(store.load_profile(target['id']),target)

    def test_api_requires_delete_confirmation_and_refuses_busy_profiles(self):
        source,target=self.pair();app=App(0);ids=[j['id'] for j in source['jobs']]
        with self.assertRaises(ValueError):app.handle('POST','/api/jobs/delete-all',{'profile_id':source['id']},{})
        with patch.object(app.worker,'busy',return_value=True):
            app.worker.profile_id=target['id']
            with self.assertRaises(ValueError):app.handle('POST','/api/jobs/transfer',{'profile_id':source['id'],'target_profile_id':target['id'],'job_ids':ids,'mode':'copy'},{})
        result=app.handle('POST','/api/jobs/transfer',{'profile_id':source['id'],'target_profile_id':target['id'],'job_ids':ids,'mode':'copy'}, {})
        self.assertEqual(result['count'],2)
        self.assertEqual(app.handle('POST','/api/jobs/delete-all',{'profile_id':source['id'],'confirm':True,'expected_job_ids':ids},{})['count'],2)

    def test_bulk_enable_disable_preserves_unselected_jobs_mappings_and_other_profiles(self):
        source,target=self.pair();ids=[j['id'] for j in source['jobs']]
        source['excel_mappings']=[{'job_id':ids[0],'column':'All Total','sheet':'Rapor','mode':'fixed_cell','cell':'B2'}]
        original=store.save_profile(source)
        for action,enabled in [('disable',False),('enable',True)]:
            self.assertEqual(M.bulk(source['id'],[ids[0]],action),{'count':1,'action':action})
            expected=deepcopy(original);expected['jobs'][0]['enabled']=enabled
            self.assertEqual(store.load_profile(source['id']),expected)
            self.assertEqual(store.load_profile(target['id']),target)

    def test_bulk_delete_rejects_dangling_formulas_then_removes_only_selected_mappings(self):
        source,target=self.pair();ids=[j['id'] for j in source['jobs']]
        extra=J.normalize_job(event_job(label='Korunan sentetik olay'))
        source['jobs'].append(extra)
        source['excel_mappings']=[{'job_id':jid,'column':col,'sheet':'Rapor','mode':'fixed_cell','cell':cell}
            for jid,col,cell in [(ids[0],'All Total','B2'),(ids[1],None,'B3'),(extra['id'],'All Total','B4')]]
        source=store.save_profile(source)
        history=store.profile_dir(source['id'])/'sonuclar.jsonl';history.write_text('synthetic history',encoding='utf-8')
        with self.assertRaisesRegex(J.JobError,'hesaplamayı da birlikte seçin'):
            M.bulk(source['id'],[ids[0]],'delete')
        self.assertEqual(store.load_profile(source['id']),source)
        self.assertEqual(M.bulk(source['id'],ids,'delete')['count'],2)
        remaining=store.load_profile(source['id'])
        self.assertEqual(remaining['jobs'],[extra])
        self.assertEqual([m['job_id'] for m in remaining['excel_mappings']],[extra['id']])
        self.assertEqual(history.read_text(encoding='utf-8'),'synthetic history')
        self.assertEqual(store.load_profile(target['id']),target)

    def test_bulk_delete_ga4_package_job_stays_excluded_from_package_sync(self):
        source=self.profile('GA4 kaynak',[ga4_job('Sentetik GA4',paket={'kimlik':'kutuphane:sentetik','rapor':'rapor-1'}),event_job(label='Korunan olay')])
        source['ga4']['paketler']=[{'kimlik':'kutuphane:sentetik','ad':'Sentetik paket','kaynak':'kutuphane','tarih':{'tur':'dun'},'cekildi':'','haric':[],'surum':1}]
        source=store.save_profile(source)
        M.bulk(source['id'],[source['jobs'][0]['id']],'delete')
        remaining=store.load_profile(source['id'])
        self.assertEqual(remaining['ga4']['paketler'][0]['haric'],['rapor-1'])
        self.assertEqual(remaining['jobs'],source['jobs'][1:])

    def test_bulk_invalid_selection_action_confirmation_and_busy_leave_profile_unchanged(self):
        source,_=self.pair();ids=[j['id'] for j in source['jobs']];app=App(0)
        for selection,action in [([], 'enable'),(None,'enable'),([{}],'enable'),([ids[0],ids[0]],'disable'),
                                 ([ids[0],'missing'],'delete'),(ids,'invalid')]:
            with self.subTest(selection=selection,action=action),self.assertRaises(J.JobError):
                M.bulk(source['id'],selection,action)
            self.assertEqual(store.load_profile(source['id']),source)
        body={'profile_id':source['id'],'job_ids':ids,'action':'delete'}
        with self.assertRaisesRegex(ValueError,'onay gerekli'):
            app.handle('POST','/api/jobs/bulk',body,{})
        app.worker.profile_id=source['id']
        with patch.object(app.worker,'busy',return_value=True):
            for action in ('enable','disable','delete'):
                with self.assertRaisesRegex(ValueError,'işlem sürüyor'):
                    app.handle('POST','/api/jobs/bulk',{**body,'action':action,'confirm':True},{})
        self.assertEqual(store.load_profile(source['id']),source)
        for action in ('disable','enable','delete'):
            result=app.handle('POST','/api/jobs/bulk',{**body,'action':action,'confirm':True},{})
            self.assertEqual(result['count'],2)
