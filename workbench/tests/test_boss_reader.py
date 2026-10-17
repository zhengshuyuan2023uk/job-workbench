from test_support import ConfiguredTestCase, SELF_UID, SELF_NAME, RECIPIENT
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from boss_reader import BossBlocked, BossReader, baseline_from_master, message_fingerprint


def response(rows=None, **extra):
    return subprocess.CompletedProcess([], 0, json.dumps({'ok': True, 'data': rows or [], 'pagination': None, **extra}), '')


class BossReaderTests(ConfiguredTestCase):
    def test_null_pagination_walks_to_empty_page_and_removes_tokens(self):
        calls = []
        answers = [response([{'uid': 7, 'brand_name': '公司', 'encrypt_job_id': 'job', 'security_id': 'secret', 'last_msg': '你好', 'last_ts': 11}]), response()]
        def run(args, **kwargs):
            calls.append((args, kwargs))
            return answers.pop(0)
        rows = BossReader(runner=run).list_conversations()
        self.assertEqual([x[0][-1] for x in calls], ['1', '2'])
        self.assertEqual(rows[0]['company'], '公司')
        self.assertNotIn('secret', json.dumps(rows))
        self.assertEqual(calls[0][0][calls[0][0].index('--cdp-url') + 1], 'http://127.0.0.1:9229')
        self.assertIn('127.0.0.1', calls[0][1]['env']['NO_PROXY'])

    def test_explicit_has_more_is_obeyed(self):
        calls = []
        def run(*args, **kwargs):
            calls.append(args)
            return response([{'uid': 1}], pagination={'has_more': False})
        self.assertEqual(len(BossReader(runner=run).list_conversations()), 1)
        self.assertEqual(len(calls), 1)

    def test_safety_error_no_retry_and_no_token_in_exception(self):
        calls = []
        def run(*args, **kwargs):
            calls.append(args)
            return response(ok=False, error={'code': 'PLATFORM_ERROR', 'message': 'secret_securityId_token', 'details': {'platform_code': 37}})
        with self.assertRaises(BossBlocked) as raised:
            BossReader(runner=run).list_conversations()
        self.assertEqual(len(calls), 1)
        self.assertNotIn('secret', str(raised.exception))

    def test_timeout_is_unknown_not_empty(self):
        def run(*args, **kwargs):
            raise subprocess.TimeoutExpired('boss', 30)
        with self.assertRaises(BossBlocked):
            BossReader(runner=run).list_conversations()

    def test_repeated_or_inconsistent_pages_fail_closed(self):
        for pages in [[response([{'uid': 1}]), response([{'uid': 1}])], [response([{'uid': 1, 'last_ts': 1}]), response([{'uid': 1, 'last_ts': 2}])]]:
            with self.subTest(pages=pages), self.assertRaises(BossBlocked):
                BossReader(runner=lambda *args, **kw: pages.pop(0)).list_conversations()

    def test_read_state_changes_do_not_trigger_message_fingerprint(self):
        a = {'uid': 7, 'job_id': 'x', 'last_msg': '&ldquo;你好&rdquo;', 'last_ts': 123, 'unread': 3, 'msg_status': '未读'}
        b = {**a, 'last_msg': '“你好”', 'msg_status': '已读', 'unread': 0}
        self.assertEqual(message_fingerprint(a), message_fingerprint(b))
        self.assertNotEqual(message_fingerprint(a), message_fingerprint({**a, 'last_ts': 124}))

    def test_master_baseline_wins_over_older_contact_index(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'applications.json').write_text(json.dumps({'reply_monitor': {
                'baseline': [{'conversation_uid': 7, 'job_id': 'job', 'last_list_message': 'new', 'last_message_timestamp': 123}],
                'account_contact_index': {'7': {'job_id': 'job', 'last_msg': 'old', 'last_ts': 100}, '8': {'job_id': 'another', 'last_msg': 'existing invitation', 'last_ts': 120}}
            }}))
            result = baseline_from_master(root)
            self.assertEqual(result['7']['last_msg'], 'new')
            self.assertEqual(result['8']['last_msg'], 'existing invitation')
            self.assertEqual(message_fingerprint(result['7']), message_fingerprint({'uid': 7, 'job_id': 'job', 'last_msg': 'new', 'last_ts': 123}))

    def test_history_preserves_request_evidence_not_credentials(self):
        payload = {'text': '', 'timestamp': 10, 'raw': {'mid': 20, 'from': {'uid': 7}, 'status': 2}, 'body': {
            'type': 7, 'dialog': {'text': '我想要一份您的附件简历，您是否同意', 'operated': False, 'securityId': 'secret'},
            'hyperLink': {'text': '您的附件简历 示例候选人简历 已发送给Boss', 'url': 'https://private?token=secret'}
        }}
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            return response([payload])
        actual = BossReader(runner=run).history('7')[0]
        self.assertEqual(actual['message_id'], '20')
        self.assertEqual(actual['from_uid'], '7')
        self.assertFalse(actual['dialog']['operated'])
        self.assertNotIn('secret', json.dumps(actual))
        self.assertIn('chatmsg', calls[0])

    def test_history_reads_until_known_message_not_only_latest_page(self):
        pages = [[{'timestamp': n, 'raw': {'mid': n, 'from': {'uid': 7}}} for n in range(51, 101)],
                 [{'timestamp': n, 'raw': {'mid': n, 'from': {'uid': 7}}} for n in range(1, 51)]]
        reader = BossReader(runner=lambda *args, **kwargs: response(pages.pop(0)))
        actual = reader.full_history(7, known_ids=['45'])
        self.assertEqual(len(actual), 100)
        self.assertEqual(actual[0]['message_id'], '1')


if __name__ == '__main__':
    unittest.main()
