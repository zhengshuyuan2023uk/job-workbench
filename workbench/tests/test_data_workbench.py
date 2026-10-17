from test_support import ConfiguredTestCase, SELF_UID, SELF_NAME, RECIPIENT
"""Read-only projection tests for current workbench decisions and old evidence."""
import copy
import unittest

from test_data import application, fixture, NOW
from data_model import build_view


class WorkbenchDataTests(ConfiguredTestCase):
    def setUp(self):
        self.sources = fixture()
        self.app = application()
        self.app.update(conversation_uid='123', conversation_messages=[
            {'message_id': '100', 'kind': 'incoming_text', 'text': '旧来信', 'timestamp': 1792060000000},
            {'message_id': '200', 'kind': 'incoming_text', 'text': '新问题', 'timestamp': 1792060001000}])
        self.sources['applications']['applications'] = [self.app]
        self.monitor = self.sources['applications']['reply_monitor']

    def item(self, status, **values):
        item = dict(action_id='workbench:a:123', uid='123', row={'job_id': 'a', 'uid': '123'},
                    kind='reply', status=status, incoming_ids=['100'], reason='当前分析',
                    question_for_user='', text='')
        item.update(values)
        self.monitor['workbench_tasks'] = {'status': 'completed', 'items': [item]}
        return item

    def view(self):
        return build_view(self.sources, NOW)

    def test_completed_items_cover_only_exact_ids_and_leave_sources_unchanged(self):
        for status in ('ignored', 'verified_sent', 'observed_sent', 'superseded'):
            with self.subTest(status=status):
                self.item(status)
                before = copy.deepcopy(self.sources)
                view = self.view()
                self.assertEqual([(t['kind'], t['incoming_message_ids'], t['detail']) for t in view['tasks']],
                                 [('incoming', ['200'], '新问题')])
                self.assertEqual(self.sources, before)

    def test_wrong_uid_or_job_does_not_hide_messages(self):
        for changes in ({'uid': 'other'}, {'row': {'job_id': 'other', 'uid': '123'}},
                        {'row': {'job_id': 'a', 'uid': 'other'}}):
            with self.subTest(changes=changes):
                self.item('ignored', **changes)
                self.assertEqual(self.view()['tasks'][0]['incoming_message_ids'], ['100', '200'])

    def test_same_job_and_message_id_in_two_conversations_stays_separate(self):
        second = copy.deepcopy(self.app)
        second['conversation_uid'] = '456'
        self.sources['applications']['applications'].append(second)
        self.item('ignored', incoming_ids=['100', '200'])
        incoming = [t for t in self.view()['tasks'] if t['kind'] == 'incoming']
        self.assertEqual([(t['conversation_uid'], t['incoming_message_ids']) for t in incoming], [('456', ['100', '200'])])

    def test_awaiting_user_replaces_matching_notification_once_and_keeps_other_question(self):
        self.monitor['notification_events'] = [
            {'event_id': 'old-mail', 'job_id': 'a', 'conversation_uid': '123',
             'incoming_message_ids': ['100'], 'requires_user_review': True,
             'analysis': '邮件旧说明', 'draft_text': '旧稿', 'draft_version': 'v1', 'send_status': 'uncertain'},
            {'event_id': 'another-mail', 'job_id': 'a', 'conversation_uid': '123',
             'incoming_message_ids': ['200'], 'requires_user_review': True, 'analysis': '仍需确认新问题'}]
        self.item('awaiting_user', kind='escalate', reason='现有事实不足', question_for_user='你实际做过哪些环节？')
        view = self.view()
        questions = [t for t in view['tasks'] if t['kind'] == 'user_input']
        self.assertEqual(len(questions), 2)
        current = next(t for t in questions if t['incoming_message_ids'] == ['100'])
        self.assertEqual(current['detail'], '现有事实不足\n你实际做过哪些环节？')
        self.assertEqual(current['uncertain_fields'], ['你实际做过哪些环节？'])
        self.assertEqual(view['summary']['pending_replies'], 2)
        self.assertTrue(any(t['kind'] == 'uncertain_notification' for t in view['tasks']))
        self.assertFalse(any(t['kind'] == 'reply' for t in view['tasks']))

    def test_notification_for_different_uid_is_not_resolved_by_current_item(self):
        self.monitor['notification_events'] = [{'job_id': 'a', 'conversation_uid': '456',
            'incoming_message_ids': ['100'], 'requires_user_review': True, 'analysis': '另一会话仍待确认'}]
        self.item('verified_sent')
        self.assertTrue(any(t['kind'] == 'user_input' and t['conversation_uid'] == '456' for t in self.view()['tasks']))

    def test_ready_and_uncertain_keep_their_status_even_after_run_failure(self):
        self.item('ready', text='已整理的真实回复')
        self.monitor['workbench_tasks']['status'] = 'failed'
        view = self.view()
        current = next(t for t in view['tasks'] if t['incoming_message_ids'] == ['100'])
        self.assertEqual(current['kind'], 'incoming')
        self.assertIn('一键跟进', current['title'])
        self.assertEqual(current['draft_text'], '已整理的真实回复')
        self.assertEqual(view['summary']['pending_replies'], 0)
        self.item('uncertain', incoming_ids=[], reason='本轮没有覆盖这个联系人')
        self.assertTrue(any(t['kind'] == 'uncertain_send' and t['detail'] == '本轮没有覆盖这个联系人' for t in self.view()['tasks']))

    def test_first_failed_collection_does_not_mean_zero_pending_messages(self):
        self.monitor['workbench_tasks'] = {'status': 'failed', 'items': []}
        self.assertEqual(self.view()['tasks'][0]['incoming_message_ids'], ['100', '200'])

    def test_changed_job_identity_keeps_unknown_item_without_hiding_old_messages(self):
        self.item('uncertain', row={'job_id': 'changed-job', 'uid': '123'}, incoming_ids=[],
                  reason='同一联系人岗位变化，需要先核对身份')
        tasks = self.view()['tasks']
        self.assertEqual(tasks[0]['incoming_message_ids'], ['100', '200'])
        self.assertEqual(tasks[-1]['kind'], 'uncertain_send')
        self.assertEqual(tasks[-1]['job_id'], 'a')
        self.assertIn('岗位变化', tasks[-1]['detail'])

    def test_explicit_completed_action_also_resolves_exact_request(self):
        self.app['followup_actions'] = [{'type': 'resume', 'status': 'observed_sent',
            'conversation_uid': '123', 'incoming_message_ids': ['100']}]
        self.monitor['notification_events'] = [{'job_id': 'a', 'conversation_uid': '123',
            'incoming_message_ids': ['100'], 'requires_user_review': True, 'analysis': '已被处理的旧事项'}]
        self.assertEqual(self.view()['tasks'][0]['incoming_message_ids'], ['200'])
        self.assertEqual(self.view()['summary']['pending_replies'], 0)
        self.app['followup_actions'][0]['conversation_uid'] = '456'
        self.assertEqual(self.view()['summary']['pending_replies'], 1)

    def test_notification_subject_is_plain_text_fallback(self):
        self.app['conversation_messages'] = []
        self.monitor['notification_events'] = [{'job_id': 'a', 'incoming_message_ids': ['100'],
            'requires_user_review': True, 'subject': '邀请进一步交流，需确认微信跟进',
            'email_body_html': '<b>不解析邮件 HTML</b>', 'draft_text': '尚未发送的建议'}]
        pending = self.view()['tasks'][0]
        self.assertEqual(pending['detail'], '邀请进一步交流，需确认微信跟进')
        self.assertEqual(pending['draft_text'], '尚未发送的建议')
        self.assertIsNone(pending['draft_version'])

    def request_and_receipt(self):
        self.app['conversation_messages'][0].update(text='您好，简历方便发一下吗~', body_type=1)
        receipt = {'message_id': '150', 'timestamp': 1792060000500, 'kind': 'platform_event',
                   'actor': 'platform', 'body_type': 1, 'text': '对方已同意，您的附件简历已发送给对方'}
        self.app['conversation_messages'].append(receipt)
        return receipt

    def test_official_resume_receipt_resolves_request_but_keeps_next_question(self):
        self.request_and_receipt()
        view = self.view()
        self.assertEqual(view['tasks'][0]['incoming_message_ids'], ['200'])
        self.assertEqual(len(view['jobs'][0]['messages']), 3)

    def test_empty_card_wrong_identity_old_receipt_and_recruiter_claim_do_not_resolve(self):
        for change in ({'text': '', 'body_type': 12}, {'conversation_uid': '456'},
                       {'message_id': '90', 'timestamp': 1792059999000}, {'actor': 'recruiter', 'kind': 'incoming_text'}):
            with self.subTest(change=change):
                self.app['conversation_messages'] = self.app['conversation_messages'][:2]
                receipt = self.request_and_receipt()
                receipt.update(change)
                incoming_ids = {mid for t in self.view()['tasks'] if t['kind'] == 'incoming' for mid in t['incoming_message_ids']}
                self.assertIn('100', incoming_ids)

    def test_resume_message_with_additional_question_is_not_consumed_by_receipt(self):
        self.request_and_receipt()
        self.app['conversation_messages'][0]['text'] = '请发简历，明天可以面试吗？'
        self.assertIn('100', self.view()['tasks'][0]['incoming_message_ids'])

    def test_official_card_receipt_and_later_request_have_ordering(self):
        receipt = self.request_and_receipt()
        receipt.update(body_type=12, text='', hyperLink={'text': '您的附件简历 示例候选人简历 已发送给Boss点击查看附件'})
        self.app['conversation_messages'][1].update(body_type=1, text='请发一份简历')
        self.assertEqual(self.view()['tasks'][0]['incoming_message_ids'], ['200'])


if __name__ == '__main__':
    unittest.main()
