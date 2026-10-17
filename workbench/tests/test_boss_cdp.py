from test_support import ConfiguredTestCase, SELF_UID, SELF_NAME, RECIPIENT
import copy
import json
from pathlib import Path
import shutil
import subprocess
import threading
import unittest

from boss_cdp import (BossCDPReader, BossCDPBridge, CHAT,
                      _SNAPSHOT, _snapshot_script, identity_matches, validate_action)
from boss_reader import BossBlocked


def row(uid='7', **extra):
    return {'uid': uid, 'name': '陈女士', 'company': '公司', 'title': '招聘主管',
            'job_id': 'abcdefghijklmnop', 'last_msg': '你好', 'last_ts': 20,
            'from_uid': uid, 'to_uid': SELF_UID, **extra}


def raw(mid='20', uid='7', text='你好', *, mine=False, **body):
    me = {'uid': SELF_UID, 'name': SELF_NAME}
    other = {'uid': uid, 'name': '陈女士'}
    return {'mid': mid, 'time': int(mid), 'status': 2,
            'from': me if mine else other, 'to': other if mine else me,
            'body': {'type': 1, 'text': text, **body}}


def message(mid='20', text='你好', *, mine=False, **extra):
    return {'message_id': mid, 'timestamp': int(mid), 'from_uid': SELF_UID if mine else '7',
            'speaker': '本人' if mine else '招聘方', 'text': text, 'body_type': 1, 'status': 2, **extra}


def action(kind='text', **extra):
    return {'kind': kind, 'uid': '7', 'company': '公司', 'recruiter': '陈女士',
            'job_id': 'abcdefghijklmnop', 'title': 'AI应用', 'city': '广州',
            'known_message_ids': ['20'], 'text': '谢谢，已收到。', **extra}


def snapshot(**extra):
    a = action()
    return {**{k: a[k] for k in ('uid', 'company', 'recruiter', 'job_id', 'title', 'city')},
            'message_ids': ['20'], 'composer_text': '', 'evidence': [], **extra}


class PageReader(BossCDPReader):
    def __init__(self, lists=(), histories=(), **kwargs):
        super().__init__(Path('/not-used'), **kwargs)
        self.lists = list(lists)
        self.histories = list(histories)
        self.calls = []

    def _read_page(self, page):
        self.check_stop()
        self.calls.append(('list', page))
        return {'rows': self.lists.pop(0)}

    def _history_page(self, uid, page, max_msg_id='0'):
        self.check_stop()
        self.calls.append(('history', uid, page, max_msg_id))
        return self.histories.pop(0)


class FakeSession:
    def __init__(self, target, stop_event=None):
        self.target_id = target['id']
        self.target = target
        self.closed = False

    def evaluate(self, expression):
        if 'focus:document.hasFocus()' in expression:
            return {'url': self.target.get('url', CHAT), 'name': self.target.get('name', SELF_NAME),
                    'focus': self.target.get('focus', False), 'safety': self.target.get('safety', False)}
        return self.target.get('response', {'code': 0})

    def close(self):
        self.closed = True


class FakeReader:
    def __init__(self, histories=None):
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.rows = {'7': row()}
        self.histories = histories or [[message()]]
        self.history_calls = 0
        self.snap = snapshot()
        self.evaluations = []
        self.failure = None

    def check_stop(self):
        if self.stop_event.is_set():
            raise BossBlocked('stop', 'STOPPED')

    def full_history(self, uid, known_ids=()):
        self.check_stop()
        result = self.histories[min(self.history_calls, len(self.histories) - 1)]
        self.history_calls += 1
        return copy.deepcopy(result)

    def snapshot(self):
        self.check_stop()
        return self.snap

    def _evaluate(self, expression):
        self.check_stop()
        self.evaluations.append(expression)
        if '.panel-resume.sentence-popover' in expression:
            return 0
        return None

    def _connect(self):
        return self

    def call(self, method, params):
        self.check_stop()
        self.evaluations.append(method)


class TestBridge(BossCDPBridge):
    __test__ = False

    def __init__(self, reader, authorize=lambda: True):
        super().__init__('/not-used', reader, authorize=authorize)
        self.clicks = 0
        self.attachments = 0
        self.after_attachment = None
        self.click_error = None

    def _select(self, a):
        if not identity_matches(a, self.reader.snap):
            raise BossBlocked('identity', 'IDENTITY_MISMATCH')
        return self.reader.snap

    def _attachment(self):
        self.attachments += 1
        if self.after_attachment:
            self.after_attachment()
        return {'name': '示例候选人简历.pdf', 'count': 1}

    def _click(self, a, *, confirmation=False):
        self._authorized()
        self.clicks += 1
        if self.click_error:
            raise self.click_error
        return {'clicked': True}

    def _pause(self, seconds):
        self.reader.check_stop()


class BossCDPReaderTests(ConfiguredTestCase):
    def test_lists_until_empty_not_short_page(self):
        reader = PageReader(lists=[[row()], []])
        rows = reader.list_conversations()
        self.assertEqual(len(rows), 1)
        self.assertEqual(reader.calls, [('list', 1), ('list', 2)])
        self.assertNotIn('from_uid', rows[0])

    def test_wrong_account_uid_does_not_publish_rows(self):
        reader = PageReader(lists=[[row(to_uid='999')], []])
        with self.assertRaises(BossBlocked) as raised:
            reader.list_conversations()
        self.assertEqual(raised.exception.code, 'SELF_IDENTITY_MISMATCH')
        self.assertFalse(reader.rows)

    def test_duplicate_list_page_fails_closed(self):
        reader = PageReader(lists=[[row()], [row()]])
        with self.assertRaises(BossBlocked) as raised:
            reader.list_conversations()
        self.assertEqual(raised.exception.code, 'INCOMPLETE_PAGINATION')

    def test_history_uses_explicit_has_more_even_for_short_page(self):
        reader = PageReader(histories=[{'messages': [raw('20')], 'hasMore': True},
                                      {'messages': [raw('19')], 'hasMore': False}])
        self.assertEqual([m['message_id'] for m in reader.full_history('7')], ['19', '20'])
        self.assertEqual(len(reader.calls), 2)
        self.assertEqual(reader.calls, [('history', '7', 1, '0'), ('history', '7', 2, '20')])

    def test_history_uses_server_min_message_id_before_returned_minimum(self):
        reader = PageReader(histories=[{'messages': [raw('25'), raw('30')], 'hasMore': True, 'minMsgId': '21'},
                                      {'messages': [raw('19')], 'hasMore': False, 'minMsgId': '19'}])
        self.assertEqual([m['message_id'] for m in reader.full_history('7')], ['19', '25', '30'])
        self.assertEqual(reader.calls[1], ('history', '7', 2, '21'))

    def test_history_cursor_must_move_toward_older_messages(self):
        reader = PageReader(histories=[{'messages': [raw('30')], 'hasMore': True, 'minMsgId': '25'},
                                      {'messages': [raw('24')], 'hasMore': True, 'minMsgId': '25'}])
        with self.assertRaises(BossBlocked) as raised:
            reader.full_history('7')
        self.assertEqual(raised.exception.code, 'INCOMPLETE_HISTORY')
        self.assertIn('历史分页不完整', str(raised.exception))
        self.assertNotIn('连接', str(raised.exception))

    def test_history_cursor_handles_out_of_order_page_and_overlap(self):
        reader = PageReader(histories=[{'messages': [raw('29'), raw('30'), raw('28')], 'hasMore': True},
                                      {'messages': [raw('28'), raw('27')], 'hasMore': False}])
        self.assertEqual([m['message_id'] for m in reader.full_history('7')], ['27', '28', '29', '30'])
        self.assertEqual(reader.calls[1][-1], '28')

    def test_history_stops_at_known_baseline(self):
        reader = PageReader(histories=[{'messages': [raw('20')], 'hasMore': True}])
        self.assertEqual(len(reader.full_history('7', ['20'])), 1)

    def test_history_repetition_and_missing_has_more_are_errors(self):
        for packets in ([{'messages': [raw()], 'hasMore': True}] * 2, [{'messages': [raw()]}]):
            with self.subTest(packets=packets), self.assertRaises(BossBlocked):
                PageReader(histories=packets).full_history('7')

    def test_wrong_history_recipient_and_self_name_are_blocked(self):
        wrong_recipient = raw(uid='8')
        wrong_name = raw()
        wrong_name['to']['name'] = '其他人'
        for packet in (wrong_recipient, wrong_name):
            with self.subTest(packet=packet), self.assertRaises(BossBlocked):
                PageReader(histories=[{'messages': [packet], 'hasMore': False}]).full_history('7')

    def test_history_normalizes_and_removes_tokens(self):
        packet = raw(type=12, text='', hyperLink={'text': '您的附件简历 示例候选人简历 已发送给Boss',
                                                 'url': 'https://example.com/?securityId=secret'})
        result = PageReader(histories=[{'messages': [packet], 'hasMore': False}]).full_history('7')
        self.assertEqual(result[0]['body_type'], 12)
        self.assertNotIn('secret', json.dumps(result))

    def test_unique_target_is_dynamic_and_wrong_name_is_blocked(self):
        target = {'id': 'new-id', 'type': 'page', 'url': CHAT, 'name': '其他人'}
        reader = BossCDPReader('/', session_factory=FakeSession, target_provider=lambda: [target])
        with self.assertRaises(BossBlocked) as raised:
            reader._evaluate('1')
        self.assertEqual(raised.exception.code, 'SELF_IDENTITY_MISMATCH')

    def test_multiple_targets_require_unique_focused_correct_account(self):
        targets = [{'id': 'a', 'type': 'page', 'url': CHAT},
                   {'id': 'b', 'type': 'page', 'url': CHAT, 'focus': True}]
        reader = BossCDPReader('/', session_factory=FakeSession, target_provider=lambda: targets)
        reader._evaluate('1')
        self.assertEqual(reader.session.target_id, 'b')
        targets[1]['focus'] = False
        reader._evaluate('2')  # Losing focus after binding is harmless.
        reader.close()
        with self.assertRaises(BossBlocked) as raised:
            reader._evaluate('3')
        self.assertEqual(raised.exception.code, 'CHAT_PAGE_NOT_UNIQUE')

    def test_preferred_target_is_exact_and_does_not_require_focus(self):
        targets = [{'id': 'a', 'type': 'page', 'url': CHAT, 'focus': True},
                   {'id': 'saved-observed-id', 'type': 'page', 'url': CHAT}]
        reader = BossCDPReader('/', session_factory=FakeSession, target_provider=lambda: targets,
                               preferred_target_id='saved-observed-id')
        reader._evaluate('read')
        self.assertEqual(reader.target_id, 'saved-observed-id')

    def test_missing_or_navigated_preferred_target_never_falls_back(self):
        for targets in ([{'id': 'other', 'type': 'page', 'url': CHAT, 'focus': True}],
                        [{'id': 'saved', 'type': 'page', 'url': 'https://example.org/web/geek/chat'}]):
            reader = BossCDPReader('/', session_factory=FakeSession, target_provider=lambda: targets,
                                   preferred_target_id='saved')
            with self.subTest(targets=targets), self.assertRaises(BossBlocked) as raised:
                reader._evaluate('read')
            self.assertEqual(raised.exception.code, 'CHAT_TARGET_NOT_FOUND')

    def test_preferred_target_still_requires_correct_account(self):
        target = {'id': 'saved', 'type': 'page', 'url': CHAT, 'name': '其他人'}
        reader = BossCDPReader('/', session_factory=FakeSession, target_provider=lambda: [target],
                               preferred_target_id='saved')
        with self.assertRaises(BossBlocked) as raised:
            reader._evaluate('read')
        self.assertEqual(raised.exception.code, 'SELF_IDENTITY_MISMATCH')

    def test_36_37_latch_and_do_not_retry(self):
        for code in (36, 37):
            target = {'id': 'a', 'type': 'page', 'url': CHAT, 'response': {'code': code, 'message': 'securityId=secret'}}
            reader = BossCDPReader('/', session_factory=FakeSession, target_provider=lambda: [target])
            with self.assertRaises(BossBlocked) as raised:
                reader._evaluate('read')
            self.assertEqual(raised.exception.code, 'OFFICIAL_SAFETY_CHECK')
            self.assertNotIn('secret', str(raised.exception))
            target['response'] = {'code': 0}
            with self.assertRaises(BossBlocked):
                reader._evaluate('read again')

    def test_stop_before_connect_does_not_touch_browser(self):
        stop = threading.Event()
        stop.set()
        reader = BossCDPReader('/', stop, target_provider=lambda: self.fail('must not discover'))
        with self.assertRaises(BossBlocked) as raised:
            reader.snapshot()
        self.assertEqual(raised.exception.code, 'STOPPED')


class BossCDPBridgeTests(ConfiguredTestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is needed for the actual browser-expression regression')
    def test_real_confirmation_expression_rejects_online_or_unbound_panels(self):
        for title, content, uid, confirm, expected in (
            ('发送在线简历', '发送在线简历', '7', True, False),
            ('确定向 Boss 发送简历吗？', 'Boss确认后，该附件简历将直接发送至对方邮箱', '8', True, False),
            ('确定向 Boss 发送简历吗？', 'Boss确认后，该附件简历将直接发送至对方邮箱', '7', False, False),
            ('确定向 Boss 发送简历吗？', 'Boss确认后，该附件简历将直接发送至对方邮箱', '7', True, True),
        ):
            with self.subTest(title=title, uid=uid, confirm=confirm):
                reader = FakeReader()
                bridge = BossCDPBridge('/', reader, authorize=lambda: True)
                bridge._click(action('resume', request_id='20', request_mode='text'), confirmation=confirm)
                expression = reader.evaluations[-1].replace(_snapshot_script(), json.dumps(snapshot(), ensure_ascii=False))
                script = r'''
                  const cfg=JSON.parse(process.argv[1]);let clicks=0;
                  global.location={origin:'https://www.zhipin.com',pathname:'/web/geek/chat'};
                  const button={innerText:'确定',classList:{contains:()=>false},getAttribute:()=>null,
                    getClientRects:()=>[{}],click:()=>clicks++};
                  const wrapper={__vue__:{conversation$:{uid:cfg.uid},resumeId:'observed-id',
                    info:{resumeInfo:{resumeId:'observed-id'}}},querySelector:()=>({})};
                  const panel={innerText:cfg.title+cfg.content,getClientRects:()=>[{}],closest:()=>wrapper,
                    querySelector:s=>({innerText:s==='.title'?cfg.title:cfg.content}),querySelectorAll:()=>[button]};
                  global.document={body:{innerText:''},querySelector:()=>({innerText:'示例候选人'}),querySelectorAll:()=>[panel]};
                  const result=eval(cfg.expression);console.log(JSON.stringify({result,clicks}));
                '''
                proc = subprocess.run(['node', '-e', script, json.dumps({'title': title, 'content': content,
                    'uid': uid, 'expression': expression}, ensure_ascii=False)], capture_output=True, text=True, timeout=5)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                result = json.loads(proc.stdout)
                self.assertEqual(result['result']['clicked'], expected)
                self.assertEqual(result['clicks'], int(expected))

    def test_all_identity_fields_are_required(self):
        for key in ('uid', 'job_id', 'company', 'recruiter', 'title', 'city'):
            with self.subTest(key=key):
                self.assertFalse(identity_matches(action(), snapshot(**{key: 'wrong'})))

    def test_invalid_baseline_cannot_reach_browser(self):
        with self.assertRaises(BossBlocked):
            validate_action(action(known_message_ids=[]))

    def test_default_bridge_has_no_send_authorization(self):
        reader = FakeReader()
        result = TestBridge(reader, authorize=None)(action())
        self.assertEqual(result['reason'], 'NOT_AUTHORIZED_NOW')
        self.assertFalse(result['clicked'])

    def test_wrong_identity_never_clicks(self):
        reader = FakeReader()
        reader.snap['company'] = '其他公司'
        bridge = TestBridge(reader)
        self.assertEqual(bridge(action())['reason'], 'IDENTITY_MISMATCH')
        self.assertEqual(bridge.clicks, 0)

    def test_duplicate_text_never_clicks(self):
        reader = FakeReader([[message('20'), message('21', action()['text'], mine=True)]])
        bridge = TestBridge(reader)
        self.assertEqual(bridge(action())['status'], 'duplicate')
        self.assertEqual(bridge.clicks, 0)

    def test_new_server_message_during_final_read_never_clicks(self):
        reader = FakeReader([[message()], [message(), message('21', '新问题')]])
        bridge = TestBridge(reader)
        self.assertEqual(bridge(action())['reason'], 'HISTORY_CHANGED')
        self.assertEqual(bridge.clicks, 0)

    def test_stop_after_attachment_before_click(self):
        reader = FakeReader([[message(text='方便发一份简历吗')]])
        bridge = TestBridge(reader)
        bridge.after_attachment = reader.stop_event.set
        result = bridge(action('resume', request_id='20', request_mode='text'))
        self.assertEqual(result['reason'], 'STOPPED')
        self.assertFalse(result['clicked'])
        self.assertEqual(bridge.clicks, 0)

    def test_unknown_click_is_not_retried(self):
        reader = FakeReader()
        bridge = TestBridge(reader)
        bridge.click_error = BossBlocked('secret', 'CONNECTION_FAILED')
        first = bridge(action())
        self.assertEqual(first['status'], 'uncertain')
        self.assertIsNone(first['clicked'])
        self.assertEqual(bridge(action())['reason'], 'PREVIOUS_ATTEMPT_UNKNOWN')
        self.assertEqual(bridge.clicks, 1)

    def test_malformed_click_result_stays_unknown_and_cannot_retry(self):
        bridge = TestBridge(FakeReader())
        bridge._click = lambda a: None
        result = bridge(action())
        self.assertEqual(result['status'], 'uncertain')
        self.assertIsNone(result['clicked'])
        self.assertEqual(result['reason'], 'CLICK_RESULT_UNKNOWN')
        self.assertEqual(bridge(action())['reason'], 'PREVIOUS_ATTEMPT_UNKNOWN')

    def test_text_resume_path_is_guarded_not_unconditionally_disabled(self):
        reader = FakeReader([[message(text='方便发一份简历吗')]])
        bridge = TestBridge(reader)
        result = bridge(action('resume', request_id='20', request_mode='text'))
        self.assertEqual(result['status'], 'clicked')
        self.assertEqual(result['attachment']['name'], '示例候选人简历.pdf')
        self.assertEqual((bridge.clicks, bridge.attachments), (1, 1))

    def test_resume_receipt_or_prior_action_prevents_duplicate_send(self):
        request = message(text='方便发一份简历吗')
        receipt = message('21', '', body_type=12, hyperLink={'text': '您的附件简历 示例候选人简历 已发送给Boss点击查看附件'})
        prior = message('21', '', mine=True, body_type=4, action={'aid': 38})
        for later, expected in ((receipt, 'duplicate'), (prior, 'uncertain')):
            with self.subTest(expected=expected):
                reader = FakeReader([[request, later]])
                bridge = TestBridge(reader)
                result = bridge(action('resume', request_id='20'))
                self.assertEqual(result['status'], expected)
                self.assertEqual(bridge.clicks, 0)

    def test_click_alone_is_never_delivery(self):
        result = TestBridge(FakeReader())(action())
        self.assertEqual(result['status'], 'clicked')
        self.assertNotEqual(result['status'], 'verified')

    def test_verify_requires_both_server_and_ui_delivery(self):
        reader = FakeReader([[message(), message('21', action()['text'], mine=True)]])
        reader.snap['evidence'] = [{'message_id': '21', 'actor': 'self', 'text': action()['text'],
                                    'delivery': '', 'resume_receipt': False}]
        bridge = TestBridge(reader)
        a = action('verify', message_ids=['21'])
        self.assertEqual(bridge(a)['status'], 'uncertain')
        reader.snap['evidence'][0]['delivery'] = '未送达'
        self.assertEqual(bridge(a)['status'], 'uncertain')
        reader.snap['evidence'][0]['delivery'] = '已读'
        self.assertEqual(bridge(a)['status'], 'verified')
        reader.histories = [[message()]]
        self.assertEqual(bridge(a)['status'], 'uncertain')


if __name__ == '__main__':
    unittest.main()
