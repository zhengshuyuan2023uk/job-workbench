from test_support import ConfiguredTestCase, SELF_UID, SELF_NAME, RECIPIENT
import json
import multiprocessing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from automation_state import (
    AutomationState, EventStore, EventConflict, InvalidControl, RevisionConflict,
    RunnerAlreadyActive, StateCorrupt, exclusive_runner_lock,
)


def _enqueue_batch(root, start, count):
    events = EventStore(Path(root))
    for index in range(start, start + count):
        events.enqueue(f'event-{index}', company='测试公司')


def _try_runner_lock(root, results):
    try:
        with exclusive_runner_lock(Path(root)):
            results.put('acquired')
    except RunnerAlreadyActive:
        results.put('already-active')


def _try_control(root, results):
    try:
        AutomationState(Path(root)).control('resume', 0)
        results.put('changed')
    except RevisionConflict:
        results.put('conflict')


class AutomationStateTests(ConfiguredTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = AutomationState(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_default_is_paused_shadow(self):
        state = self.state.status()
        self.assertEqual(state['revision'], 0)
        self.assertEqual(state['mode'], 'shadow')
        self.assertTrue(state['paused'])

    def test_pause_inflight_and_revision_conflict(self):
        self.state.update(status='running', paused=False, in_flight={'event_key': 'x', 'phase': 'dispatching'})
        result = self.state.control('pause', 1)
        self.assertTrue(result['paused'])
        self.assertEqual(result['status'], 'pausing')
        with self.assertRaises(RevisionConflict):
            self.state.control('resume', 1)
        self.assertTrue(self.state.status()['paused'])

    def test_resume_does_not_clear_blocked_issue(self):
        self.state.update(status='blocked', issue={'code': '37', 'message': '需要官方验证'})
        with self.assertRaises(InvalidControl):
            self.state.control('resume', 1)
        result = self.state.control('recheck', 1)
        self.assertEqual(result['mode'], 'shadow')
        self.assertTrue(result['paused'])
        self.assertEqual(result['status'], 'blocked')
        self.assertTrue(result['recheck_requested_at'])
        self.assertEqual(result['issue']['code'], '37')

    def test_stale_status_is_view_only(self):
        old = (datetime.now(timezone.utc) - timedelta(seconds=100)).isoformat()
        self.state.update(status='running', paused=False, last_heartbeat_at=old)
        result = self.state.status()
        self.assertEqual(result['status'], 'offline')
        self.assertEqual(result['stored_status'], 'running')
        path = self.root / 'state/自动回复状态.json'
        self.assertEqual(json.loads(path.read_text())['status'], 'running')
        self.assertEqual(result['revision'], 1)

    def test_state_corruption_not_overwritten(self):
        self.state.update(status='starting')
        path = self.root / 'state/自动回复状态.json'
        path.write_text('{broken')
        for operation in (lambda: self.state.status(), lambda: self.state.update(status='running'),
                          lambda: self.state.control('resume', 1)):
            with self.assertRaises(StateCorrupt):
                operation()
        self.assertEqual(path.read_text(), '{broken')

    def test_private_fields_rejected(self):
        for fields in ({'prompt': 'secret'}, {'in_flight': {'prompt': 'secret'}},
                       {'issue': {'cookie': 'secret'}}, {'revision': 50}, {'paused': 'false'}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.state.update(**fields)
        self.assertEqual(self.state.status()['revision'], 0)

    def test_resume_only_changes_pause_not_mode(self):
        result = self.state.control('resume', 0)
        self.assertFalse(result['paused'])
        self.assertEqual(result['mode'], 'shadow')

    def test_second_process_refused_then_can_acquire(self):
        context = multiprocessing.get_context('spawn')
        results = context.Queue()
        with exclusive_runner_lock(self.root):
            process = context.Process(target=_try_runner_lock, args=(str(self.root), results))
            process.start()
            process.join(5)
            self.assertEqual(process.exitcode, 0)
            self.assertEqual(results.get(timeout=1), 'already-active')
        process = context.Process(target=_try_runner_lock, args=(str(self.root), results))
        process.start()
        process.join(5)
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(results.get(timeout=1), 'acquired')

    def test_path_symlink_refused(self):
        destination = self.root / 'elsewhere'
        destination.mkdir()
        # State parent is the workspace root.
        (self.root / 'state').symlink_to(destination, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.state.update(status='running')

    def test_failed_replace_preserves_original(self):
        self.state.update(status='starting')
        path = self.root / 'state/自动回复状态.json'
        before = path.read_bytes()
        with patch('automation_state.os.replace', side_effect=OSError('test disk error')):
            with self.assertRaises(OSError):
                self.state.update(status='running')
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(list(path.parent.glob('.自动回复状态-*')))

    def test_two_processes_cannot_control_same_revision(self):
        context = multiprocessing.get_context('spawn')
        results = context.Queue()
        processes = [context.Process(target=_try_control, args=(str(self.root), results)) for _ in range(2)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(5)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(sorted([results.get(timeout=1), results.get(timeout=1)]), ['changed', 'conflict'])


class EventStoreTests(ConfiguredTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = EventStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_enqueue_is_idempotent_and_preserves_intent(self):
        first = self.store.enqueue('job:message', text='原始已批准文案', status='authorized_pending_send')
        second = self.store.enqueue('job:message', text='新文案')
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.pending()), 1)

    def test_dispatching_restart_never_requeues(self):
        self.store.enqueue('x', text='已持久化意图')
        self.store.update('x', status='dispatching', expected_status='pending')
        restarted = EventStore(self.root)
        self.assertEqual(restarted.pending()[0]['status'], 'dispatching')
        with self.assertRaises(EventConflict):
            restarted.update('x', status='pending')
        with self.assertRaises(EventConflict):
            restarted.update('x', status='retrying')
        restarted.update('x', status='uncertain')
        with self.assertRaises(EventConflict):
            restarted.update('x', status='dispatching')
        restarted.update('x', status='verified_sent', message_id='123')
        self.assertEqual(restarted.pending(), [])
        self.assertEqual(restarted.get('x')['text'], '已持久化意图')

    def test_compare_and_set_prevents_double_claim(self):
        self.store.enqueue('x')
        self.store.update('x', status='dispatching', expected_status='pending')
        with self.assertRaises(EventConflict):
            self.store.update('x', status='dispatching', expected_status='pending')

    def test_corrupt_events_preserved(self):
        self.store.enqueue('x')
        path = self.root / 'state/自动回复事件.json'
        path.write_text('{broken')
        with self.assertRaises(StateCorrupt):
            self.store.enqueue('y')
        self.assertEqual(path.read_text(), '{broken')

    def test_concurrent_process_writes_have_no_lost_events(self):
        context = multiprocessing.get_context('spawn')
        processes = [context.Process(target=_enqueue_batch, args=(str(self.root), i * 10, 10)) for i in range(4)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(len(self.store.pending()), 40)
        json.loads((self.root / 'state/自动回复事件.json').read_text())


if __name__ == '__main__':
    unittest.main()
