"""Offline fixture and task tests. No browser, account, model or mail needed."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from demo import DemoTasks, initialize_demo
from demo_data import CASES, NOTICE
from source_manifest import SOURCES, DOCUMENTS
from store import WorkspaceStore


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'demo'
        self.tasks = DemoTasks(self.root, step_delay=.001)

    def tearDown(self):
        self.tasks.stop()
        self.tasks.join(2)
        self.temp.cleanup()

    def record(self):
        return json.loads((self.root / SOURCES['applications']).read_text())

    def collect(self):
        self.tasks.start('collect')
        self.tasks.join(2)
        return self.tasks.status()

    def test_five_fictional_companies_and_six_usable_jobs_and_documents(self):
        record = self.record()
        self.assertTrue(record['demo_mode'])
        self.assertEqual(len(record['applications']), 6)
        self.assertEqual(len({a['company'] for a in record['applications']}), 5)
        self.assertTrue(all('虚构' in a['company'] for a in record['applications']))
        self.assertTrue(any(a['salary'] == '8-9K' for a in record['applications']))
        view = WorkspaceStore(self.root).read_view()
        self.assertTrue(view['meta']['available'])
        self.assertEqual(len(view['jobs']), 6)
        self.assertTrue(all(j['reviews'] and j['messages'] for j in view['jobs']))
        self.assertTrue(all((self.root / name).exists() for _, name, _, _ in DOCUMENTS))
        self.assertTrue(all(NOTICE in (self.root / name).read_text() for _, name, _, _ in DOCUMENTS))

    def test_collect_produces_exact_expected_outcomes_without_external_calls(self):
        with patch('socket.create_connection', side_effect=AssertionError('network forbidden')), \
             patch('subprocess.Popen', side_effect=AssertionError('process forbidden')):
            state = self.collect()
        self.assertEqual(state['status'], 'completed')
        self.assertTrue(state['demo'])
        self.assertEqual(state['counts'], dict(conversations=6, changed=6, ready=3, sent=0, needs_user=1, skipped=1, uncertain=1))
        ready = [i for i in state['items'] if i['status'] == 'ready']
        self.assertEqual([i['kind'] for i in ready], ['reply', 'reply', 'resume'])
        view = WorkspaceStore(self.root).read_view()
        self.assertEqual(len(view['tasks']), 5)
        self.assertEqual(view['summary']['pending_replies'], 1)

    def test_followup_only_simulates_once_and_preserves_decision_and_unknown(self):
        self.collect()
        with patch('socket.create_connection', side_effect=AssertionError('network forbidden')), \
             patch('subprocess.Popen', side_effect=AssertionError('process forbidden')):
            self.tasks.start('followup')
            self.tasks.join(2)
        state = self.tasks.status()
        self.assertEqual(state['counts']['sent'], 3)
        self.assertEqual(state['counts']['ready'], 0)
        self.assertEqual(state['counts']['needs_user'], 1)
        self.assertEqual(state['counts']['uncertain'], 1)
        records = self.record()
        outgoing = [m for a in records['applications'] for m in a['conversation_messages'] if m['kind'] == 'outgoing_text']
        self.assertEqual(len(outgoing), 3)
        self.assertTrue(all(m['text'].startswith('[本地模拟') for m in outgoing))
        self.assertFalse(any(i['status'] == 'verified_sent' for i in state['items']))
        self.assertEqual(records['reply_monitor']['notification_events'], [])
        self.collect()
        with self.assertRaises(ValueError):
            self.tasks.start('followup')
        self.assertEqual(len(self.record()['demo_completed_actions']), 3)
        self.assertEqual(len([m for a in self.record()['applications'] for m in a['conversation_messages'] if m['kind'] == 'outgoing_text']), 3)
        self.assertEqual({t['kind'] for t in WorkspaceStore(self.root).read_view()['tasks']}, {'user_input', 'uncertain_send'})

    def test_duplicate_start_and_stop_are_testable(self):
        self.tasks.step_delay = .2
        self.tasks.start('collect')
        with self.assertRaises(RuntimeError):
            self.tasks.start('collect')
        self.tasks.stop()
        self.tasks.join(2)
        self.assertEqual(self.tasks.status()['status'], 'stopped')
        self.assertEqual(self.record()['demo_completed_actions'], [])

    def test_stopping_followup_keeps_remaining_items_ready(self):
        self.collect()
        self.tasks.step_delay = .1
        self.tasks.start('followup')
        deadline = time.monotonic()+2
        while self.tasks.status()['counts']['sent'] == 0 and time.monotonic() < deadline:
            threading.Event().wait(.005)
        self.tasks.stop()
        self.tasks.join(2)
        state = self.tasks.status()
        self.assertEqual(state['status'], 'stopped')
        self.assertEqual(state['counts']['sent'], 1)
        self.assertEqual(state['counts']['ready'], 2)
        self.tasks.step_delay = .001
        self.tasks.start('followup')
        self.tasks.join(2)
        self.assertEqual(len(self.record()['demo_completed_actions']), 3)

    def test_restart_preserves_local_state_and_personal_preferences(self):
        self.collect()
        store = WorkspaceStore(self.root)
        store.update_preferences('demo-job-1', {'favorite': True, 'priority': '高', 'note': '演示备注：准备知识库说明'}, 0)
        restored = DemoTasks(self.root, step_delay=.001)
        self.assertEqual(restored.status()['counts']['ready'], 3)
        self.assertTrue(WorkspaceStore(self.root).read_preferences()['jobs']['demo-job-1']['favorite'])
        self.assertIn('知识库', WorkspaceStore(self.root).read_preferences()['jobs']['demo-job-1']['note'])

    def test_stopped_recollection_keeps_previously_known_items(self):
        self.collect()
        self.tasks.step_delay = .1
        self.tasks.start('collect')
        deadline = time.monotonic()+2
        while self.tasks.status()['counts']['conversations'] == 6 and time.monotonic() < deadline:
            threading.Event().wait(.005)
        self.tasks.stop()
        self.tasks.join(2)
        state = self.tasks.status()
        self.assertEqual(state['status'], 'stopped')
        self.assertEqual(len(state['items']), 6)
        self.assertEqual(state['counts']['ready'], 3)
        self.assertEqual(state['counts']['needs_user'], 1)
        self.assertEqual(state['counts']['uncertain'], 1)

    def test_existing_non_demo_record_is_never_overwritten(self):
        path = self.root / SOURCES['applications']
        original = '{"applications": [], "demo_mode": false}'
        path.write_text(original)
        with self.assertRaises(ValueError):
            initialize_demo(self.root)
        self.assertEqual(path.read_text(), original)
        # Restore the fixture so teardown can ask the local runner to stop.
        path.unlink()
        initialize_demo(self.root)


if __name__ == '__main__':
    unittest.main()
