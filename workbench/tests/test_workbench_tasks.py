from test_support import ConfiguredTestCase, SELF_UID, SELF_NAME, RECIPIENT
"""Workflows use fakes; never contact recruiters during regression tests."""
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from test_reply_engine import FakeReader, FakePlanner, FakeVerifier, FakeBridge, ROW, OLD, NEW
from master_record import read_master
from workbench_tasks import WorkbenchTasks, eligibility

class TasksTests(ConfiguredTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.data={'applications':[dict(company=ROW['company'],title=ROW['title'],conversation_uid=7,job_id='job7',
            city='广州',salary='10-15K',address='广州天河区',description='负责跨境业务内容，运用AI辅助整理企业资料。',
            identity_verified=True,monitoring_enabled=True,conversation_messages=[deepcopy(OLD)])],
            'ongoing_search_authorization':{'city_priority':['广州','深圳','杭州','上海','海宁'],'minimum_monthly_cny':10000,
              'excluded_districts':['白云区'],'exclude_phone_sales':True,'reply_authorization':{'enabled':True,'auto_send':'事实明确可回复'},
              'resume_request_authorization':{'enabled':True,'attachment_name':'示例候选人简历','text_resume_ui_verified':False,'allow_text_resume_verification':True}},
            'reply_monitor':{'execution_owner':'workbench-app-server','notification_events':[],
              'baseline':[dict(conversation_uid=7,job_id='job7',known_message_ids=['100'],last_list_message='您好',last_message_timestamp=1)]}}
        (self.root/'applications.json').write_text(json.dumps(self.data))
        (self.root/'facts.md').write_text('用户拥有跨境电商学习背景，做过海外内容实践。')
        self.reader=FakeReader(); self.bridge=FakeBridge(self.reader); self.planner=FakePlanner()
        self.task=WorkbenchTasks(self.root,reader_factory=lambda stop:self.reader,
            planner_factory=lambda stop,progress:self.planner,verifier_factory=lambda stop,progress:FakeVerifier(),
            bridge_factory=lambda reader,stop,allow:self.bridge,runtime_check=lambda:True,deliver_notifications=False,receipt_timeout=.01)
    def tearDown(self):self.tmp.cleanup()
    def run_mode(self,mode):
        self.task.start(mode); self.task.join(5); return self.task.status()
    def test_collect_only_analyzes_followup_sends_once(self):
        result=self.run_mode('collect'); self.assertEqual(result['status'],'completed')
        self.assertEqual(result['counts']['ready'],1); self.assertEqual(self.bridge.sends(),[])
        result=self.run_mode('followup'); self.assertEqual(result['counts']['sent'],1)
        self.run_mode('collect'); self.run_mode('followup')
        self.assertEqual(len(self.bridge.sends()),1)
    def test_changed_history_invalidates_preview(self):
        self.run_mode('collect'); self.reader.messages.append(dict(NEW,message_id='300',timestamp=4,text='明天能来面试吗？'))
        result=self.run_mode('followup'); self.assertEqual(self.bridge.sends(),[])
        self.assertEqual(result['items'][0]['status'],'superseded')
    def test_stop_before_dispatch_does_not_send(self):
        self.run_mode('collect')
        self.reader.on_history=lambda n:self.task.stop()
        result=self.run_mode('followup'); self.assertEqual(self.bridge.sends(),[]); self.assertEqual(result['status'],'stopped')
    def test_stop_during_notifications_is_never_marked_completed(self):
        self.task.deliver_notifications=True
        self.task._deliver_notifications=self.task.stop
        result=self.run_mode('collect')
        self.assertEqual(result['status'],'stopped')
        self.assertEqual(self.bridge.sends(),[])
    def test_missing_reply_authorization_cannot_become_ready(self):
        self.data['ongoing_search_authorization']['reply_authorization'].pop('enabled')
        (self.root/'applications.json').write_text(json.dumps(self.data))
        result=self.run_mode('collect')
        self.assertEqual(result['counts']['ready'],0)
        self.assertEqual(result['counts']['needs_user'],1)
        self.run_mode('followup')
        self.assertEqual(self.bridge.sends(),[])
    def test_stopped_recollection_preserves_unvisited_ready_preview(self):
        second=deepcopy(self.data['applications'][0])
        second.update(conversation_uid=8,job_id='job8',company='第二测试公司')
        self.data['applications'].append(second)
        (self.root/'applications.json').write_text(json.dumps(self.data))
        self.reader.rows.append(dict(ROW,uid='8',job_id='job8',company='第二测试公司'))
        def history(uid,known=()):
            return [deepcopy(OLD),dict(NEW,from_uid=uid)]
        self.reader.full_history=history
        self.assertEqual(self.run_mode('collect')['counts']['ready'],2)
        original=deepcopy(next(i for i in self.task._read()['items'] if i['uid']=='8'))
        def interrupted_history(uid,known=()):
            if uid=='8':self.task.stop()
            return history(uid,known)
        self.reader.full_history=interrupted_history
        stopped=self.run_mode('collect')
        self.assertEqual(stopped['status'],'stopped')
        self.assertEqual(stopped['counts']['ready'],2)
        self.assertEqual(next(i for i in self.task._read()['items'] if i['uid']=='8'),original)
        self.reader.full_history=history
        resumed=self.run_mode('collect')
        self.assertEqual(resumed['status'],'completed')
        self.assertEqual({i['uid'] for i in resumed['items'] if i['status']=='ready'},{'7','8'})
        self.assertEqual(self.bridge.sends(),[])
    def test_next_collection_enriches_job_deferred_by_detail_limit(self):
        base=deepcopy(self.data['applications'][0])
        self.data['applications']=[];self.reader.rows=[]
        for uid in range(7,13):
            app=dict(base,conversation_uid=uid,job_id=f'job{uid}',company=f'测试公司{uid}')
            app.pop('description');app.pop('address')
            self.data['applications'].append(app)
            self.reader.rows.append(dict(ROW,uid=str(uid),job_id=f'job{uid}',company=app['company']))
        (self.root/'applications.json').write_text(json.dumps(self.data))
        self.reader.full_history=lambda uid,known=():[deepcopy(OLD),dict(NEW,from_uid=uid)]
        detail_calls=[]
        def detail_bridge(payload):
            self.assertEqual(payload['kind'],'job_detail')
            detail_calls.append(payload['job_id'])
            return {'status':'job_detail','data':dict(base,job_id=payload['job_id'])}
        self.task.bridge_factory=lambda reader,stop,allow:detail_bridge
        first=self.run_mode('collect')
        self.assertEqual(first['status'],'completed')
        self.assertEqual(len(detail_calls),5)
        self.assertEqual(next(i['status'] for i in first['items'] if i['uid']=='12'),'awaiting_user')
        second=self.run_mode('collect')
        self.assertEqual(second['status'],'completed')
        self.assertEqual(detail_calls,['job7','job8','job9','job10','job11','job12'])
        self.assertEqual(second['counts']['ready'],6)
    def test_verified_no_response_message_needs_no_job_detail(self):
        app=self.data['applications'][0]
        app['conversation_messages'].append(dict(NEW,response_required=False))
        app.pop('description');app.pop('address')
        (self.root/'applications.json').write_text(json.dumps(self.data))
        result=self.run_mode('collect')
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['items'],[])
        self.assertEqual(self.planner.calls,[])
        self.assertEqual(self.bridge.calls,[])
    def test_no_response_marker_only_covers_its_exact_message_id(self):
        self.data['applications'][0]['conversation_messages'].append(dict(NEW,response_required=False))
        (self.root/'applications.json').write_text(json.dumps(self.data))
        self.reader.messages.append(dict(NEW,message_id='201',timestamp=3,text='能介绍一下跨境电商学习背景吗？'))
        result=self.run_mode('collect')
        self.assertEqual(result['counts']['ready'],1)
        self.assertEqual([m['message_id'] for m in self.planner.calls[0]['incoming']],['201'])
        self.assertEqual(self.bridge.sends(),[])
    def test_no_response_marker_invalidates_cached_awaiting_user(self):
        class Reject:
            def verify(self,context,plan):return {'approved':False,'reason':'需本人补充事实'}
        self.task.verifier_factory=lambda stop,progress:Reject()
        self.assertEqual(self.run_mode('collect')['items'][0]['status'],'awaiting_user')
        data=read_master(self.root)
        for message in data['applications'][0]['conversation_messages']:
            if message['message_id']=='200':message['response_required']=False
        (self.root/'applications.json').write_text(json.dumps(data))
        self.assertEqual(self.run_mode('collect')['items'],[])
        self.assertEqual(len(self.planner.calls),1)
        self.assertEqual(self.bridge.calls,[])
    def test_plain_text_resume_exact_request_and_receipt_not_repeated(self):
        self.reader.messages[-1]['text']='请发一份简历'
        result=self.run_mode('collect'); self.assertEqual(result['items'][0]['kind'],'resume'); self.assertEqual(self.bridge.sends(),[])
        result=self.run_mode('followup'); self.assertEqual(result['counts']['sent'],1)
        self.run_mode('collect'); self.run_mode('followup'); self.assertEqual(len(self.bridge.sends()),1)
    def test_receipt_before_collect_skips_resume(self):
        self.reader.messages[-1]['text']='请发一份简历'
        self.reader.messages.append(dict(message_id='300',timestamp=3,from_uid='7',body_type=12,
            text='',hyperLink={'text':'您的附件简历 示例候选人简历 已发送给Boss点击查看附件'}))
        result=self.run_mode('collect'); self.assertEqual(result['counts']['ready'],0); self.assertEqual(self.bridge.sends(),[])
    def test_unknown_send_never_retries_after_restart(self):
        self.run_mode('collect'); self.bridge.result={'status':'uncertain','clicked':True}
        self.run_mode('followup'); self.assertEqual(len(self.bridge.sends()),1)
        self.run_mode('collect'); self.run_mode('followup'); self.assertEqual(len(self.bridge.sends()),1)
    def test_resume_does_not_consume_separate_question(self):
        self.reader.messages.append(dict(NEW,message_id='201',timestamp=3,text='请发一份简历'))
        self.run_mode('collect');self.run_mode('followup')
        result=self.run_mode('collect')
        self.assertEqual(result['counts']['ready'],1)
        self.assertEqual(result['items'][0]['kind'],'reply')
    def test_superseding_resume_still_runs_fact_verifier(self):
        self.reader.messages[-1]['text']='请发一份简历'
        self.reader.messages.append(dict(NEW,message_id='201',timestamp=3,text='简历不用发了，先聊项目'))
        class Reject:
            def verify(self,context,plan):return {'approved':False,'reason':'无依据'}
        self.task.verifier_factory=lambda stop,progress:Reject()
        result=self.run_mode('collect');self.assertEqual(result['counts']['ready'],0)
        self.assertEqual(result['items'][0]['status'],'awaiting_user')
    def test_facts_change_recollects_invalidated_preview(self):
        self.run_mode('collect')
        (self.root/'facts.md').write_text('用户拥有跨境电商学习背景，做过海外内容实践。新增事实。')
        self.run_mode('followup');result=self.run_mode('collect')
        self.assertEqual(len(self.planner.calls),2);self.assertEqual(result['counts']['ready'],1)
    def test_stop_after_intent_before_click_remains_retryable(self):
        self.run_mode('collect')
        def before_click(payload):
            self.task.stop()
            return {'status':'blocked','clicked':False,'reason':'STOPPED'}
        self.task.bridge_factory=lambda reader,stop,allow:before_click
        result=self.run_mode('followup')
        self.assertEqual(result['items'][0]['status'],'ready')
        self.task.bridge_factory=lambda reader,stop,allow:self.bridge
        result=self.run_mode('followup')
        self.assertEqual(result['counts']['sent'],1)
    def test_legacy_awaiting_review_does_not_override_new_fact_review(self):
        from automation_state import EventStore
        from reply_engine import event_key
        EventStore(self.root).enqueue(event_key(ROW,[NEW]),uid='7',status='awaiting_user')
        self.run_mode('collect');result=self.run_mode('followup')
        self.assertEqual(result['counts']['sent'],1)
    def test_low_salary_and_phone_sales_never_send(self):
        app=self.data['applications'][0]
        for fields in ({'salary':'8-15K'},{'title':'电话销售'},{'description':'电话外呼获客，销售业务。'},{'address':'广州白云区某路'},
                       {'description':'试用期底薪6000元加提成，运营业务。'}):
            with self.subTest(fields=fields):
                self.assertEqual(eligibility({**app,**fields},self.data['ongoing_search_authorization'])[0],'ignore')
    def test_monthly_chinese_salary_units(self):
        for salary in ('1.5-3万元','1-2万','10000-20000元/月','10-15K·13薪'):
            self.assertIsNone(eligibility({**self.data['applications'][0],'salary':salary},self.data['ongoing_search_authorization'])[0])
        self.assertEqual(eligibility({**self.data['applications'][0],'salary':'20-30万/年'},self.data['ongoing_search_authorization'])[0],'escalate')
    def test_unknown_salary_needs_data(self):
        self.assertEqual(eligibility({**self.data['applications'][0],'salary':'200元/天'},self.data['ongoing_search_authorization'])[0],'escalate')
    def test_owner_change_prevents_send(self):
        self.run_mode('collect'); x=read_master(self.root); x['reply_monitor']['execution_owner']='heartbeat-boss'
        (self.root/'applications.json').write_text(json.dumps(x))
        with self.assertRaises(ValueError): self.task.start('followup')
    def test_double_start_rejected(self):
        gate=threading.Event(); self.planner.callback=lambda:gate.wait(2)
        self.task.start('collect')
        try:
            with self.assertRaises(RuntimeError):self.task.start('collect')
        finally:gate.set();self.task.join(5)

if __name__=='__main__':unittest.main()
