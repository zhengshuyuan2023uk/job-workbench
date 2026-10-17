"""On-demand workbench runs. One owner, one journal, no polling or automatic sends."""
from copy import deepcopy
from config import get_config
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import tomllib
import uuid
from automation_state import exclusive_runner_lock, EventStore
from boss_reader import BossBlocked, baseline_from_master, message_fingerprint
from master_record import (read_master, transaction, application, archive_history,
                           advance, observe, save_action, now)
from reply_engine import ReplyEngine, event_key, effective_text
from reply_planner import validate_plan
from reply_verifier import deterministic_gate
from resume_policy import (request_mode, resume_key, delivered_receipt,
                           resume_send_attempt, superseding_request)

OWNER='workbench-app-server'
ANALYSIS_VERSION=2
READY={'ready','authorized_pending_send'}
UNKNOWN={'dispatching','uncertain'}
TERMINAL={'verified_sent','observed_sent','superseded','ignored','awaiting_user','failed'}

class Stopped(RuntimeError): pass

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()

def authority_digest(policy):
    policy=deepcopy(policy)
    resume=policy.get('resume_request_authorization',{})
    for key in ('text_resume_ui_verified','text_resume_verified_at','text_resume_verification_action'):
        resume.pop(key,None)
    return digest(policy)

def history_digest(messages):
    keys=('message_id','timestamp','from_uid','text','body_type','dialog','hyperLink','action','jobDesc')
    return digest([{k:m[k] for k in keys if k in m} for m in sorted(messages,key=lambda v:str(v['message_id']))])

def eligibility(app, policy):
    """Deterministic minimum requirements apply before both texts and resumes."""
    floor=policy.get('minimum_monthly_cny')
    cities=policy.get('city_priority')
    if (isinstance(floor,bool) or not isinstance(floor,(int,float)) or floor<0
            or not isinstance(cities,list) or not cities or any(not isinstance(c,str) or not c.strip() for c in cities)):
        return 'escalate','请先明确本人期望城市与最低月薪'
    salary=str(app.get('salary') or app.get('job_card_snapshot',{}).get('salary') or '')
    match=re.search(r'(\d+(?:\.\d+)?)\s*(?:[-–—~至]\s*\d+(?:\.\d+)?)?\s*([kK万千])',salary)
    if re.search(r'/\s*[天日时年]|日薪|时薪|年薪|元每[日时]|元/[日天时]',salary):
        return 'escalate','月薪口径未核实，先补充岗位信息'
    if match:
        minimum=float(match[1])*(10000 if match[2]=='万' else 1000)
    else:
        yuan=re.fullmatch(r'\s*(\d{4,6})(?:[-–—~至]\d{4,6})?元?(?:/月|每月)?\s*',salary)
        if not yuan:return 'escalate','月薪口径未核实，先补充岗位信息'
        minimum=float(yuan[1])
    if minimum < floor:
        return 'ignore','月薪下限低于已配置要求，不回复或发简历'
    content=' '.join(str(app.get(k) or '') for k in ('description','address','district','screening_note','title'))
    if any(d in content for d in policy.get('excluded_districts',[])):
        return 'ignore','工作地点属于已排除区域'
    if policy.get('exclude_phone_sales',False) and re.search(r'电话销售|电话外呼|电销|外呼获客|陌生电话',content):
        return 'ignore','岗位涉及电话销售或外呼获客'
    for m in re.finditer(r'(?:固定(?:月薪|工资)|试用期(?:固定)?(?:月薪|工资|底薪)?|底薪)\s*(?:为|是|[:：])?\s*(\d+(?:\.\d+)?)\s*([kK千万元]?)',content):
        unit=m[2]; amount=float(m[1]) * (1000 if unit in ('k','K','千') else 10000 if unit=='万' else 1)
        if amount<floor:
            return 'ignore','已明确固定或试用期月薪低于配置要求'
    city=str(app.get('city') or '')
    if not city or not any(c in city for c in cities):
        return 'escalate','岗位城市未核实或不在当前意向城市'
    if not app.get('description') or not app.get('address'):
        return 'escalate','完整职位正文或地址待核实'
    return None,''

def default_runtime_check():
    path=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))/'automations/boss/automation.toml'
    if path.exists() and tomllib.loads(path.read_text()).get('status')!='PAUSED':return False
    # A fresh install has no old heartbeat or launchd job. Never create either.
    import shutil
    if shutil.which('launchctl'):
        p=subprocess.run(['launchctl','print',f'gui/{os.getuid()}/local.findjobs.autoreply'],capture_output=True,timeout=3)
        if p.returncode==0:return False
    return True

class WorkbenchTasks:
    def __init__(self,root,reader_factory=None,planner_factory=None,verifier_factory=None,
                 bridge_factory=None,runtime_check=None,deliver_notifications=True,receipt_timeout=5):
        self.root=Path(root); self.mutex=threading.RLock(); self.thread=None; self.stop_event=threading.Event()
        self.reader_factory=reader_factory;self.planner_factory=planner_factory;self.verifier_factory=verifier_factory
        self.bridge_factory=bridge_factory;self.runtime_check=runtime_check or default_runtime_check
        self.deliver_notifications=deliver_notifications; self.engine=None; self.receipt_timeout=receipt_timeout
        # A crashed run never resumes itself. Dispatch intents remain in the journal.
        try:current=read_master(self.root).get('reply_monitor',{}).get('workbench_tasks',{})
        except FileNotFoundError:current={}
        if current.get('status') in ('running','stopping'):
            self._update(status='stopped',phase='interrupted',message='上次任务中断；下次先核验未决发送，不自动重试',finished_at=now())

    def _read(self):
        return read_master(self.root).get('reply_monitor',{}).get('workbench_tasks',{
            'status':'idle','items':[],'counts':{},'message':'点击收集并分析，整理当前待办'})
    def _update(self,**fields):
        def save(data):data['reply_monitor'].setdefault('workbench_tasks',{}).update(deepcopy(fields))
        transaction(self.root,save)
    def status(self):
        with self.mutex:
            value=deepcopy(self._read());items=value.get('items',[])
            value['counts']={**value.get('counts',{}),'ready':sum(i.get('status') in READY for i in items),
                'sent':sum(i.get('status') in ('verified_sent','observed_sent') for i in items),
                'needs_user':sum(i.get('status')=='awaiting_user' for i in items),
                'uncertain':sum(i.get('status') in UNKNOWN for i in items),
                'skipped':sum(i.get('status') in ('ignored','superseded','failed') for i in items)}
            value['items']=[{k:i.get(k) for k in ('action_id','uid','company','title','kind','status','reason','text','question_for_user')} for i in items]
            return value
    def _guard(self):
        if self.stop_event.is_set():raise Stopped('已停止后续操作')
        if read_master(self.root)['reply_monitor'].get('execution_owner')!=OWNER:
            raise ValueError('当前执行权不属于工作台，请停止其他执行方后再试')
    def allowed(self):
        try:self._guard(); return self._read().get('mode')=='followup' and self.runtime_check()
        except (ValueError,OSError,subprocess.SubprocessError,Stopped):return False
    def start(self,mode):
        if mode not in ('collect','followup'):raise ValueError('未知任务')
        get_config().require_live()
        with self.mutex:
            if self.thread and self.thread.is_alive():raise RuntimeError('本轮正在执行，请等待或停止，不能重复启动')
            if read_master(self.root)['reply_monitor'].get('execution_owner')!=OWNER:
                raise ValueError('当前执行权不属于工作台')
            if not self.runtime_check():raise ValueError('定时任务或常驻执行器未停止，不能同时操作')
            self.stop_event.clear()
            self._update(status='running',run_id=uuid.uuid4().hex,mode=mode,phase='starting',error=None,
                         started_at=now(),finished_at=None,message='准备读取并核验当前状态')
            self.thread=threading.Thread(target=self._run,args=(mode,),daemon=True,name='findjobs-on-demand')
            self.thread.start();return self.status()
    def stop(self):
        self.stop_event.set()
        with self.mutex:
            if self.thread and self.thread.is_alive():
                self._update(status='stopping',message='正在停止；已经点击的操作会保留结果并核验，不会重发')
            return self.status()
    def join(self,timeout=None):
        if self.thread:self.thread.join(timeout)
    def progress(self,message):
        self._update(message=str(message)[:250])
    def _items(self,items):self._update(items=items)
    def _run(self,mode):
        reader=planner=verifier=None
        try:
            with exclusive_runner_lock(self.root):
                self._guard()
                if self.reader_factory:reader=self.reader_factory(self.stop_event)
                else:
                    from boss_cdp import BossCDPReader
                    preferred=read_master(self.root)['reply_monitor'].get('workbench_connection',{}).get('target_id')
                    reader=BossCDPReader(self.root,stop_event=self.stop_event,preferred_target_id=preferred)
                if self.planner_factory:planner=self.planner_factory(self.stop_event,self.progress)
                else:
                    from app_server_client import AppServerPlanner
                    planner=AppServerPlanner(self.root,stop_event=self.stop_event,progress=self.progress)
                if self.verifier_factory:verifier=self.verifier_factory(self.stop_event,self.progress)
                else:
                    from app_server_client import AppServerVerifier
                    verifier=AppServerVerifier(self.root,stop_event=self.stop_event,progress=self.progress)
                if self.bridge_factory:bridge=self.bridge_factory(reader,self.stop_event,self.allowed)
                else:
                    from boss_cdp import BossCDPBridge
                    bridge=BossCDPBridge(self.root,reader,stop_event=self.stop_event,authorize=self.allowed)
                self.engine=ReplyEngine(self.root,reader,planner,verifier,bridge)
                self.engine.allowed=self.allowed
                if mode=='collect':self._collect()
                else:self._followup()
                self._guard()
                self._queue_notifications()
                if self.deliver_notifications:
                    self._update(phase='notifications',message='核验必要提醒的发送状态')
                    self._deliver_notifications()
                self._guard()
                self._update(status='completed',phase='done',message='本轮处理完成' if mode=='followup' else '收集分析完成，可查看并跟进待办',finished_at=now())
        except Exception as exc:
            cancelled=self.stop_event.is_set() or isinstance(exc,Stopped)
            message='本轮已停止，未处理事项保留；结果不明的发送不会重试' if cancelled else str(exc)[:250]
            items=self._read().get('items',[])
            for item in items:
                if item.get('status')=='dispatching':
                    item.update(status='uncertain',reason='发送阶段中断，须回读核验，禁止重试')
                    if self.engine:
                        action=self._action(item,'uncertain');save_action(self.root,item['uid'],action)
                        if self.engine.events.get(item['action_id']):
                            self.engine.events.update(item['action_id'],status='uncertain',action=action)
            self._items(items)
            self._update(status='stopped' if cancelled else 'failed',phase='stopped' if cancelled else 'error',
                         error=None if cancelled else message,message=message,finished_at=now())
            if isinstance(exc,BossBlocked) and not cancelled and exc.code != 'INCOMPLETE_HISTORY':
                self._connection_issue(str(exc),exc.code)
        finally:
            for resource in (reader,planner,verifier):
                close=getattr(resource,'close',None)
                if close:
                    try:close()
                    except Exception:pass
            self.engine=None
    def _connection_issue(self,message,code):
        def save(data):
            monitor=data['reply_monitor'];old=monitor.get('last_connection_issue') or {}
            key=old.get('notification_event_id') if old.get('status')=='unresolved' and old.get('code')==code else None
            key=key or 'connection:'+code+':'+now()
            monitor['last_connection_issue']={'status':'unresolved','code':code,'message':message,'observed_at':now(),'notification_event_id':key}
            return key
        key=transaction(self.root,save)
        if self.engine:
            self.engine.mail.enqueue(key,'boss_connection_issue','BOSS连接',message+'\n当前任务已停止，没有切换网络或自动重试。')
            if self.deliver_notifications:self._deliver_notifications()

    def _collect(self):
        engine=self.engine; self._update(phase='collecting',message='读取会话列表并对照历史')
        rows=engine.reader.list_conversations();self._guard()
        target_id=getattr(engine.reader,'target_id',None)
        if target_id:
            def bind(data):data['reply_monitor']['workbench_connection']={'target_id':target_id,'verified_at':now(),'self_uid':get_config().self_uid}
            transaction(self.root,bind)
        data=read_master(self.root); baselines=baseline_from_master(self.root)
        previous={i['uid']:i for i in self._read().get('items',[])};items=[];reads=0;details=0
        remaining=dict(previous)
        def save_progress():
            # A stopped collection must retain previews it has not reached yet.
            self._items(items+list(remaining.values()))
        facts_digest=digest((self.root/'facts.md').read_text())
        policy_digest=authority_digest(data['ongoing_search_authorization'])
        pending_byuid={str(e.get('uid')):e for e in engine.events.pending() if e.get('status') in UNKNOWN}
        for row in rows:
            self._guard();uid=row['uid'];app=application(data,uid);old=previous.get(uid)
            remaining.pop(uid,None)
            handled_ids={str(m['message_id']) for m in (app or {}).get('conversation_messages',[]) if m.get('response_required') is False}
            if not app and uid not in previous:
                # New user-created conversations require history to establish participation.
                pass
            elif old and old.get('analysis_version')==ANALYSIS_VERSION and old.get('row_fingerprint')==message_fingerprint(row) and old.get('status') in ('ignored','awaiting_user') and old.get('facts_digest')==facts_digest and old.get('policy_digest')==policy_digest and not handled_ids.intersection(old.get('incoming_ids',[])) and (old.get('status')=='ignored' or (app and app.get('description') and app.get('address'))):
                items.append(old);continue
            baseline=baselines.get(uid)
            # At first button run inspect existing latest unanswered messages once;
            # subsequent runs only read changed, pending or unknown conversations.
            if old is None and self._read().get('collection_complete') and baseline and message_fingerprint(baseline)==message_fingerprint(row) and uid not in pending_byuid:
                continue
            self.progress(f'核对 {row["company"] or row["name"]} 的最新会话')
            messages=engine.reader.full_history(uid,set());reads+=1;self._guard()
            if self.reader_factory is None:self.stop_event.wait(.2)
            self._guard()
            if app and str(app.get('job_id'))!=str(row['job_id']):
                items.append(self._item(row,app,'uncertain','uncertain','同一联系人岗位变化，需要先核对身份'));save_progress();continue
            app=archive_history(self.root,row,messages)
            if not app:continue
            if any(m.get('body_type') in (1,7) and effective_text(m) and str(m.get('from_uid')) not in (uid,get_config().self_uid) for m in messages):
                raise BossBlocked('消息来源与会话身份不一致','UNKNOWN_MESSAGE_SOURCE')
            unknown=pending_byuid.get(uid)
            if unknown:
                result=engine.reconcile(unknown)
                item=self._item(row,app,unknown.get('kind','uncertain'),result['status'],result.get('reason','已核验既有发送'))
                item['action_id']=unknown['event_key'];items.append(item);save_progress();continue
            if any(a.get('status') in UNKNOWN for a in app.get('followup_actions',[])):
                items.append(self._item(row,app,'uncertain','uncertain','既有发送结果待核验，禁止重发'));save_progress();continue
            last_user=max((int(m.get('timestamp') or 0) for m in messages if str(m.get('from_uid'))==get_config().self_uid and m.get('body_type')==1 and effective_text(m)),default=0)
            new=[m for m in messages if str(m.get('from_uid'))==uid and m.get('body_type') in (1,7) and effective_text(m).strip() and int(m.get('timestamp') or 0)>=last_user]
            covered={str(i) for a in app.get('followup_actions',[]) if a.get('status') in ('verified_sent','observed_sent','superseded','ignored') for i in a.get('incoming_message_ids',a.get('trigger_message_ids',[]))}
            covered.update(str(m['message_id']) for m in app.get('conversation_messages',[]) if m.get('response_required') is False)
            new=[m for m in new if str(m['message_id']) not in covered]
            new=[m for m in new if not request_mode(m) or not delivered_receipt(messages,m)]
            if not new:
                advance(self.root,row,messages);continue
            rejection=all(re.search(r'祝您在BOSS直聘找到更适合您的工作|与我们目前的职位需求并不完全吻合|暂不考虑您的申请',effective_text(m)) for m in new)
            # A clear rejection needs no response or further job-detail lookup.
            if not rejection and (not app.get('description') or not app.get('address')) and details<5:
                app=engine.enrich_job(app,row,False);details+=1
            kind,reason=('ignore','招聘方已明确拒绝，无需补发消息') if rejection else eligibility(app,data['ongoing_search_authorization'])
            request=next((m for m in reversed(new) if request_mode(m)),None)
            key=resume_key(row,str(request['message_id'])) if request else event_key(row,new)+':wb:'+facts_digest[:10]+policy_digest[:10]
            existing=engine.events.get(key)
            if existing and existing.get('status') in ('verified_sent','observed_sent','ignored','superseded'):
                advance(self.root,row,messages);continue
            if kind:
                plan=dict(kind=kind,text='',reason=reason,question_for_user=reason if kind=='escalate' else '',evidence=[],notification='none')
            elif request:
                attempt=resume_send_attempt(messages,request,get_config().self_uid)
                if attempt:
                    items.append(self._item(row,app,'resume','uncertain','账号已有附件发送动作，等待回执，禁止重复发送'));save_progress();continue
                if superseding_request(messages,request):
                    request=None;key=event_key(row,new)+':wb:'+facts_digest[:10]+policy_digest[:10]
                    plan=engine.planner.plan(engine.context(app,messages,new))
                else:plan=dict(kind='resume',text='',reason='招聘方明确索要已配置附件简历且未见送达回执',question_for_user='',evidence=[],notification='none')
            else:
                self._update(phase='analyzing');context=engine.context(app,messages,new)
                plan=engine.planner.plan(context);self._guard()
            if plan['kind'] not in ('resume','ignore','escalate') or (plan['kind']=='resume' and not request):
                context=engine.context(app,messages,new)
                plan=validate_plan({k:v for k,v in plan.items() if k!='planning_seconds'},context['facts'],context['policy'])
                if plan['kind']=='resume':
                    plan.update(kind='escalate',reason='此请求还包含其他问题或指定文件，需要本人确认',question_for_user='请确认对方需要的内容。',notification='needs_user_input')
                if plan['kind']=='reply':
                    verdict=engine.verifier.verify(context,plan);self._guard()
                    if not verdict['approved']:
                        plan.update(kind='escalate',reason=verdict['reason'],question_for_user='请补充或确认这条回复中的事实。',notification='needs_user_input')
            status='ready' if plan['kind'] in ('reply','resume') else 'awaiting_user' if plan['kind']=='escalate' else 'ignored'
            auth=data['ongoing_search_authorization']
            permission=auth.get('reply_authorization' if plan['kind']=='reply' else 'resume_request_authorization',{})
            if status=='ready' and permission.get('enabled') is not True:
                plan.update(kind='escalate',reason='尚未明确授权此类发送操作',question_for_user='请确认是否授权此类发送操作。',notification='needs_user_input')
                status='awaiting_user'
            item=self._item(row,app,plan['kind'],status,plan['reason'])
            item.update(action_id=key,text=plan.get('text',''),question_for_user=plan.get('question_for_user',''),plan=plan,
                analysis_version=ANALYSIS_VERSION,
                incoming_ids=[str(request['message_id'])] if plan['kind']=='resume' and request else [str(m['message_id']) for m in new],snapshot=history_digest(messages),
                policy_digest=authority_digest(data['ongoing_search_authorization']),facts_digest=digest((self.root/'facts.md').read_text()),
                request_id=str(request['message_id']) if request else '',request_mode=request_mode(request) if request else '')
            if status=='ready':
                action=self._action(item,'authorized_pending_send')
                save_action(self.root,uid,action)
            items.append(item);save_progress();advance(self.root,row,messages)
        observed_uids={r['uid'] for r in rows}
        for app in data['applications']:
            uid=str(app.get('conversation_uid') or '')
            if app.get('monitoring_enabled') and uid not in observed_uids:
                row=dict(uid=uid,company=app.get('company',''),title=app.get('title',''),job_id=app.get('job_id',''))
                item=previous.get(uid) or self._item(row,app,'uncertain','uncertain','本轮列表未覆盖此联系人，状态未知')
                item['status']='uncertain';items.append(item)
        self._items(items);observe(self.root,rows,history_reads=reads)
        def finish(data):
            m=data['reply_monitor'];m['last_successful_check']['executor']=OWNER
            m['workbench_tasks'].update(collection_complete=True,counts={'conversations':len(rows),'changed':reads})
            issue=m.get('last_connection_issue') or {}
            if issue.get('status')=='unresolved':
                issue.update(status='resolved',resolved_at=now(),resolution='工作台完成会话列表与所需完整历史的实际读取')
        transaction(self.root,finish)

    def _item(self,row,app,kind,status,reason):
        return dict(action_id='review:'+str(row.get('job_id'))+':'+row['uid'],uid=row['uid'],company=app.get('company',row.get('company','')),
            title=app.get('title',''),kind=kind,status=status,reason=reason,text='',question_for_user='',row=row,
            row_fingerprint=message_fingerprint(row))
    def _action(self,item,status):
        return dict(action_id=item['action_id'],type=item['kind'],version='workbench-app-server-v1',actor='agent',
            status=status,text=item.get('text',''),incoming_message_ids=item.get('incoming_ids',[]),
            request_id=item.get('request_id',''),request_mode=item.get('request_mode',''),attachment_name=get_config().attachment_name if item['kind']=='resume' else None,
            created_at=now(),authorization='ongoing_search_authorization.'+('resume_request_authorization' if item['kind']=='resume' else 'reply_authorization'))
    def _followup(self):
        engine=self.engine;items=self._read().get('items',[])
        # This button only executes previews already produced by Collect.
        for item in items:
            self._guard()
            if item.get('status') not in READY:continue
            self._update(phase='checking',message='发送前重新核对 '+item['company'])
            row=item['row'];uid=item['uid'];key=item['action_id']
            previous=engine.events.get(key)
            if previous and previous.get('status') in UNKNOWN:
                result=engine.reconcile(previous);item['status']=result['status'];self._items(items);continue
            if previous and previous.get('status') in TERMINAL:
                item['status']=previous['status'];self._items(items);continue
            data=read_master(self.root);app=application(data,uid)
            if not app or str(app['job_id'])!=str(row['job_id']):raise BossBlocked('会话岗位身份变化','IDENTITY_CHANGED')
            kind,reason=eligibility(app,data['ongoing_search_authorization'])
            if kind:
                item.update(status='ignored' if kind=='ignore' else 'awaiting_user',reason=reason);self._items(items);continue
            if item['policy_digest']!=authority_digest(data['ongoing_search_authorization']) or item['facts_digest']!=digest((self.root/'facts.md').read_text()):
                item.update(status='superseded',reason='事实或授权已更新，请重新收集分析');self._items(items);continue
            messages=engine.reader.full_history(uid,set());self._guard();archive_history(self.root,row,messages)
            request=next((m for m in messages if str(m['message_id'])==item.get('request_id')),None)
            if item['kind']=='resume' and request and delivered_receipt(messages,request):
                item.update(status='observed_sent',reason='账号已发送并有附件回执，不重复发送');self._items(items);continue
            if item['kind']=='resume' and request and resume_send_attempt(messages,request,get_config().self_uid):
                item.update(status='uncertain',reason='账号已有发送动作，等待回执，不重复发送');self._items(items);continue
            if item['snapshot']!=history_digest(messages):
                item.update(status='superseded',reason='聊天有新内容，请重新收集分析');self._items(items);continue
            if item['kind']=='reply':
                if data['ongoing_search_authorization'].get('reply_authorization',{}).get('enabled') is not True:
                    item.update(status='awaiting_user',reason='普通回复发送尚未获得明确授权');self._items(items);continue
                context=engine.context(app,messages,[m for m in messages if str(m['message_id']) in item['incoming_ids']])
                reason=deterministic_gate(context,item['plan'])
                if reason:
                    item.update(status='awaiting_user',reason=reason);self._items(items);continue
                if any(str(m.get('from_uid'))==get_config().self_uid and m.get('text')==item['text'] for m in messages):
                    item.update(status='superseded',reason='历史已有相同介绍，不重复发送');self._items(items);continue
            else:
                auth=data['ongoing_search_authorization'].get('resume_request_authorization',{})
                if not request or not request_mode(request) or not auth.get('enabled') or auth.get('attachment_name')!=get_config().attachment_name:
                    item.update(status='awaiting_user',reason='现有附件或对应请求未核实');self._items(items);continue
            payload=engine.payload(row,app,messages,item['plan'],item.get('request_id',''),item.get('request_mode','card'))
            payload['allow_text_resume_verification']=data['ongoing_search_authorization'].get('resume_request_authorization',{}).get('allow_text_resume_verification') is True
            action=self._action(item,'authorized_pending_send');save_action(self.root,uid,action)
            ev=engine.events.enqueue(key,uid=uid,row=row,kind=item['kind'],incoming_ids=item['incoming_ids'],payload=payload,action=action)
            if ev['status'] in UNKNOWN|TERMINAL:
                item['status']=ev['status'];self._items(items);continue
            self._guard()
            if not self.allowed():raise Stopped('发送前执行条件已变化')
            action.update(status='dispatching',dispatching_at=now());save_action(self.root,uid,action)
            engine.events.update(key,status='dispatching',payload=payload,action=action)
            item['status']='dispatching';self._items(items);self._update(phase='sending',message='正在处理 '+item['company'])
            try:ui=engine.bridge(payload)
            except Exception:
                engine.events.update(key,status='uncertain',action=dict(action,status='uncertain'))
                save_action(self.root,uid,dict(action,status='uncertain'));item['status']='uncertain';self._items(items);raise
            engine.events.update(key,ui_result=ui)
            if ui.get('clicked') is False and ui.get('reason') in ('STOPPED','NOT_AUTHORIZED_NOW'):
                action['status']='authorized_pending_send';save_action(self.root,uid,action)
                engine.events.update(key,status='authorized_pending_send',action=action,execution_not_started=True)
                item.update(status='ready',reason='已停止，尚未点击发送');self._items(items)
                raise Stopped('点击前已停止')
            if ui.get('clicked') is False and ui.get('status')!='verified':
                action.update(status='failed',issue=ui.get('reason','官方页面没有核实发送入口'))
                engine.events.update(key,status='failed',action=action);save_action(self.root,uid,action)
                item.update(status='failed',reason=action['issue']);self._items(items)
                if ui.get('reason') in ('OFFICIAL_SAFETY_CHECK','CONNECTION_FAILED','BROWSER_OPERATION_FAILED'):
                    raise BossBlocked(action['issue'],ui['reason'])
                continue
            # Wait only for a receipt, never retry the click.
            deadline=time.monotonic()+self.receipt_timeout
            while not self.stop_event.is_set():
                received=engine.reader.full_history(uid,set())
                if (item['kind']=='resume' and delivered_receipt(received,request)) or (item['kind']=='reply' and any(str(m.get('from_uid'))==get_config().self_uid and m.get('text')==item['text'] and str(m['message_id']) not in payload['known_message_ids'] for m in received)):
                    break
                if time.monotonic()>=deadline:break
                self.stop_event.wait(.4)
            result=engine.reconcile(engine.events.get(key));item['status']=result['status']
            item['reason']='已核验官方界面和服务器回执' if result['status'] in ('verified_sent','observed_sent') else result.get('reason','结果待核验，禁止重发')
            self._items(items)
            if result['status']=='verified_sent' and item['kind']=='resume' and item.get('request_mode')=='text':
                def verified(data):
                    auth=data['ongoing_search_authorization']['resume_request_authorization']
                    auth.update(text_resume_ui_verified=True,text_resume_verified_at=now(),text_resume_verification_action=key)
                transaction(self.root,verified)
            if result['status'] in UNKNOWN:break
            if item['plan'].get('notification')=='meaningful_progress':
                engine.notify(key,'meaningful_progress',row,[m for m in messages if str(m['message_id']) in item['incoming_ids']],item['plan'],'已自动回复并核验送达')
    def _queue_notifications(self):
        # Escalations are queued once, preserving the existing notification policy.
        engine=self.engine
        for item in self._read().get('items',[]):
            if item.get('status')!='awaiting_user' or not item.get('plan') or item['plan'].get('notification')!='needs_user_input':continue
            data=read_master(self.root)
            already=any(str(e.get('conversation_uid'))==item['uid'] and set(item.get('incoming_ids',[])) & set(map(str,e.get('incoming_message_ids',[]))) for e in data['reply_monitor'].get('notification_events',[]))
            if not already:
                app=application(data,item['uid']) or {}; messages=[m for m in app.get('conversation_messages',[]) if str(m.get('message_id')) in item.get('incoming_ids',[])]
                engine.notify(item['action_id'],'needs_user_input',item['row'],messages,item['plan'])

    def _deliver_notifications(self):
        from automation_state import _lock
        with _lock(self.root,'自动邮件执行器.lock',nonblocking=True):
            events=read_master(self.root)['reply_monitor'].get('notification_events',[])
            for event in events:
                if self.stop_event.is_set():return
                if event.get('status') in ('pending','dispatching','uncertain'):
                    self.engine.mail.deliver(event)
