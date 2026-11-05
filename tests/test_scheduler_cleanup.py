"""Silme/yeniden kurma yalnız bellekteki sahte Windows kayıtlarında denenir."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import xml.etree.ElementTree as ET

from netmera_automation import scheduler as S, store, jobs as J
from tests.helpers import TempDataCase

NS={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}


def task(profile, name=None, **changes):
    return {'name':name or S.task_name(profile),'xml':S.build_task_xml(profile,S.python_for_tasks()),
            'owner_matches':True,'state':3,'enabled':True,'command':str(S.python_for_tasks()),
            'working_directory':str(S.APP_ROOT),'arguments':S._arguments(profile),
            'last_run':None,'next_run':None,'result_code':0,**changes}


class FakeWindows:
    def __init__(self,tasks):
        self.tasks=deepcopy(tasks); self.calls=[]; self.fail_create=0; self.fail_delete_name=None

    def inventory(self,*_): return deepcopy(self.tasks)

    def schtasks(self,*args):
        op=args[0]; name=args[args.index('/TN')+1]; self.calls.append((op,name))
        existing=next((t for t in self.tasks if t['name'].casefold()==name.casefold()),None)
        if op=='/Delete':
            if name==self.fail_delete_name: return SimpleNamespace(returncode=1)
            if existing: self.tasks.remove(existing)
            return SimpleNamespace(returncode=0 if existing else 1)
        if op=='/Create':
            if self.fail_create:
                self.fail_create-=1; return SimpleNamespace(returncode=1)
            if existing and '/F' not in args: return SimpleNamespace(returncode=1)
            xml=Path(args[args.index('/XML')+1]).read_text(encoding='utf-16')
            root=ET.fromstring(xml); action=root.find('t:Actions/t:Exec',NS)
            if existing: self.tasks.remove(existing)
            self.tasks.append({'name':name,'xml':xml,'owner_matches':True,'state':3,'enabled':True,
                'command':action.findtext('t:Command',namespaces=NS),'working_directory':action.findtext('t:WorkingDirectory',namespaces=NS),
                'arguments':action.findtext('t:Arguments',namespaces=NS),'last_run':None,'next_run':None,'result_code':0})
            return SimpleNamespace(returncode=0)
        raise AssertionError(args)


class SchedulerCleanupTests(TempDataCase):
    def setUp(self):
        super().setUp(); store.ensure_defaults()
        self.profile=store.load_profile('demo-sahte-veri')
        self.profile['schedule']={'times':['09:00'],'days':['MON'],'name':'Sentetik günlük raporu'}

    def backend(self,fake):
        a=patch('netmera_automation.scheduler._inventory',side_effect=fake.inventory)
        b=patch('netmera_automation.scheduler._schtasks',side_effect=fake.schtasks)
        self.addCleanup(a.stop); self.addCleanup(b.stop); a.start(); b.start()

    def test_friendly_name_validation_and_stable_profile_marker(self):
        self.assertIn('Sentetik günlük raporu',S.task_name(self.profile))
        xml=ET.fromstring(S.build_task_xml(self.profile,S.python_for_tasks()))
        self.assertEqual(xml.findtext('t:RegistrationInfo/t:URI',namespaces=NS),S.task_owner(self.profile))
        for value in ('bad/name','bad\\name','a:b','a'*81):
            with self.subTest(value=value), self.assertRaises(J.JobError):
                J.normalize_profile({**self.profile,'schedule':{**self.profile['schedule'],'name':value}})

    def test_query_labels_are_literal_and_placeholders_are_not_expanded_recursively(self):
        self.profile['schedule']['name']="O'Brien __ARGS__"
        with patch('netmera_automation.scheduler._powershell',return_value={'tasks':[]}) as query:
            S._inventory(self.profile)
        script=query.call_args.args[0]
        self.assertIn("'Netmera - O''Brien __ARGS__ (demo-sahte-veri)'",script)
        self.assertIn("'"+S._arguments(self.profile)+"'",script)

    def test_reset_cleans_old_names_and_keeps_other_profiles(self):
        other={**self.profile,'id':'other'}
        fake=FakeWindows([task(self.profile,'Netmera Otomasyonu - demo-sahte-veri'),task(self.profile,'Eski rapor'),task(other)])
        self.backend(fake)
        result=S.reset(self.profile)
        self.assertEqual(result['removed'],2); self.assertTrue(result['installed'])
        self.assertEqual({t['name'] for t in fake.tasks},{S.task_name(self.profile),S.task_name(other)})
        self.assertFalse(any(op=='/Delete' and name==S.task_name(other) for op,name in fake.calls))

    def test_legacy_xml_without_owner_marker_is_recognized_by_exact_command(self):
        old=task(self.profile,'Netmera Otomasyonu - demo-sahte-veri')
        root=ET.fromstring(old['xml']); reg=root.find('t:RegistrationInfo',NS); reg.remove(reg.find('t:URI',NS))
        old['xml']=ET.tostring(root,encoding='unicode')
        self.assertTrue(S._owned(old,self.profile))
        other=deepcopy(old); other['xml']=other['xml'].replace('--profile demo-sahte-veri','--profile other')
        self.assertFalse(S._owned(other,self.profile))

    def test_remove_does_not_touch_wrong_owner_or_wrong_command(self):
        good=task(self.profile,'Eski görev')
        foreign=task(self.profile,'Başka kullanıcı',owner_matches=False)
        wrong=task(self.profile,'Başka komut'); wrong['xml']=wrong['xml'].replace('netmera_automation run','other_module run')
        fake=FakeWindows([good,foreign,wrong]); self.backend(fake)
        self.assertTrue(S.remove(self.profile))
        self.assertEqual({t['name'] for t in fake.tasks},{'Başka kullanıcı','Başka komut'})

    def test_active_task_blocks_all_mutations(self):
        for operation in (S.remove,S.reset,S.install):
            fake=FakeWindows([task(self.profile,state=4)])
            with patch('netmera_automation.scheduler._inventory',side_effect=fake.inventory),patch('netmera_automation.scheduler._schtasks',side_effect=fake.schtasks):
                with self.assertRaises(ValueError): operation(self.profile)
            self.assertEqual(fake.calls,[])

    def test_changed_snapshot_blocks_delete(self):
        fake=FakeWindows([task(self.profile)]); count=0
        def changed(*_):
            nonlocal count
            count+=1
            if count==3: fake.tasks[0]['xml']=fake.tasks[0]['xml'].replace('09:00','10:00')
            return fake.inventory()
        with patch('netmera_automation.scheduler._inventory',side_effect=changed),patch('netmera_automation.scheduler._schtasks',side_effect=fake.schtasks):
            with self.assertRaises(RuntimeError): S.remove(self.profile)
        self.assertEqual(fake.calls,[])

    def test_failed_new_registration_restores_old_tasks(self):
        originals=[task(self.profile,'Eski 1'),task(self.profile,'Eski 2')]
        fake=FakeWindows(originals); fake.fail_create=1; self.backend(fake)
        with self.assertRaisesRegex(RuntimeError,'geri yüklendi'): S.reset(self.profile)
        self.assertEqual({t['name']:t['xml'] for t in fake.tasks},{t['name']:t['xml'] for t in originals})

    def test_partial_delete_failure_restores_previously_removed_task(self):
        originals=[task(self.profile,'Eski 1'),task(self.profile,'Eski 2')]
        fake=FakeWindows(originals); fake.fail_delete_name='Eski 2'; self.backend(fake)
        with self.assertRaises(RuntimeError): S.remove(self.profile)
        self.assertEqual({t['name'] for t in fake.tasks},{'Eski 1','Eski 2'})

    def test_name_collision_and_legacy_install_require_explicit_cleanup(self):
        for tasks in ([task(self.profile,owner_matches=False)],[task(self.profile,'Eski ad')]):
            fake=FakeWindows(tasks)
            with patch('netmera_automation.scheduler._inventory',side_effect=fake.inventory),patch('netmera_automation.scheduler._schtasks',side_effect=fake.schtasks):
                with self.assertRaises(ValueError): S.install(self.profile)
            self.assertEqual(fake.calls,[])

    def test_install_update_and_repeated_remove_are_verified(self):
        fake=FakeWindows([]); self.backend(fake)
        self.assertEqual(S.install(self.profile),S.task_name(self.profile))
        self.assertEqual(S.install(self.profile),S.task_name(self.profile))
        self.assertEqual(len(fake.tasks),1)
        self.assertTrue(S.remove(self.profile)); self.assertFalse(S.remove(self.profile))

    def test_bad_schedule_does_not_delete_existing_tasks(self):
        fake=FakeWindows([task(self.profile)]); self.backend(fake)
        self.profile['schedule']['times']=[]
        with self.assertRaises(ValueError): S.reset(self.profile)
        self.assertEqual(fake.calls,[])

    def test_rollback_failure_is_reported_explicitly(self):
        fake=FakeWindows([task(self.profile,'Eski')]); fake.fail_create=3; self.backend(fake)
        with self.assertRaisesRegex(RuntimeError,'tamamı geri yüklenemedi'): S.reset(self.profile)
