from test_support import ConfiguredTestCase, SELF_UID, SELF_NAME, RECIPIENT
"""Integration regressions using only temporary files and in-memory BOSS fakes."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from automation_state import AutomationState, EventStore
from boss_reader import BossBlocked
from master_record import read_master
from reply_engine import ReplyEngine, event_key


ROW = dict(uid='7', job_id='job7', company='测试公司', title='跨境运营', name='王女士',
           last_msg='你做过什么项目？', last_ts=2, msg_status='read', unread=1)
OLD = dict(message_id='100', timestamp=1, from_uid=SELF_UID, body_type=1, text='您好')
NEW = dict(message_id='200', timestamp=2, from_uid='7', body_type=1, text='你做过什么项目？')
REPLY = dict(kind='reply', text='我有跨境电商学习背景，并做过海外内容实践。',
             reason='依据已确认经历', evidence=['用户拥有跨境电商学习背景'], notification='none', question_for_user='')


class FakeReader:
    def __init__(self, messages=None):
        self.messages = deepcopy(messages or [OLD, NEW])
        self.rows = [deepcopy(ROW)]
        self.reads = []
        self.on_history = None

    def full_history(self, uid, known=()):
        self.reads.append((uid, set(known)))
        if self.on_history:
            self.on_history(len(self.reads))
        return deepcopy(self.messages)

    def list_conversations(self):
        return deepcopy(self.rows)


class FakePlanner:
    def __init__(self):
        self.calls = []
        self.callback = None

    def plan(self, context):
        self.calls.append(deepcopy(context))
        if self.callback:
            self.callback()
        return deepcopy(REPLY)


class FakeVerifier:
    def verify(self, context, plan):
        return dict(approved=True, reason='测试事实已核验', unsupported_claims=[])


class FakeBridge:
    def __init__(self, reader):
        self.reader = reader
        self.calls = []
        self.result = None

    def __call__(self, payload):
        self.calls.append(deepcopy(payload))
        if self.result is not None:
            return deepcopy(self.result)
        if payload['kind'] == 'verify':
            return dict(status='verified', clicked=False, evidence=[])
        next_id = str(max(499, *(int(message['message_id']) for message in self.reader.messages)) + 1)
        if payload['kind'] == 'text':
            self.reader.messages.append(dict(message_id=next_id, timestamp=6, from_uid=SELF_UID,
                                             body_type=1, text=payload['text']))
        elif payload['kind'] == 'resume':
            self.reader.messages.append(dict(message_id=next_id, timestamp=4, from_uid=SELF_UID,
                                             body_type=4, text='', action={'aid': 38}))
            self.reader.messages.append(dict(message_id=str(int(next_id) + 1), timestamp=5, from_uid='7', body_type=12,
                                             text='',hyperLink={'text':'您的附件简历 示例候选人简历 已发送给Boss点击查看附件'}))
        return dict(status='clicked', clicked=True)

    def sends(self):
        return [call for call in self.calls if call['kind'] in ('text', 'resume')]


class ReplyEngineTests(ConfiguredTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.master_path = self.root / 'applications.json'
        self.master_path.write_text(json.dumps({
            'applications': [dict(company='测试公司', title='跨境运营', city='广州', job_id='job7',
                                  description='负责跨境业务内容，运用AI辅助整理企业资料。',address='广州天河区测试地址',
                                  conversation_uid=7, monitoring_enabled=True, identity_verified=True,
                                  conversation_messages=[deepcopy(OLD)])],
            'ongoing_search_authorization': {'reply_authorization': {'enabled': True},
                'resume_request_authorization': {'enabled':True,'attachment_name':'示例候选人简历','text_resume_ui_verified':True}},
            'reply_monitor': {
                'execution_owner': 'resident-v1', 'notification_events': [],
                'baseline': [dict(conversation_uid=7, job_id='job7', known_message_ids=['100'],
                                  last_list_message='您好', last_message_timestamp=1)]},
        }, ensure_ascii=False))
        (self.root / 'facts.md').write_text('用户拥有跨境电商学习背景，做过海外内容实践。')
        self.reader = FakeReader()
        self.planner = FakePlanner()
        self.bridge = FakeBridge(self.reader)
        self.engine = ReplyEngine(self.root, self.reader, self.planner, FakeVerifier(), self.bridge)
        self.state = AutomationState(self.root)
        self.state.update(mode='live', paused=False, status='running')
        # Avoid reading the real machine automation file. Test the actual runtime
        # pause/issue boundary, while the fixture supplies validated ownership.
        self.engine.allowed = lambda: self.state.status()['mode'] == 'live' and not self.state.status()['paused'] and not self.state.status()['issue']

    def tearDown(self):
        self.temp.cleanup()

    def test_shadow_never_sends_or_changes_master(self):
        before = self.master_path.read_bytes()
        result = self.engine.process(deepcopy(ROW), shadow=True)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.bridge.calls, [])
        self.assertEqual(before, self.master_path.read_bytes())

    def test_user_already_replied_is_not_repeated(self):
        self.reader.messages.append(dict(message_id='201', timestamp=3, from_uid=SELF_UID, body_type=1, text='我来回复这个问题。'))
        self.assertEqual(self.engine.process(deepcopy(ROW))['status'], 'superseded')
        self.assertEqual(self.planner.calls, [])
        self.assertEqual(self.bridge.calls, [])

    def test_ignore_decision_is_archived_once_without_becoming_a_sent_action(self):
        self.planner.plan=lambda context: dict(kind='ignore',text='',reason='普通收到回执无需重复回应',evidence=[],notification='none',question_for_user='')
        self.assertEqual(self.engine.process(deepcopy(ROW))['status'],'ignored')
        self.engine.process(deepcopy(ROW))
        app=read_master(self.root)['applications'][0]
        self.assertEqual(len(app['followup_actions']),1)
        action=app['followup_actions'][0]
        self.assertEqual(action['incoming_message_ids'],['200'])
        self.assertEqual(action['status'],'ignored')
        self.assertNotIn('sent_at',action)
        self.assertNotIn('send_stage',app)
        self.assertEqual(self.bridge.sends(),[])

    def test_plain_text_resume_request_without_verified_control_is_not_clicked(self):
        self.reader.messages[-1]['text']='请发一份简历'
        data=json.loads(self.master_path.read_text())
        data['ongoing_search_authorization']['resume_request_authorization'].pop('text_resume_ui_verified')
        self.master_path.write_text(json.dumps(data,ensure_ascii=False))
        self.planner.plan=lambda context: dict(kind='resume',text='',reason='文字索要',evidence=[],notification='none',question_for_user='')
        result=self.engine.process(deepcopy(ROW))
        self.assertEqual(result['status'],'awaiting_user')
        self.assertEqual(self.bridge.sends(),[])
        self.assertFalse(self.state.status()['paused'])
        event=read_master(self.root)['reply_monitor']['notification_events'][0]
        self.assertIn('没有已核验',event['body'])


    def resume_request(self):
        self.reader.messages[-1]['text']='请发一份简历'
        return self.reader.messages[-1]

    def receipt(self, mid='300', timestamp=3):
        return dict(message_id=mid,timestamp=timestamp,from_uid='7',body_type=12,text='',
                    hyperLink={'text':'您的附件简历 示例候选人简历 已发送给Boss点击查看附件'})

    def test_text_resume_verified_control_sends_once_and_retains_platform_actor(self):
        self.resume_request()
        result=self.engine.process(deepcopy(ROW))
        self.assertEqual(result['status'],'verified_sent')
        self.engine.process(deepcopy(ROW))
        self.assertEqual(len(self.bridge.sends()),1)
        payload=self.bridge.sends()[0]
        self.assertEqual((payload['kind'],payload['request_mode'],payload['request_id'],payload['attachment_name']),
                         ('resume','text','200','示例候选人简历'))
        app=read_master(self.root)['applications'][0]
        self.assertEqual(app['followup_actions'][-1]['status'],'verified_sent')
        self.assertEqual(next(m for m in app['conversation_messages'] if m['body_type']==12)['actor'],'platform')
        self.assertEqual(self.planner.calls,[])

    def test_false_text_gate_preserves_card_path(self):
        data=json.loads(self.master_path.read_text())
        data['ongoing_search_authorization']['resume_request_authorization']['text_resume_ui_verified']=False
        self.master_path.write_text(json.dumps(data))
        self.resume_request()
        self.assertEqual(self.engine.process(deepcopy(ROW))['status'],'awaiting_user')
        self.assertEqual(self.bridge.sends(),[])
        self.reader.messages.append(dict(message_id='202',timestamp=3,from_uid='7',body_type=7,
                                         dialog={'text':'我想要一份您的附件简历，您是否同意','operated':False}))
        self.assertEqual(self.engine.process(deepcopy(ROW))['status'],'verified_sent')
        self.assertEqual(self.bridge.sends()[0]['request_mode'],'card')

    def test_existing_official_receipt_is_observed_even_in_known_ids_no_send(self):
        self.resume_request()
        self.reader.messages.append(self.receipt())
        data=json.loads(self.master_path.read_text())
        data['reply_monitor']['baseline'][0]['known_message_ids'].append('300')
        data['ongoing_search_authorization']['resume_request_authorization']['text_resume_ui_verified']=False
        self.master_path.write_text(json.dumps(data))
        result=self.engine.process(deepcopy(ROW))
        self.assertEqual(result['status'],'observed_sent')
        self.assertEqual(self.bridge.sends(),[])
        self.assertEqual([c['kind'] for c in self.bridge.calls],['verify'])
        action=read_master(self.root)['applications'][0]['followup_actions'][-1]
        self.assertEqual(action['actor'],'user_account_not_agent')

    def test_receipt_during_final_preclick_read_prevents_duplicate(self):
        self.resume_request()
        def manual_send(count):
            if count==3:self.reader.messages.append(self.receipt())
        self.reader.on_history=manual_send
        self.assertEqual(self.engine.process(deepcopy(ROW))['status'],'observed_sent')
        self.assertEqual(self.bridge.sends(),[])

    def test_manual_resume_action_waits_for_receipt_without_sending_again(self):
        self.resume_request()
        self.reader.messages.append(dict(message_id='300',timestamp=3,from_uid=SELF_UID,
                                         body_type=4,text='',action={'aid':38}))
        result=self.engine.process(deepcopy(ROW))
        self.assertEqual(result['status'],'uncertain')
        self.assertEqual(self.bridge.calls,[])
        action=read_master(self.root)['applications'][0]['followup_actions'][-1]
        self.assertEqual(action['actor'],'user_account_not_agent')
        self.assertEqual(action['observed_send_action_id'],'300')
        self.assertEqual(action['observed_send_action_timestamp'],3)
        self.assertNotIn('sent_at',action)
        self.assertNotIn('200',read_master(self.root)['reply_monitor']['baseline'][0]['known_message_ids'])

    def test_manual_resume_action_during_final_read_stops_before_bridge(self):
        self.resume_request()
        def manual_send(count):
            if count==3:
                self.reader.messages.append(dict(message_id='300',timestamp=3,from_uid=SELF_UID,
                                                 body_type=4,text='',action={'aid':38}))
        self.reader.on_history=manual_send
        self.assertEqual(self.engine.process(deepcopy(ROW))['status'],'uncertain')
        self.assertEqual(self.bridge.calls,[])
        action=read_master(self.root)['applications'][0]['followup_actions'][-1]
        self.assertEqual(action['actor'],'user_account_not_agent')
        self.assertEqual(action['observed_send_action_id'],'300')

    def test_manual_pending_resume_survives_restart_then_only_verifies_receipt(self):
        self.resume_request()
        self.reader.messages.append(dict(message_id='300',timestamp=3,from_uid=SELF_UID,
                                         body_type=4,text='',action={'aid':38}))
        self.assertEqual(self.engine.process(deepcopy(ROW))['status'],'uncertain')
        self.reader.messages.append(dict(message_id='310',timestamp=4,from_uid='7',body_type=1,text='你做过什么项目？'))
        restarted=ReplyEngine(self.root,self.reader,self.planner,FakeVerifier(),self.bridge)
        restarted.allowed=self.engine.allowed
        self.assertEqual(restarted.process(deepcopy(ROW))['status'],'uncertain')
        self.assertEqual(self.bridge.calls,[])
        self.reader.messages.append(self.receipt('320',5))
        self.assertEqual(restarted.process(deepcopy(ROW))['status'],'retry_remaining')
        self.assertEqual([c['kind'] for c in self.bridge.calls],['verify'])
        action=read_master(self.root)['applications'][0]['followup_actions'][-1]
        self.assertEqual((action['status'],action['actor']),('observed_sent','user_account_not_agent'))
        self.assertEqual(action['observed_send_action_id'],'300')
        self.assertNotIn('310',read_master(self.root)['reply_monitor']['baseline'][0]['known_message_ids'])

    def test_later_request_withdrawal_preserves_followup_without_sending_attachment(self):
        self.resume_request()
        self.reader.messages.append(dict(message_id='201',timestamp=3,from_uid='7',body_type=1,text='先不用发了'))
        self.assertEqual(self.engine.process(deepcopy(ROW))['status'],'retry_remaining')
        self.assertEqual(self.bridge.sends(),[])
        baseline=read_master(self.root)['reply_monitor']['baseline'][0]['known_message_ids']
        self.assertIn('200',baseline)
        self.assertNotIn('201',baseline)

    def test_later_request_for_different_file_stops_existing_resume_send(self):
        self.resume_request()
        self.reader.messages.append(dict(message_id='201',timestamp=3,from_uid='7',body_type=1,text='请发英文版简历'))
        self.assertEqual(self.engine.process(deepcopy(ROW))['status'],'retry_remaining')
        self.assertEqual(self.bridge.sends(),[])
        self.assertNotIn('201',read_master(self.root)['reply_monitor']['baseline'][0]['known_message_ids'])

    def test_ui_history_change_is_known_no_click_then_manual_action_only_reconciles(self):
        self.resume_request()
        self.bridge.result={'status':'not_sent','clicked':False,'reason':'RESUME_HISTORY_CHANGED'}
        result=self.engine.process(deepcopy(ROW))
        self.assertEqual(result['status'],'authorized_pending_send')
        self.assertTrue(result['execution_not_started'])
        self.assertEqual(read_master(self.root)['reply_monitor']['notification_events'],[])
        self.reader.messages.append(dict(message_id='300',timestamp=3,from_uid=SELF_UID,
                                         body_type=4,text='',action={'aid':38}))
        self.bridge.result=None
        self.assertEqual(self.engine.process(deepcopy(ROW))['status'],'uncertain')
        self.assertEqual(len(self.bridge.sends()),1)
        self.assertFalse(self.engine.events.pending()[0]['ui_result']['clicked'])

    def test_resume_unknown_is_persistent_across_changed_incoming_batch(self):
        self.resume_request()
        self.bridge.result={'status':'uncertain','reason':'unknown','clicked':True}
        self.assertEqual(self.engine.process(deepcopy(ROW))['status'],'uncertain')
        self.reader.messages.append(dict(message_id='250',timestamp=3,from_uid='7',body_type=1,text='方便吗？'))
        self.assertEqual(self.engine.process(deepcopy(ROW))['status'],'uncertain')
        self.assertEqual(len(self.bridge.sends()),1)
        app=read_master(self.root)['applications'][0]
        self.assertEqual(len(app['followup_actions']),1)
        self.assertEqual(app['followup_actions'][0]['status'],'uncertain')
        self.assertNotIn('250',read_master(self.root)['reply_monitor']['baseline'][0]['known_message_ids'])

    def test_unknown_resume_recovers_from_master_without_event_journal(self):
        self.resume_request()
        data=json.loads(self.master_path.read_text())
        data['applications'][0]['followup_actions']=[dict(action_id='old',type='resume',status='uncertain',
                         request_id='200',attachment_name='示例候选人简历')]
        self.master_path.write_text(json.dumps(data))
        self.assertEqual(self.engine.process(deepcopy(ROW))['status'],'uncertain')
        self.assertEqual(self.bridge.sends(),[])

    def test_compound_resume_request_cannot_consume_interview_question(self):
        self.reader.messages[-1]['text']='发一份简历，周二可以面试吗？'
        self.planner.plan=lambda context: dict(kind='resume',text='',reason='索要简历',evidence=[],notification='none',question_for_user='')
        self.assertEqual(self.engine.process(deepcopy(ROW))['status'],'awaiting_user')
        self.assertEqual(self.bridge.sends(),[])

    def test_text_request_with_separate_question_retries_remaining(self):
        self.resume_request()
        self.reader.messages.append(dict(message_id='201',timestamp=3,from_uid='7',body_type=1,text='你做过什么项目？'))
        self.assertEqual(self.engine.process(deepcopy(ROW))['status'],'retry_remaining')
        self.assertNotIn('201',read_master(self.root)['reply_monitor']['baseline'][0]['known_message_ids'])
        self.engine.process(deepcopy(ROW))
        self.assertEqual([c['kind'] for c in self.bridge.sends()],['resume','text'])

    def test_old_receipt_and_text_spoof_do_not_prove_delivery(self):
        request=self.resume_request()
        payload=self.engine.payload(ROW,read_master(self.root)['applications'][0],self.reader.messages,
                                    {'kind':'resume'},request['message_id'],'text')
        self.reader.messages.extend([self.receipt('150',1),dict(message_id='300',timestamp=3,body_type=1,
                                   from_uid='7',text='您的附件简历 示例候选人简历 已发送给Boss点击查看附件')])
        event=self.engine.events.enqueue('uncertain-resume',uid='7',row=deepcopy(ROW),payload=payload,status='uncertain')
        self.assertEqual(self.engine.reconcile(event)['status'],'uncertain')
        self.assertEqual(self.bridge.calls,[])

    def test_text_gate_revoked_immediately_before_click_is_respected(self):
        self.resume_request()
        def revoke(count):
            if count==3:
                data=json.loads(self.master_path.read_text())
                data['ongoing_search_authorization']['resume_request_authorization']['text_resume_ui_verified']=False
                self.master_path.write_text(json.dumps(data))
        self.reader.on_history=revoke
        self.assertEqual(self.engine.process(deepcopy(ROW))['status'],'authorized_pending_send')
        self.assertEqual(self.bridge.sends(),[])

    def test_user_reply_while_drafting_prevents_send(self):
        def after_first_read(count):
            if count == 2:
                self.reader.messages.append(dict(message_id='201', timestamp=3, from_uid=SELF_UID,
                                                 body_type=1, text='本人刚刚已答复。'))
        self.reader.on_history = after_first_read
        self.assertEqual(self.engine.process(deepcopy(ROW))['status'], 'superseded')
        self.assertEqual(self.bridge.calls, [])

    def test_verified_reply_is_sent_once(self):
        self.assertEqual(self.engine.process(deepcopy(ROW))['status'], 'verified_sent')
        self.engine.process(deepcopy(ROW))
        self.assertEqual(len(self.bridge.sends()), 1)
        data = read_master(self.root)
        sent = next(message for message in data['applications'][0]['conversation_messages'] if message['message_id'] == '500')
        self.assertEqual(sent['actor'], 'agent')

    def test_dispatch_after_restart_only_reconciles_without_guessing_actor(self):
        payload = self.engine.payload(ROW, read_master(self.root)['applications'][0], [OLD, NEW], REPLY)
        key = event_key(ROW, [NEW])
        events = EventStore(self.root)
        events.enqueue(key, uid='7', row=deepcopy(ROW), payload=payload, status='dispatching',
                       action={'action_id': key, 'text': REPLY['text'], 'actor': 'agent'})
        self.reader.messages.append(dict(message_id='500', timestamp=5, from_uid=SELF_UID, body_type=1, text=REPLY['text']))
        result = self.engine.reconcile(events.get(key))
        self.assertEqual(result['status'], 'observed_sent')
        self.assertEqual(self.bridge.sends(), [])
        found = next(message for message in read_master(self.root)['applications'][0]['conversation_messages'] if message['message_id'] == '500')
        self.assertNotEqual(found['actor'], 'agent')

    def test_uncertain_dispatch_never_resends(self):
        payload = self.engine.payload(ROW, read_master(self.root)['applications'][0], [OLD, NEW], REPLY)
        key = event_key(ROW, [NEW])
        events = EventStore(self.root)
        events.enqueue(key, uid='7', row=deepcopy(ROW), payload=payload, status='uncertain')
        result = self.engine.reconcile(events.get(key))
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(self.bridge.calls, [])
        self.assertEqual(len(read_master(self.root)['reply_monitor']['notification_events']), 1)

    def test_pause_during_planning_prevents_send(self):
        self.planner.callback = lambda: self.state.control('pause', self.state.status()['revision'])
        self.engine.process(deepcopy(ROW))
        self.assertEqual(self.bridge.calls, [])

    def test_pause_after_dispatch_intent_before_bridge_prevents_click(self):
        original_update = self.engine.state.update
        def pause_at_dispatch(**fields):
            result = original_update(**fields)
            if (fields.get('in_flight') or {}).get('phase') == 'dispatching':
                self.state.control('pause', self.state.status()['revision'])
            return result
        self.engine.state.update = pause_at_dispatch
        self.engine.process(deepcopy(ROW))
        self.assertEqual(self.bridge.sends(), [])

    def test_resume_does_not_consume_simultaneous_unanswered_question(self):
        request = dict(message_id='202', timestamp=3, from_uid='7', body_type=7,
                       text='', dialog={'text': '我想要一份您的附件简历，您是否同意', 'operated': False})
        self.reader.messages.append(request)
        self.engine.process(deepcopy(ROW))
        baseline = read_master(self.root)['reply_monitor']['baseline'][0]
        # Either answer the text question too, or retain its pending baseline.
        text_answered = any(call['kind'] == 'text' for call in self.bridge.sends())
        self.assertTrue(text_answered or '200' not in baseline['known_message_ids'])
        self.engine.process(deepcopy(ROW))
        self.assertEqual([call['kind'] for call in self.bridge.sends()], ['resume', 'text'])

    def test_official_safety_block_stops_whole_run(self):
        self.bridge.result = dict(status='blocked', clicked=False, reason='OFFICIAL_SAFETY_CHECK')
        with self.assertRaises(BossBlocked):
            self.engine.process(deepcopy(ROW))

    def test_official_safety_block_during_verification_stops_run(self):
        original_bridge = self.engine.bridge
        def verification_block(payload):
            if payload['kind'] == 'verify':
                return dict(status='blocked', clicked=False, reason='OFFICIAL_SAFETY_CHECK')
            return original_bridge(payload)
        self.engine.bridge = verification_block
        with self.assertRaises(BossBlocked):
            self.engine.process(deepcopy(ROW))

    def test_browser_connection_failure_stops_whole_run(self):
        self.bridge.result = dict(status='blocked', clicked=False, reason='BROWSER_OPERATION_FAILED')
        with self.assertRaises(BossBlocked):
            self.engine.process(deepcopy(ROW))

    def test_unknown_message_sender_is_not_assumed_to_be_recruiter(self):
        self.reader.messages[-1]['from_uid'] = ''
        with self.assertRaises(BossBlocked):
            self.engine.process(deepcopy(ROW))
        self.assertEqual(self.bridge.sends(), [])
        self.assertNotIn('200', read_master(self.root)['reply_monitor']['baseline'][0]['known_message_ids'])

    def test_wrong_job_identity_fails_closed(self):
        row = deepcopy(ROW)
        row['job_id'] = 'different-job'
        with self.assertRaises(BossBlocked):
            self.engine.process(row)
        self.assertEqual(self.bridge.calls, [])


if __name__ == '__main__':
    unittest.main()
