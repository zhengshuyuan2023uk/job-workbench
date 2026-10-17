from test_support import ConfiguredTestCase, SELF_UID, SELF_NAME, RECIPIENT
"""No real email: all Codex executions return explicit synthetic tool receipts."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest

from mail_notifier import MailNotifier, inspect_receipts
from master_record import read_master, transaction


def call(tool, arguments, structured, *, alias='structured_content', status='completed', error=None):
    return {'type': 'item.completed', 'item': {'type': 'mcp_tool_call', 'server': 'codex_apps',
        'tool': tool, 'arguments': arguments, 'result': {alias: structured}, 'status': status, 'error': error}}


def trace(event, *, ids=None, send=True, recipient=RECIPIENT, account=RECIPIENT):
    result = [
        call('gmail.get_profile', {}, {'email': account}),
        call('gmail.search_email_ids', {'query': f'in:sent to:{RECIPIENT} subject:"{event["stable_marker"]}"'}, {'ids': ids or []}),
    ]
    if send:
        result.append(call('gmail.send_email', {'to': recipient, 'subject': event['subject'],
            'payload': {'mime_type': 'text/plain', 'body': event['body']}}, {'id': 'aabbcc1122334455'}))
    return result


class ReceiptTests(ConfiguredTestCase):
    def setUp(self):
        self.event = {'event_id': 'job:message', 'stable_marker': 'BOSS-EVENT-aabbcc',
                      'subject': 'BOSS求职 BOSS-EVENT-aabbcc', 'body': '这是已授权的本人通知。'}

    def inspect(self, events, allow=True):
        return inspect_receipts('\n'.join(json.dumps(item) for item in events), self.event, allow_new_send=allow)

    def test_send_requires_real_profile_search_and_send_receipt(self):
        result = self.inspect(trace(self.event))
        self.assertEqual(result['status'], 'sent')
        self.assertEqual(result['email_id'], 'aabbcc1122334455')

    def test_actual_gmail_mime_body_schema(self):
        events=trace(self.event)
        payload=events[-1]['item']['arguments']['payload']
        payload['body']={'content':self.event['body']}
        self.assertEqual(self.inspect(events)['status'],'sent')
        payload['parts']=[{'body':{'content':'unrelated'}}]
        self.assertEqual(self.inspect(events)['status'],'uncertain')

    def test_final_model_json_is_not_a_receipt(self):
        forged = {'type': 'item.completed', 'item': {'type': 'agent_message',
            'text': json.dumps({'status': 'sent', 'message_id': 'fake', 'account': RECIPIENT})}}
        self.assertEqual(self.inspect([forged])['status'], 'uncertain')

    def test_existing_sent_id_can_reconcile_without_send(self):
        result = self.inspect(trace(self.event, ids=['1234abcdef1234'], send=False), allow=False)
        self.assertEqual(result['status'], 'already_sent')

    def test_uncertain_run_with_no_sent_match_stays_uncertain(self):
        result = self.inspect(trace(self.event, send=False), allow=False)
        self.assertEqual(result['status'], 'uncertain')

    def test_reconciliation_run_cannot_claim_fresh_send(self):
        self.assertEqual(self.inspect(trace(self.event), allow=False)['status'], 'uncertain')

    def test_wrong_account_and_wrong_to_rejected(self):
        self.assertNotEqual(self.inspect(trace(self.event, account='other@example.com'))['status'], 'sent')
        self.assertNotEqual(self.inspect(trace(self.event, recipient='other@example.com'))['status'], 'sent')

    def test_cc_bcc_attachments_and_wrong_content_rejected(self):
        for change in ({'cc': 'other@example.com'}, {'bcc': ['other@example.com']},
                       {'attachments': ['file']}, {'subject': 'wrong'},
                       {'payload': {'mime_type': 'text/plain', 'body': 'wrong'}}):
            events = trace(self.event)
            events[-1]['item']['arguments'].update(change)
            with self.subTest(change=change):
                self.assertNotEqual(self.inspect(events)['status'], 'sent')

    def test_search_must_be_exact_sent_marker_query(self):
        for query in (self.event['stable_marker'], 'in:inbox ' + self.event['stable_marker'],
                      f'in:sent to:{RECIPIENT} subject:"{self.event["stable_marker"]}" OR in:inbox'):
            events = trace(self.event)
            events[1]['item']['arguments']['query'] = query
            self.assertNotEqual(self.inspect(events)['status'], 'sent')

    def test_failed_or_started_tool_call_is_not_proof(self):
        for changes in ({'error': {'message': 'failed'}}, {'status': 'in_progress'}):
            events = trace(self.event)
            events[-1]['item'].update(changes)
            self.assertNotEqual(self.inspect(events)['status'], 'sent')

    def test_structured_content_alias_and_underscore_tool_name(self):
        events = trace(self.event)
        for event in events:
            item = event['item']
            item['tool'] = 'mcp__codex_apps__' + item['tool'].replace('.', '_')
            item['result']['structuredContent'] = item['result'].pop('structured_content')
        self.assertEqual(self.inspect(events)['status'], 'sent')

    def test_nested_messages_ids_supported(self):
        events = trace(self.event, send=False)
        events[1]['item']['result']['structured_content'] = {'messages': [{'id': 'a123456789abcd'}]}
        self.assertEqual(self.inspect(events, allow=False)['status'], 'already_sent')

    def test_send_before_profile_or_search_rejected(self):
        events = trace(self.event)
        self.assertNotEqual(self.inspect([events[-1], *events[:2]])['status'], 'sent')


class DeliveryTests(ConfiguredTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'applications.json').write_text(json.dumps({'applications': [], 'reply_monitor': {'notification_events': []}}))

    def tearDown(self):
        self.temp.cleanup()

    def notifier(self, events_factory, *, code=0, seen=None):
        def run(command, **kwargs):
            payload = json.loads(kwargs['input'].split('\n---MATERIAL---\n')[1])
            if seen is not None:
                seen.append(payload)
            for event in events_factory(payload):
                kwargs['stdout'].write(json.dumps(event) + '\n')
            kwargs['stderr'].write('stderr diagnostic, not JSON\n')
            output = Path(command[command.index('--output-last-message') + 1])
            output.write_text(json.dumps({'status': 'sent', 'message_id': 'forged-final', 'account': RECIPIENT, 'error': ''}))
            return SimpleNamespace(returncode=code)
        return MailNotifier(self.root, runner=run)

    def test_real_tool_receipt_overrides_wrong_final_id(self):
        notifier = self.notifier(lambda material: trace(material))
        event = notifier.enqueue('event1', 'needs_user_input', '测试公司', '待确认问题。')
        result = notifier.deliver(event)
        self.assertEqual(result['status'], 'sent')
        self.assertEqual(result['email_id'], 'aabbcc1122334455')

    def test_final_only_result_does_not_mark_sent(self):
        notifier = self.notifier(lambda material: [])
        event = notifier.enqueue('event2', 'needs_user_input', '测试公司', '待确认问题。')
        self.assertEqual(notifier.deliver(event)['status'], 'uncertain')

    def test_stale_pending_input_cannot_enable_resend(self):
        seen = []
        notifier = self.notifier(lambda material: trace(material, send=False), seen=seen)
        event = notifier.enqueue('event3', 'needs_user_input', '测试公司', '待确认问题。')
        transaction(self.root, lambda data: data['reply_monitor']['notification_events'][0].update(status='uncertain'))
        self.assertEqual(notifier.deliver(event)['status'], 'uncertain')
        self.assertIs(seen[0]['allow_new_send'], False)

    def test_first_pending_intent_allows_exactly_first_send_attempt(self):
        seen = []
        notifier = self.notifier(lambda material: trace(material, send=False), seen=seen)
        event = notifier.enqueue('event4', 'needs_user_input', '测试公司', '待确认问题。')
        notifier.deliver(event)
        notifier.deliver(event)
        self.assertEqual([item['allow_new_send'] for item in seen], [True, False])

    def test_nonzero_exit_after_gmail_receipt_is_still_confirmed(self):
        notifier = self.notifier(lambda material: trace(material), code=1)
        event = notifier.enqueue('event5', 'needs_user_input', '测试公司', '待确认问题。')
        self.assertEqual(notifier.deliver(event)['status'], 'sent')

    def test_already_sent_is_not_reexecuted(self):
        seen = []
        notifier = self.notifier(lambda material: trace(material), seen=seen)
        event = notifier.enqueue('event6', 'needs_user_input', '测试公司', '待确认问题。')
        notifier.deliver(event)
        notifier.deliver(event)
        self.assertEqual(len(seen), 1)

    def test_concurrent_reconciliation_never_downgrades_verified_send(self):
        seen=[]
        notifier=self.notifier(lambda material: trace(material,send=material['allow_new_send']),seen=seen)
        event=notifier.enqueue('concurrent', 'needs_user_input', '测试公司', '待确认问题。')
        first_entered=threading.Event(); second_entered=threading.Event(); first_finished=threading.Event()
        original=notifier.runner
        def runner(command,**kwargs):
            material=json.loads(kwargs['input'].split('\n---MATERIAL---\n')[1])
            if material['allow_new_send']:
                first_entered.set()
                if not second_entered.wait(2): raise RuntimeError('missing reconciliation')
            else:
                second_entered.set()
                if not first_finished.wait(2): raise RuntimeError('missing first completion')
            return original(command,**kwargs)
        notifier.runner=runner
        results=[]
        def first():
            results.append(notifier.deliver(event))
            first_finished.set()
        one=threading.Thread(target=first)
        two=threading.Thread(target=lambda: results.append(notifier.deliver(event)))
        one.start()
        self.assertTrue(first_entered.wait(2))
        two.start()
        one.join(3); two.join(3)
        self.assertFalse(one.is_alive()); self.assertFalse(two.is_alive())
        self.assertEqual(sorted(item['allow_new_send'] for item in seen), [False, True])
        self.assertTrue(all(item['status']=='sent' for item in results))
        self.assertEqual(read_master(self.root)['reply_monitor']['notification_events'][0]['status'], 'sent')


if __name__ == '__main__':
    unittest.main()
