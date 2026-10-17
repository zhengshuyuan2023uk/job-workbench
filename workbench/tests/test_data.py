from test_support import ConfiguredTestCase, SELF_UID, SELF_NAME, RECIPIENT
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data_model import build_view
from store import WorkspaceStore
from source_manifest import SOURCES

NOW = datetime(2026, 10, 15, 20, 0, tzinfo=ZoneInfo('Asia/Shanghai'))


def fixture():
    return {**{key: {} for key in SOURCES}, 'applications': {'applications': [], 'reply_monitor': {
        'last_checked_at': '2026-10-15T19:50:00+08:00', 'interval_minutes': 30,
        'notification_events': []}, 'daily_contact_audits': {}},
        'guangzhou': {}, 'second': {}, 'cities': {}}


def application(job_id='a'):
    return {'job_id': job_id, 'company': '示例公司', 'title': 'AI应用',
            'status': 'verified_sent', 'monitoring_enabled': True,
            'attempted_at': '2026-10-15T18:00:00+08:00',
            'server_verified_at': '2026-10-15T18:02:00+08:00',
            'server_read_status': '未读', 'conversation_observations': []}


def write_sources(root, sources):
    for key, name in SOURCES.items():
        (root / name).write_text(json.dumps(sources[key], ensure_ascii=False))


class DataTests(ConfiguredTestCase):
    def setUp(self):
        self.sources = fixture()
        self.sources['applications']['applications'] = [application()]

    def view(self):
        return build_view(self.sources, NOW)

    def test_latest_observation_wins(self):
        self.sources['applications']['applications'][0]['conversation_observations'] = [
            {'observed_at': '2026-10-15T19:00:00+08:00', 'read_status': '已读'}]
        self.assertEqual(self.view()['jobs'][0]['read_status'], 'read')

    def test_resolved_connection_is_not_a_pending_user_task(self):
        self.sources['applications']['reply_monitor']['last_connection_issue']={'status':'resolved','message':'已复查成功'}
        self.assertFalse(any(t['kind']=='connection' for t in self.view()['tasks']))

    def test_resident_user_question_keeps_company_and_message_context(self):
        self.sources['applications']['reply_monitor']['notification_events']=[{
            'event_id':'reply:a:123','job_id':'a','incoming_message_ids':['123'],
            'requires_user_review':True,'analysis':'noon经历尚未确认',
            'uncertain_fields':['你独立完成过哪些环节？'],
            'status':'sent','send_status':'sent','observed_at':'2026-10-15T19:55:00+08:00'}]
        tasks=self.view()['tasks']
        question=next(t for t in tasks if t['kind']=='user_input')
        self.assertEqual(question['job_id'],'a')
        self.assertEqual(question['uncertain_fields'],['你独立完成过哪些环节？'])
        self.assertFalse(any(t['kind']=='unknown_event' for t in tasks))
        self.assertEqual(self.view()['summary']['pending_replies'],1)

    def test_resident_ignore_and_escalate_match_live_record_shapes(self):
        records=[
            ('synthetic-job-1',900000011,'示例公司甲','ignored',
             [('100001','你好，已收到你的虚构测试资料。'),
              ('100002','此次示例岗位暂不匹配。')],
             '附件简历请求已处理且对方已查看；最新来信为招聘方明确婉拒，无需补发礼貌回复。',''),
            ('synthetic-job-2',900000012,'示例公司乙','ignored',
             [('100003','示例团队已收件，后续如需补充会再联系。')],
             '来信仅确认收到简历并告知等待评估，无需回复，也无实质评估进展。',''),
            ('synthetic-job-3',900000013,'示例公司丙','awaiting_user',
             [('100004','是否做过示例平台从零搭建店铺？'),
              ('100005','范围包括账号创建、上架与日常维护。'),('100006','你个人负责过哪些步骤？')],
             '现有材料未确认独立全流程能力或noon经验，需本人补充。',
             '你对亚马逊和noon分别实际做过哪些环节？能否独立完成店铺注册、上架、出单到精细化运营？')]
        apps=[]
        for jid,uid,company,status,messages,reason,question in records:
            app=application(jid)
            app.update(conversation_uid=uid,company=company,
                conversation_messages=[{'message_id':mid,'timestamp':1792123015000+i,'from_uid':str(uid),
                    'speaker':'招聘方','text':text,'body_type':1,'kind':'incoming_text','actor':'recruiter'}
                    for i,(mid,text) in enumerate(messages)],
                followup_actions=[{'action_id':f'reply:{jid}:{uid}:event','type':'reply_decision',
                    'status':status,'incoming_message_ids':[m[0] for m in messages],
                    'reason':reason,'question_for_user':question,'text':'','version':'resident-v1',
                    'actor':'agent','created_at':'2026-10-16T12:26:09+08:00'}])
            apps.append(app)
        self.sources['applications']['applications']=apps
        pending=apps[-1]['followup_actions'][0]
        self.sources['applications']['reply_monitor']['notification_events']=[{
            'event_id':pending['action_id'],'stable_marker':'BOSS-EVENT-example','category':'needs_user_input',
            'company':apps[-1]['company'],'job_id':apps[-1]['job_id'],'conversation_uid':str(apps[-1]['conversation_uid']),
            'status':'sent','send_status':'sent','requires_user_review':True,
            'incoming_message_ids':pending['incoming_message_ids'],'analysis':pending['reason'],
            'uncertain_fields':[pending['question_for_user']],'observed_at':'2026-10-16T12:26:09+08:00'}]
        original=copy.deepcopy(self.sources)
        view=self.view()
        self.assertEqual([(t['kind'],t['job_id']) for t in view['tasks']],[('user_input',apps[-1]['job_id'])])
        self.assertEqual(view['summary']['pending_replies'],1)
        self.assertEqual(view['tasks'][0]['uncertain_fields'],[pending['question_for_user']])
        self.assertEqual(sum(len(j['messages']) for j in view['jobs']),6)
        self.assertEqual(self.sources,original)

    def test_awaiting_user_decision_without_email_still_counts_once(self):
        app=self.sources['applications']['applications'][0]
        app.update(conversation_uid=123,conversation_messages=[
            {'message_id':'in','kind':'incoming_text','text':'你个人负责过哪些步骤？？'}],
            followup_actions=[{'action_id':'reply:a:123:one','type':'reply_decision','status':'awaiting_user',
                'incoming_message_ids':['in'],'reason':'独立能力尚未确认','question_for_user':'请确认做过哪些环节',
                'text':'','version':'resident-v1','actor':'agent','created_at':'2026-10-15T19:55:00+08:00'}])
        view=self.view()
        self.assertEqual([t['kind'] for t in view['tasks']],['user_input'])
        self.assertEqual(view['summary']['pending_replies'],1)

    def test_ignored_decision_does_not_hide_new_message_or_other_identity(self):
        app=self.sources['applications']['applications'][0]
        app.update(conversation_uid=123,conversation_messages=[
            {'message_id':'old','kind':'incoming_text','text':'收到简历'},
            {'message_id':'new','kind':'incoming_text','text':'请介绍具体项目'}],
            followup_actions=[{'action_id':'reply:a:123:old','type':'reply_decision','status':'ignored',
                'incoming_message_ids':['old'],'reason':'仅确认收件','created_at':'2026-10-15T19:50:00+08:00'}])
        view=self.view()
        self.assertEqual(view['tasks'][0]['incoming_message_ids'],['new'])
        app['followup_actions'][0]['conversation_uid']=456
        self.assertEqual(self.view()['tasks'][0]['incoming_message_ids'],['old','new'])

    def test_duplicate_user_notifications_do_not_duplicate_decision_count(self):
        event={'event_id':'one','job_id':'a','incoming_message_ids':['m1'],'requires_user_review':True,
               'analysis':'需要本人补充','uncertain_fields':['请确认'],'send_status':'sent'}
        self.sources['applications']['reply_monitor']['notification_events']=[event,{**event,'event_id':'two'}]
        view=self.view()
        self.assertEqual(len(view['tasks']),1)
        self.assertEqual(view['summary']['pending_replies'],1)

    def test_user_contact_is_monitored_without_becoming_an_agent_send(self):
        manual = application('manual')
        manual.update(status='user_contact_verified', origin='user_initiated', identity_verified=True,
                      server_read_status='已读', message='默认招呼', opening_version='未发送的定制稿',
                      server_message_timestamp='2026-10-15T18:00:00+08:00',
                      conversation_messages=[{'message_id':'user-first', 'kind':'outgoing_text',
                          'actor':'user', 'text':'?', 'timestamp_iso':'2026-10-15T18:00:00+08:00'}])
        self.sources['applications']['applications'].append(manual)
        view = self.view()
        job = next(j for j in view['jobs'] if j['job_id'] == 'manual')
        self.assertEqual((job['contact_status'], job['origin']), ('user_contact_verified', 'user_initiated'))
        self.assertTrue(job['identity_verified'])
        self.assertTrue(job['monitored'])
        self.assertEqual((view['summary']['verified_sent'], view['summary']['monitored']), (1, 2))
        self.assertEqual((view['summary']['read'], view['summary']['unread']), (1, 1))
        self.assertEqual(view['summary']['daily_contacts']['confirmed'], 2)
        self.assertEqual(view['summary']['daily_contacts']['uncertain'], 0)
        self.assertEqual(job['contacted_at'], '2026-10-15T18:00:00+08:00')
        self.assertIsNone(job['sent_text'])
        self.assertIsNone(job['sent_at'])
        self.assertIsNone(job['sent_kind'])
        self.assertIsNone(job['sent_version'])
        self.assertEqual(job['drafts'], [])
        self.assertEqual((job['messages'][0]['actor'], job['messages'][0]['text']), ('user', '?'))

    def test_user_contact_requires_verified_identity_and_enabled_monitoring(self):
        manual = application('manual')
        manual.update(status='user_contact_verified', origin='user_initiated')
        self.sources['applications']['applications'].append(manual)
        for identity, enabled in ((False, True), (True, False), (False, False)):
            with self.subTest(identity=identity, enabled=enabled):
                manual.update(identity_verified=identity, monitoring_enabled=enabled)
                view = self.view()
                self.assertEqual(view['summary']['monitored'], 1)
                self.assertEqual(view['summary']['verified_sent'], 1)
                self.assertFalse(next(j for j in view['jobs'] if j['job_id'] == 'manual')['monitored'])

    def test_message_actors_are_preserved_without_guessing_legacy_sender(self):
        self.sources['applications']['applications'][0]['conversation_messages'] = [
            {'message_id':str(i), 'kind':'outgoing_text', 'text':'原话', 'actor':actor}
            for i, actor in enumerate(('user', 'agent', 'platform', 'unexpected', None))]
        self.assertEqual([m['actor'] for m in self.view()['jobs'][0]['messages']],
                         ['user', 'agent', 'platform', None, None])

    def test_superseded_opening_is_not_pending_reply(self):
        self.sources['cities'] = {'shortlist': [{'job_id': 'a'}], 'candidate_drafts': [
            {'job_id': 'a', 'version': 'v3', 'text': '旧稿', 'status': 'superseded_by_authorized_version'},
            {'job_id': 'a', 'version': 'v4', 'text': '实际稿', 'status': 'verified_sent', 'sent': True}]}
        v = self.view()
        self.assertEqual(len(v['jobs']), 1)
        self.assertEqual(v['summary']['pending_replies'], 0)
        self.assertEqual(len(v['jobs'][0]['drafts']), 2)

    def test_daily_audit_is_not_added_twice(self):
        self.sources['applications']['applications'] = [application(str(i)) for i in range(16)]
        self.sources['applications']['daily_contact_audits'] = {'2026-10-15': {
            'tracked_contacts': 16, 'preexisting_contact_count': 4,
            'total_confirmed_account_contacts': 20, 'cap': 20,
            'observed_at': '2026-10-15T19:52:00+08:00'}}
        self.assertEqual(self.view()['summary']['daily_contacts']['confirmed'], 20)

    def test_inbound_reply_is_not_a_new_outbound_contact(self):
        reply = application('invited')
        reply.update(origin='recruiter_initiated', counts_as_new_outbound_contact=False)
        self.sources['applications']['applications'].append(reply)
        for status in ('verified_sent', 'dispatching', 'uncertain'):
            with self.subTest(status=status):
                reply['status'] = status
                counts = self.view()['summary']['daily_contacts']
                self.assertEqual(counts['confirmed'], 1)
                self.assertEqual(counts['uncertain'], 0)
                self.assertEqual(counts['minimum_count'], 1)

    def test_inbound_reply_after_daily_audit_does_not_increase_total(self):
        self.sources['applications']['daily_contact_audits'] = {'2026-10-15': {
            'total_confirmed_account_contacts': 3,
            'observed_at': '2026-10-15T19:00:00+08:00'}}
        reply = application('invited')
        reply.update(origin='recruiter_initiated', counts_as_new_outbound_contact=False,
                     attempted_at='2026-10-15T19:30:00+08:00')
        self.sources['applications']['applications'].append(reply)
        self.assertEqual(self.view()['summary']['daily_contacts']['confirmed'], 3)

    def test_verified_inbound_reply_keeps_actual_version_separate_from_notified_draft(self):
        reply = application('invited')
        reply.update(origin='recruiter_initiated', counts_as_new_outbound_contact=False,
                     reply_version='v2', verified_text='已批准的实际回复',
                     conversation_messages=[{'message_id': 'out-v2', 'kind': 'outgoing_text',
                         'text': '已批准的实际回复', 'timestamp_iso': '2026-10-15T19:30:00+08:00'}])
        self.sources['applications']['applications'].append(reply)
        self.sources['applications']['reply_monitor']['notification_events'] = [{
            'job_id': 'invited', 'draft_version': 'v1', 'draft_text': '原提醒邮件中的旧稿',
            'incoming_message_ids': ['invitation'], 'notified_draft_version': 'v1',
            'send_status': 'sent', 'reply_status': 'verified_sent', 'reply_message_id': 'out-v2'}]
        view = self.view()
        job = next(j for j in view['jobs'] if j['job_id'] == 'invited')
        self.assertEqual((job['sent_kind'], job['sent_version']), ('reply', 'v2'))
        self.assertTrue(job['monitored'])
        self.assertEqual(view['summary']['monitored'], 2)
        self.assertEqual(view['summary']['pending_replies'], 0)
        actual = next(d for d in job['drafts'] if d['status'] == 'verified_sent')
        self.assertEqual((actual['kind'], actual['version'], actual['text']),
                         ('reply', 'v2', '已批准的实际回复'))
        old = next(d for d in job['drafts'] if d['version'] == 'v1')
        self.assertEqual((old['status'], old['text']), ('resolved', '原提醒邮件中的旧稿'))
        outbound = next(j for j in view['jobs'] if j['job_id'] == 'a')
        self.assertEqual(outbound['sent_kind'], 'opening')

    def test_equal_time_conflict_is_unknown(self):
        self.sources['applications']['applications'][0]['conversation_observations'] = [
            {'observed_at': '2026-10-15T19:00:00+08:00', 'read_status': s} for s in ['已读', '未读']]
        self.assertEqual(self.view()['jobs'][0]['read_status'], 'unknown')

    def test_missing_salary_stays_unknown(self):
        self.assertIsNone(self.view()['jobs'][0]['salary_monthly_min'])

    def test_daily_salary_not_monthly(self):
        self.sources['applications']['applications'][0]['salary'] = '500-800元/天'
        self.assertIsNone(self.view()['jobs'][0]['salary_monthly_min'])

    def test_unrecognized_event_is_visible(self):
        self.sources['applications']['reply_monitor']['notification_events'] = [{'new_shape': True}]
        self.assertTrue(any(t['kind'] == 'unknown_event' for t in self.view()['tasks']))

    def test_incoming_without_notification_is_not_hidden(self):
        self.sources['applications']['applications'][0]['conversation_messages'] = [
            {'message_id': 'm1', 'speaker': '招聘方', 'kind': 'incoming_text', 'text': '能介绍一下项目吗？'}]
        self.assertTrue(any(t['kind'] == 'incoming' for t in self.view()['tasks']))

    def test_handled_resume_request_keeps_unanswered_question_pending(self):
        self.sources['applications']['applications'][0]['conversation_messages'] = [
            {'message_id': 'question', 'kind': 'incoming_text', 'text': '做过哪些平台，有什么结果？',
             'timestamp_iso': '2026-10-15T19:00:00+08:00'},
            {'message_id': 'resume-request', 'kind': 'incoming_text', 'text': '请发一份简历。',
             'timestamp_iso': '2026-10-15T19:01:00+08:00', 'response_required': False},
            {'message_id': 'resume-sent', 'kind': 'platform_event', 'actor': 'user', 'text': '',
             'timestamp_iso': '2026-10-15T19:02:00+08:00'}]
        view = self.view()
        incoming = [t for t in view['tasks'] if t['kind'] == 'incoming']
        self.assertEqual(len(incoming), 1)
        self.assertEqual(incoming[0]['incoming_message_ids'], ['question'])
        self.assertEqual(incoming[0]['detail'], '做过哪些平台，有什么结果？')
        messages = {m['message_id']: m for m in view['jobs'][0]['messages']}
        self.assertEqual(messages['resume-request']['text'], '请发一份简历。')
        self.assertFalse(messages['resume-request']['response_required'])
        self.assertTrue(messages['question']['response_required'])

    def test_daily_rollover_uses_shanghai(self):
        v = build_view(self.sources, datetime.fromisoformat('2026-10-15T17:00:00+00:00'))
        self.assertEqual(v['summary']['daily_contacts']['date'], '2026-10-16')
        self.assertEqual(v['summary']['daily_contacts']['confirmed'], 0)
        self.assertFalse(v['summary']['daily_contacts']['complete'])

    def test_history_search_failure_and_array_description(self):
        self.sources['guangzhou'] = {'followup_research': [{'full_job_descriptions': [
            {'job_id': 'b', 'descriptions': ['职责', '要求'], 'fit': '备选'}]}]}
        self.sources['cities'] = {'searches': [{'ok': False, 'data': None, 'query': 'AI'}]}
        v = self.view()
        self.assertEqual(next(j for j in v['jobs'] if j['job_id'] == 'b')['description'], '职责\n\n要求')
        self.assertEqual(v['meta']['search_failures'], 1)

    def test_model_does_not_modify_sources(self):
        before = copy.deepcopy(self.sources)
        self.view()
        self.assertEqual(self.sources, before)

    def test_duplicate_application_keeps_newer_observation_and_incoming(self):
        first = self.sources['applications']['applications'][0]
        first['server_verified_at'] = '2026-10-15T19:00:00+08:00'
        second = application()
        second['conversation_observations'] = [{'observed_at':'2026-10-15T19:45:00+08:00','read_status':'已读'}]
        second['conversation_messages'] = [{'message_id':'new','speaker':'招聘方','kind':'incoming_text','text':'请介绍项目'}]
        self.sources['applications']['applications'].append(second)
        v = self.view()
        self.assertEqual(len(v['jobs']), 1)
        self.assertEqual(v['jobs'][0]['read_status'], 'read')
        self.assertTrue(any(t['kind']=='incoming' for t in v['tasks']))

    def test_superseded_reply_event_stays_history_not_pending(self):
        self.sources['applications']['reply_monitor']['notification_events'] = [
            {'job_id':'a','draft_text':'旧回复','draft_version':'v1','incoming_message_ids':['in'], 'status':'superseded','superseded_by':'v2'},
            {'job_id':'a','draft_text':'新回复','draft_version':'v2','incoming_message_ids':['in']}]
        v = self.view()
        self.assertEqual(v['summary']['pending_replies'], 1)
        self.assertEqual([t['draft_text'] for t in v['tasks'] if t['kind']=='reply'], ['新回复'])
        self.assertTrue(any(d['text']=='旧回复' and d['status']=='superseded' for d in v['jobs'][0]['drafts']))

    def test_salary_units_and_mixed_pay_are_not_monthly(self):
        for value in ['20-30万/年', '年薪20-30万', '底薪8K+提成，综合15-20K', '15K/月+20万/年']:
            with self.subTest(value=value):
                self.sources['applications']['applications'][0]['salary'] = value
                self.assertIsNone(self.view()['jobs'][0]['salary_monthly_min'])
        self.sources['applications']['applications'][0]['salary'] = '15-20K·13薪'
        self.assertEqual(self.view()['jobs'][0]['salary_monthly_min'], 15000)

    def test_full_description_beats_older_search_and_conflict_is_visible(self):
        self.sources['applications']['applications'] = []
        self.sources['guangzhou'] = {'job_details': [{'job_id':'a','city':'广州','salary':'15-20K', 'description':'完整正文', 'observed_at':'2026-10-15T19:00:00+08:00'}]}
        self.sources['second'] = {'searches':[{'data':[{'job_id':'a','city':'深圳','salary':'8-10K', 'observed_at':'2026-10-15T18:00:00+08:00'}]}]}
        j = self.view()['jobs'][0]
        self.assertEqual((j['city'], j['salary_monthly_min']), ('广州', 15000))
        self.assertTrue(j['gaps'])

    def test_non_object_or_changed_event_collection_is_visible(self):
        for events in [['unexpected record'], {'new_events': [1]}, 'unknown']:
            with self.subTest(events=events):
                self.sources['applications']['reply_monitor']['notification_events'] = events
                self.assertTrue(any(t['kind']=='unknown_event' for t in self.view()['tasks']))

    def test_verified_response_event_resolves_prior_draft_only_with_actual_message(self):
        events = [
            {'job_id':'a','draft_text':'回复','draft_version':'v1','incoming_message_ids':['in']},
            {'job_id':'a','incoming_message_ids':['in'],'reply_status':'verified_sent','reply_message_id':'out'}]
        self.sources['applications']['reply_monitor']['notification_events'] = events
        self.assertEqual(self.view()['summary']['pending_replies'], 1)
        self.sources['applications']['applications'][0]['conversation_messages'] = [
            {'message_id':'out','kind':'outgoing_text','text':'实际回复','timestamp_iso':'2026-10-15T19:45:00+08:00'}]
        self.assertEqual(self.view()['summary']['pending_replies'], 0)

    def test_duplicate_event_does_not_duplicate_pending(self):
        event = {'job_id':'a','draft_text':'回复','draft_version':'v1','incoming_message_ids':['in']}
        self.sources['applications']['reply_monitor']['notification_events'] = [event, copy.deepcopy(event)]
        self.assertEqual(self.view()['summary']['pending_replies'], 1)

    def test_information_only_notifications_do_not_require_reply_drafts(self):
        self.sources['applications']['reply_monitor']['notification_events'] = [
            {'event_type': 'progress', 'job_id': 'a', 'new_message_ids': ['in'],
             'requires_user_review': False, 'send_status': 'sent'},
            {'event_type': 'connection', 'requires_user_review': False, 'send_status': 'sent'}]
        view = self.view()
        self.assertEqual(view['summary']['pending_replies'], 0)
        self.assertEqual(view['tasks'], [])

    def test_information_only_notification_keeps_uncertain_email_visible(self):
        self.sources['applications']['reply_monitor']['notification_events'] = [{
            'event_type': 'connection', 'requires_user_review': False, 'send_status': 'uncertain'}]
        self.assertEqual([t['kind'] for t in self.view()['tasks']], ['uncertain_notification'])

    def test_non_review_draft_preserves_text_without_requesting_approval(self):
        self.sources['applications']['reply_monitor']['notification_events'] = [{
            'job_id': 'a', 'draft_text': '邮件中留存的回复参考', 'draft_version': 'v1',
            'incoming_message_ids': ['in'], 'requires_user_review': False, 'send_status': 'sent'}]
        view = self.view()
        self.assertEqual(view['summary']['pending_replies'], 0)
        self.assertEqual(view['tasks'], [])
        draft = view['jobs'][0]['drafts'][0]
        self.assertEqual(draft['text'], '邮件中留存的回复参考')
        self.assertFalse(draft['requires_user_review'])
        self.assertNotIn(draft['status'], ('pending_review', 'verified_sent', 'resolved'))

    def test_reply_execution_states_are_not_approval_requests(self):
        self.sources['applications']['applications'][0]['conversation_messages'] = [
            {'message_id': 'in', 'kind': 'incoming_text', 'text': '请介绍项目',
             'timestamp_iso': '2026-10-15T19:40:00+08:00'}]
        for status in ('authorized_pending_send', 'dispatching', 'uncertain'):
            with self.subTest(status=status):
                self.sources['applications']['reply_monitor']['notification_events'] = [{
                    'job_id': 'a', 'draft_text': '真实项目介绍', 'draft_version': 'v1',
                    'incoming_message_ids': ['in'], 'reply_status': status, 'send_status': 'sent'}]
                view = self.view()
                self.assertEqual(view['summary']['pending_replies'], 0)
                self.assertEqual([t['kind'] for t in view['tasks']], ['uncertain_send'])
                draft = view['jobs'][0]['drafts'][0]
                self.assertEqual(draft['status'], 'authorized_pending_send')
                self.assertEqual(draft['execution_status'], status)
                self.assertFalse(draft['requires_user_review'])

    def test_explicit_review_request_can_ask_for_missing_facts_without_draft(self):
        self.sources['applications']['reply_monitor']['notification_events'] = [{
            'event_type': 'needs_user_input', 'job_id': 'a', 'incoming_message_ids': ['in'],
            'requires_user_review': True, 'analysis': '需要本人补充到岗时间',
            'uncertain_fields': ['最早到岗时间'], 'send_status': 'sent'}]
        view = self.view()
        self.assertEqual(view['summary']['pending_replies'], 1)
        self.assertEqual([t['kind'] for t in view['tasks']], ['user_input'])
        self.assertEqual(view['tasks'][0]['detail'], '需要本人补充到岗时间')
        self.assertEqual(view['tasks'][0]['uncertain_fields'], ['最早到岗时间'])

    def test_explicit_review_request_keeps_legacy_approval_behavior(self):
        self.sources['applications']['reply_monitor']['notification_events'] = [{
            'job_id': 'a', 'draft_text': '需要本人确认的承诺', 'draft_version': 'v1',
            'incoming_message_ids': ['in'], 'requires_user_review': True, 'send_status': 'sent'}]
        view = self.view()
        self.assertEqual(view['summary']['pending_replies'], 1)
        self.assertEqual([t['kind'] for t in view['tasks']], ['reply'])
        self.assertTrue(view['jobs'][0]['drafts'][0].get('requires_user_review'))

    def test_distinct_conversations_are_preserved_as_identity_uncertain(self):
        first=self.sources['applications']['applications'][0]
        first['conversation_uid']='one'
        second=application();second['conversation_uid']='two'
        second['conversation_messages']=[{'message_id':'in','kind':'incoming_text','text':'来信'}]
        self.sources['applications']['applications'].append(second)
        v=self.view()
        self.assertEqual(v['jobs'][0]['read_status'], 'unknown')
        self.assertTrue(v['jobs'][0]['gaps'])
        self.assertTrue(any(t['kind']=='incoming' for t in v['tasks']))


class SnapshotTests(ConfiguredTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        write_sources(self.root, fixture())
        self.store = WorkspaceStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_partial_json_retains_last_good(self):
        original = self.store.read_view()
        (self.root / SOURCES['applications']).write_text('{')
        result = self.store.read_view()
        self.assertEqual(result['jobs'], original['jobs'])
        self.assertTrue(result['meta']['read_errors'])
        self.assertTrue(result['meta']['stale'])

    def test_first_load_failure_is_unavailable(self):
        (self.root / SOURCES['applications']).write_text('{')
        result = self.store.read_view()
        self.assertIsNone(result['summary'])
        self.assertFalse(result['meta']['available'])


if __name__ == '__main__':
    unittest.main()
