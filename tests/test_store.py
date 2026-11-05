"""Bozuk proje kayıtları sağlam projeleri engellemez; yalnız geçici ve sentetik dosyalar."""
from netmera_automation import jobs as J, store
from tests.helpers import TempDataCase


class ProfileRecoveryTests(TempDataCase):
    def test_profile_cache_is_isolated_and_invalidated_by_external_edits(self):
        from unittest.mock import patch
        store.ensure_defaults()
        p=store.load_profile('demo-sahte-veri')
        with patch.object(store,'normalize_profile',wraps=store.normalize_profile) as normalize:
            first=store.load_profile(p['id'])
            first['jobs'][0]['enabled']=False
            again=store.load_profile(p['id'])
            self.assertTrue(again['jobs'][0]['enabled'])
            self.assertEqual(normalize.call_count,0)
            path=store.profile_dir(p['id'])/'profil.json'
            changed={**p,'name':'Dışarıda değiştirilmiş sentetik proje'}
            store.write_json(path,changed)
            self.assertEqual(store.load_profile(p['id'])['name'],changed['name'])
            self.assertEqual(normalize.call_count,1)

    def broken(self, content):
        path = store.profiles_root() / 'sentetik-bozuk' / 'profil.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_truncated_json_does_not_break_other_profiles_or_disappear(self):
        store.ensure_defaults()
        path = self.broken(b'{"name": "synthetic",')
        self.assertEqual(len(store.list_profiles()), 2)
        broken = store.broken_profiles()
        self.assertEqual([p['id'] for p in broken], ['sentetik-bozuk'])
        self.assertIn('okunamadı', broken[0]['error'])
        self.assertEqual(path.read_bytes(), b'{"name": "synthetic",')
        with self.assertRaises(J.JobError):
            store.load_profile('sentetik-bozuk')

    def test_invalid_encoding_and_non_object_json_are_reported(self):
        for content in (b'\xff', b'[]', b'null', b'42'):
            with self.subTest(content=content):
                self.broken(content)
                self.assertEqual(store.list_profiles(), [])
                self.assertEqual(len(store.broken_profiles()), 1)
                with self.assertRaises(J.JobError):
                    store.load_profile('sentetik-bozuk')

    def test_defaults_preserve_broken_profile_instead_of_replacing_it(self):
        path = self.broken(b'{')
        store.ensure_defaults()
        self.assertEqual(len(store.list_profiles()), 2)
        self.assertEqual(path.read_bytes(), b'{')

    def test_files_in_profile_root_are_not_scanned_as_directories(self):
        store.ensure_defaults()
        (store.profiles_root() / 'sentetik-not.txt').write_text('synthetic', encoding='utf-8')
        self.assertEqual(len(store.list_profiles()), 2)

    def test_profile_id_cannot_escape_profile_directory(self):
        for bad in (None, {}, '../sentetik-dis', '..\\sentetik-dis', str(self.tmp / 'dis'), 'a/b', 'a_b'):
            with self.subTest(bad=bad), self.assertRaises(J.JobError):
                store.profile_dir(bad)

    def test_broken_json_can_be_deleted_via_api_without_reading_its_content(self):
        from unittest.mock import patch
        from netmera_automation.server import App
        path = self.broken(b'{')
        with patch('netmera_automation.server.scheduler.remove', return_value=False):
            App(0).handle('POST', '/api/profile/delete', {'profile_id': 'sentetik-bozuk'}, {})
        self.assertFalse(path.parent.exists())
