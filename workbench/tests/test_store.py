from test_support import ConfiguredTestCase, SELF_UID, SELF_NAME, RECIPIENT
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from test_data import fixture, application, write_sources
from source_manifest import SOURCES
from store import WorkspaceStore, RevisionConflict


class PreferenceTests(ConfiguredTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        data = fixture()
        data['applications']['applications'] = [application()]
        write_sources(self.root, data)
        self.store = WorkspaceStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_preferences_survive_reload(self):
        self.store.update_preferences('a', {'favorite': True, 'note': '优先了解业务', 'priority': '高'}, 0)
        actual = WorkspaceStore(self.root).read_preferences()['jobs']['a']
        self.assertTrue(actual['favorite'])
        self.assertEqual(actual['note'], '优先了解业务')

    def test_stale_revision_rejected(self):
        self.store.update_preferences('a', {'note': '第一笔'}, 0)
        with self.assertRaises(RevisionConflict):
            self.store.update_preferences('a', {'note': '第二笔'}, 0)
        self.assertEqual(self.store.read_preferences()['jobs']['a']['note'], '第一笔')

    def test_invalid_preferences_do_not_write(self):
        for values in [{'favorite': 'false'}, {'priority': '急'}, {'note': 'a' * 10001}, {'status': 'sent'}]:
            with self.subTest(values=str(values)[:40]), self.assertRaises(ValueError):
                self.store.update_preferences('a', values, 0)
        self.assertEqual(self.store.read_preferences()['revision'], 0)

    def test_unknown_job_rejected(self):
        with self.assertRaises(ValueError):
            self.store.update_preferences('../../other', {'note': 'x'}, 0)

    def test_sources_unchanged_by_save(self):
        before = {p: hashlib.sha256((self.root / p).read_bytes()).digest() for p in SOURCES.values()}
        self.store.update_preferences('a', {'favorite': True}, 0)
        after = {p: hashlib.sha256((self.root / p).read_bytes()).digest() for p in SOURCES.values()}
        self.assertEqual(before, after)

    def test_corrupt_preferences_not_overwritten(self):
        self.store.update_preferences('a', {'note': 'keep'}, 0)
        path = self.root / 'state/个人管理.json'
        path.write_text('{broken')
        with self.assertRaises(ValueError):
            self.store.update_preferences('a', {'note': 'overwrite'}, 1)
        self.assertEqual(path.read_text(), '{broken')


if __name__ == '__main__':
    unittest.main()
