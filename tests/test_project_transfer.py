"""Proje JSON aktarımı: sentetik profiller, geçici klasörler, ağ ve Windows görevi yok."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from unittest.mock import patch

from netmera_automation import jobs as J, project_transfer as P, store
from tests.helpers import TempDataCase, event_job
from tests.test_ga4_merge import ga4_job


class ProjectTransferTests(TempDataCase):
    def setUp(self):
        super().setUp()
        store.ensure_defaults()
        self.profile = store.create_profile('Sentetik proje', source='demo')
        event = J.normalize_job(event_job(label='Sentetik olay'))
        self.profile.update(jobs=[event, {'type': 'formula', 'label': 'Sentetik hesap', 'expression': 'A / 2',
            'variables': [{'name': 'A', 'kind': 'job', 'job_id': event['id'], 'column': 'All Total'}]}, ga4_job('Sentetik GA4')],
            output_file=str(self.tmp / 'rapor.xlsx'), excel_mode='mapped_cells',
            excel_mappings=[{'job_id': event['id'], 'column': 'All Total', 'sheet': 'Rapor', 'mode': 'fixed_cell', 'cell': 'B4'}],
            schedule={'times': ['09:00'], 'days': ['MON'], 'name': 'Sentetik görev'})
        self.profile = store.save_profile(self.profile)

    def bundle(self):
        return P.export_projects([self.profile['id']])['bundle']

    def test_export_only_normalized_settings_without_sessions_results_or_unknown_secrets(self):
        folder = store.profile_dir(self.profile['id'])
        raw = {**self.profile, 'access_token': 'synthetic-private-marker', 'password': 'synthetic-private-marker'}
        store.write_json(folder / 'profil.json', raw)
        for name in ('sonuclar.jsonl', 'son-calisma.json', 'giris-ayarlari.json', 'google-sheets-korumali.json'):
            (folder / name).write_text('synthetic-private-marker', encoding='utf-8')
        (folder / 'browser-profile').mkdir()
        before = (folder / 'profil.json').read_bytes()
        with patch.object(store, 'read_json', wraps=store.read_json) as read:
            exported = P.export_projects([self.profile['id']])
        self.assertEqual([c.args[0] for c in read.call_args_list], [folder / 'profil.json'])
        self.assertNotIn('synthetic-private-marker', json.dumps(exported))
        self.assertEqual(exported['bundle']['projects'], [self.profile])
        self.assertTrue(exported['filename'].endswith('.json'))
        self.assertTrue(exported['filename'].isascii())
        self.assertEqual((folder / 'profil.json').read_bytes(), before)

    def test_round_trip_preserves_jobs_formulas_mappings_and_schedule_with_new_identity(self):
        bundle = self.bundle()
        before = (store.profile_dir(self.profile['id']) / 'profil.json').read_bytes()
        with patch('netmera_automation.scheduler.install') as install:
            preview = P.preview_import(bundle)
            imported = P.import_projects(bundle, [0])
        install.assert_not_called()
        new = store.load_profile(imported['projects'][0]['id'])
        self.assertNotEqual(new['id'], self.profile['id'])
        self.assertEqual(new['name'], preview['items'][0]['new_name'])
        self.assertEqual(new['name'], 'Sentetik proje (2)')
        for key in ('excel_mappings', 'output_file', 'ga4', 'schedule'):
            self.assertEqual(new[key], self.profile[key])
        self.assertEqual(new['jobs'], [{**j, 'enabled': False} for j in self.profile['jobs']])
        self.assertEqual(new['jobs'][1]['variables'][0]['job_id'], new['jobs'][0]['id'])
        self.assertEqual([p.name for p in store.profile_dir(new['id']).iterdir()], ['profil.json'])
        self.assertEqual((store.profile_dir(self.profile['id']) / 'profil.json').read_bytes(), before)

    def test_keep_enabled_is_explicit_and_existing_disabled_jobs_stay_disabled(self):
        bundle = self.bundle()
        bundle['projects'][0]['jobs'][0]['enabled'] = False
        result = P.import_projects(bundle, [0], keep_enabled=True)
        new = store.load_profile(result['projects'][0]['id'])
        self.assertEqual([j['enabled'] for j in new['jobs']], [False, True, True])
        self.assertIn('durumları korundu', result['message'])

    def test_multiple_projects_subset_and_long_name_collisions(self):
        bundle = self.bundle()
        bundle['projects'].append({**deepcopy(self.profile), 'name': 'a' * 60})
        bundle['projects'].append({**deepcopy(self.profile), 'name': 'a' * 60})
        preview = P.preview_import(bundle)['items']
        self.assertEqual(preview[2]['new_name'], 'a' * 56 + ' (2)')
        result = P.import_projects(bundle, [2, 1])
        self.assertEqual([p['name'] for p in result['projects']], ['a' * 60, 'a' * 56 + ' (2)'])
        self.assertEqual(len({p['id'] for p in result['projects']}), 2)

    def test_invalid_bundle_version_and_content_never_create_projects(self):
        good = self.bundle()
        invalid = [None, [], {}, {**good, 'format': 'foreign'}, {**good, 'format_version': 3},
                   {**good, 'format_version': True}, {**good, 'projects': []}, {**good, 'projects': [None]},
                   {**good, 'projects': [self.profile] * 101}, {**good, 'token': 'synthetic'},
                   {**good, 'projects': [{**self.profile, 'id': '../foreign'}]}]
        before = sorted(p.name for p in store.profiles_root().iterdir())
        for bundle in invalid:
            with self.subTest(bundle_type=type(bundle).__name__), self.assertRaises(J.JobError):
                P.import_projects(bundle, [0])
        self.assertEqual(sorted(p.name for p in store.profiles_root().iterdir()), before)

    def test_duplicate_job_ids_missing_dependencies_and_invalid_unselected_project_are_rejected(self):
        duplicate = self.bundle()
        duplicate['projects'][0]['jobs'][2]['id'] = duplicate['projects'][0]['jobs'][0]['id']
        missing = self.bundle()
        missing['projects'][0]['jobs'][1]['variables'][0]['job_id'] = 'missing'
        bad_other = self.bundle()
        bad_other['projects'].append({**self.profile, 'jobs': [{'type': 'unsupported'}]})
        for bundle in (duplicate, missing, bad_other):
            with self.subTest(), self.assertRaises(J.JobError):
                P.import_projects(bundle, [0])
        self.assertEqual(len(store.list_profiles()), 3)

    def test_invalid_selection_and_flags_do_not_write(self):
        bundle = self.bundle()
        for indices in (None, [], [False], [0, 0], [-1], [1], ['0']):
            with self.subTest(indices=indices), self.assertRaises(J.JobError):
                P.import_projects(bundle, indices)
        with self.assertRaises(J.JobError):
            P.import_projects(bundle, [0], keep_enabled='yes')
        for selection in (None, [], [self.profile['id']] * 2, ['../foreign']):
            with self.subTest(selection=selection), self.assertRaises(J.JobError):
                P.export_projects(selection)
        self.assertEqual(len(store.list_profiles()), 3)

    def test_write_failure_rolls_back_only_new_directories(self):
        bundle = self.bundle()
        bundle['projects'].append(deepcopy(bundle['projects'][0]))
        original = store.write_json
        writes = []
        before = {p: p.read_bytes() for p in store.profiles_root().glob('*/profil.json')}
        def fail_second(path, data):
            writes.append(path)
            if len(writes) == 2:
                raise OSError('synthetic write failure')
            original(path, data)
        with patch.object(store, 'write_json', side_effect=fail_second):
            with self.assertRaisesRegex(J.JobError, 'geri alındı'):
                P.import_projects(bundle, [0, 1])
        self.assertEqual({p: p.read_bytes() for p in store.profiles_root().glob('*/profil.json')}, before)
        self.assertFalse(any(p.name.startswith('aktar-') for p in store.profiles_root().iterdir()))

    def test_size_limit_nonfinite_values_and_unknown_fields_are_not_persisted(self):
        bundle = self.bundle()
        with patch.object(P, 'MAX_BYTES', 20):
            with self.assertRaisesRegex(J.JobError, '5 MB'):
                P.preview_import(bundle)
        with self.assertRaises(J.JobError):
            P.preview_import({**bundle, 'projects': [{**self.profile, 'extra': float('nan')}]})
        bundle['projects'][0]['access_token'] = 'synthetic-private-marker'
        result = P.import_projects(bundle, [0])
        raw = (store.profile_dir(result['projects'][0]['id']) / 'profil.json').read_text(encoding='utf-8')
        self.assertNotIn('synthetic-private-marker', raw)

    def test_parallel_imports_have_distinct_names_and_do_not_overwrite(self):
        bundle = self.bundle()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: P.import_projects(bundle, [0]), range(2)))
        projects = [r['projects'][0] for r in results]
        self.assertEqual({p['name'] for p in projects}, {'Sentetik proje (2)', 'Sentetik proje (3)'})
        self.assertEqual(len({p['id'] for p in projects}), 2)
        self.assertEqual(store.load_profile(self.profile['id']), self.profile)

    def test_network_and_device_paths_are_rejected_before_filesystem_resolution(self):
        for path in ('\\\\synthetic.invalid\\report\\source.xlsx', '//synthetic.invalid/report/source.xlsx', '\\\\?\\C:\\source.xlsx'):
            for mode in ('output', 'formula'):
                bundle = self.bundle()
                if mode == 'output':
                    bundle['projects'][0]['output_file'] = path
                else:
                    bundle['projects'][0]['jobs'][1]['variables'] = [{'name': 'A', 'kind': 'cell', 'source': 'excel',
                        'file': path, 'sheet': 'Rapor', 'mode': 'fixed_cell', 'cell': 'A1'}]
                with self.subTest(mode=mode, path_type=path[:2]), patch('netmera_automation.jobs.check_local_path') as check:
                    with self.assertRaisesRegex(J.JobError, 'Ağ ve aygıt'):
                        P.preview_import(bundle)
                    check.assert_not_called()
        self.assertEqual(len(store.list_profiles()), 3)

    def test_dynamic_segment_source_dates_polling_and_daily_loop_survive_round_trip(self):
        from tests.test_segment_run import REBUILD
        segment = deepcopy(REBUILD)
        segment.update(split_days=True, poll_seconds=20, period={'type': 'custom', 'start': '2026-10-24', 'end': '2026-10-25'})
        source = {'source': 'excel', 'file': str(self.tmp / 'sentetik-kaynak.xlsx'), 'sheet': 'Rapor',
                  'date_column': 'A', 'value_column': 'B', 'first_row': 2, 'last_row': 2000}
        segment['items'][0]['attributes'][0]['value_source'] = source
        self.profile['jobs'].append(segment)
        self.profile = store.save_profile(self.profile)
        result = P.import_projects(self.bundle(), [0])
        imported = store.load_profile(result['projects'][0]['id'])['jobs'][-1]
        self.assertEqual(imported, {**self.profile['jobs'][-1], 'enabled': False})
        self.assertEqual(imported['items'][0]['attributes'][0]['value_source']['date_column'], 'A')
        self.assertEqual(imported['items'][0]['attributes'][0]['value_source']['value_column'], 'B')

    def test_long_generated_ids_and_google_bindings_survive_export_and_import(self):
        prefix = 'Sentetik uzun ortak rapor adı ' + 'x' * 35
        jobs = [ga4_job(prefix + str(n), sheets={'tablo':'synthetic_sheet_id_12345','sekme':f'Sayfa {n}'}) for n in range(3)]
        self.profile.update(jobs=jobs, excel_mappings=[], output_target='google_sheets', sheets_id='synthetic_output_12345')
        self.profile=store.save_profile(self.profile)
        second = self.profile['jobs'][1]['id']
        self.assertGreater(len(second),50)
        self.profile['jobs'].append({'type':'formula','label':'Sentetik hesap', 'expression':'A + B',
            'variables':[{'name':'A','kind':'job','job_id':second,'column':'activeUsers'},
                         {'name':'B','kind':'cell','source':'google_sheets','sheets_id':'synthetic_input_12345','sheet':'Kaynak','mode':'fixed_cell','cell':'C5'}]})
        self.profile['excel_mappings']=[{'job_id':second,'column':'activeUsers','sheet':'Rapor','mode':'fixed_cell','cell':'B4'}]
        self.profile['ga4']['sheets']['tablo']='synthetic_ga4_default_12345'
        self.profile=store.save_profile(self.profile)
        bundle=self.bundle()
        imported=P.import_projects(bundle,[0],keep_enabled=True)
        new=store.load_profile(imported['projects'][0]['id'])
        for key in ('jobs','excel_mappings','output_target','sheets_id','ga4','schedule'):
            self.assertEqual(new[key],self.profile[key])

    def test_ga4_properties_are_carried_without_credentials_and_conflicts_are_remapped(self):
        from netmera_automation.ga4 import ayarlar as settings
        settings._yaz({'propertyler':{'PROP_PROD':{'property_id':'123456789'},'UNUSED':{'property_id':'111111111'}},
                        'kimlik':{'tur':'google_hesabi','istemci_dosyasi':'synthetic-private-marker'}})
        bundle=self.bundle()
        self.assertEqual(bundle['connections'],{'ga4_properties':{'PROP_PROD':{'property_id':'123456789'}}})
        self.assertNotIn('synthetic-private-marker',json.dumps(bundle))
        target={'propertyler':{'PROP_PROD':{'property_id':'987654321'}},'kimlik':{'tur':'google_hesabi','istemci_dosyasi':'target-client'}}
        settings._yaz(target)
        preview=P.preview_import(bundle)
        self.assertEqual(preview['connections'],{'ga4_properties':1,'remapped':1})
        result=P.import_projects(bundle,[0])
        new=store.load_profile(result['projects'][0]['id'])
        self.assertEqual(new['jobs'][2]['ga4']['property_ref'],'PROP_PROD_2')
        saved=settings.load()
        self.assertEqual(saved['kimlik'],target['kimlik'])
        self.assertEqual(saved['propertyler']['PROP_PROD']['property_id'],'987654321')
        self.assertEqual(saved['propertyler']['PROP_PROD_2']['property_id'],'123456789')

    def test_connection_write_failure_rolls_back_new_projects_and_preserves_existing_settings(self):
        from netmera_automation.ga4 import ayarlar as settings
        bundle=self.bundle(); bundle['connections']={'ga4_properties':{'PROP_PROD':{'property_id':'123456789'}}}
        before={p:p.read_bytes() for p in store.profiles_root().glob('*/profil.json')}
        original=store.write_json
        def fail(path,data):
            if path == settings.settings_path(): raise OSError('synthetic failure')
            return original(path,data)
        with patch.object(store,'write_json',side_effect=fail), self.assertRaises(J.JobError):
            P.import_projects(bundle,[0])
        self.assertEqual(before,{p:p.read_bytes() for p in store.profiles_root().glob('*/profil.json')})
        self.assertFalse(settings.settings_path().exists())

    def test_version_one_exports_are_still_accepted(self):
        bundle=self.bundle(); bundle['format_version']=1; bundle.pop('connections')
        result=P.import_projects(bundle,[0])
        self.assertEqual(len(store.load_profile(result['projects'][0]['id'])['jobs']),3)
