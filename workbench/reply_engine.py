"""Serial, evidence-based reply execution. No unverified send is ever retried."""
from config import get_config
import hashlib
import json
import subprocess
import time
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from automation_state import AutomationState, EventStore
from boss_reader import BossReader, BossBlocked, message_fingerprint
from master_record import (now, read_master, application, archive_history,
                           advance, save_action, mark_agent_message, transaction)
from reply_planner import CodexPlanner
from reply_verifier import CodexVerifier
from mail_notifier import MailNotifier
from resume_policy import (request_mode, resume_key, delivered_receipt,
                           after_request, resume_send_attempt, superseding_request)

def event_key(row,messages):
    ids=','.join(sorted(str(m['message_id']) for m in messages))
    return f"reply:{row['job_id']}:{row['uid']}:{hashlib.sha256(ids.encode()).hexdigest()[:24]}"

def effective_text(m):
    return m.get('text') or (m.get('dialog') or {}).get('text') or (m.get('hyperLink') or {}).get('text') or ''

def message_time(message):
    try:
        return datetime.fromtimestamp(int(message['timestamp'])/1000,ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S 北京时间')
    except (KeyError,ValueError,TypeError,OverflowError,OSError):
        return '来信时间待核验'

def incoming(messages, known):
    return [m for m in messages if str(m['message_id']) not in known and
            str(m.get('from_uid'))!=get_config().self_uid and effective_text(m).strip() and m.get('body_type') in (1,7)]

class ReplyEngine:
    def __init__(self,root,reader=None,planner=None,verifier=None,bridge=None):
        self.root=Path(root); self.reader=reader or BossReader()
        self.state=AutomationState(root); self.events=EventStore(root)
        self.planner=planner or CodexPlanner(root); self.verifier=verifier or CodexVerifier(root)
        self.mail=MailNotifier(root); self.bridge=bridge or self.ui

    def ui(self, payload):
        return {'status': 'failed', 'reason': '需要显式注入官方页面执行器；旧写入入口已停用'}

    def allowed(self):
        # Only WorkbenchTasks installs its current-run authorization callback.
        return False

    def context(self,app,messages,new):
        data=read_master(self.root)
        auth=data['ongoing_search_authorization']
        policy={k:v for k,v in auth.items() if k not in ('manual_batches',)}
        return {'facts':(self.root/'facts.md').read_text(),
                'policy':policy,'job':{k:app.get(k) for k in ('company','title','city','salary','description','address','screening_note')},
                'history':messages[-24:],'incoming':[{**m,'text':effective_text(m)} for m in new]}

    def enrich_job(self,app,row,shadow):
        if app.get('description') and app.get('address'): return app
        detail=None
        if not detail:
            result=self.bridge({'kind':'job_detail','job_id':row['job_id'],'company':row['company']})
            if result.get('status')!='job_detail':
                if result.get('reason') in ('OFFICIAL_SAFETY_CHECK','BROWSER_OPERATION_FAILED'):
                    raise BossBlocked('读取职位正文时连接或安全验证异常。','OFFICIAL_SAFETY_CHECK')
                return app
            detail=result['data']
        fields={k:detail[k] for k in ('description','address','city','salary','title') if detail.get(k)}
        app={**app,**fields}
        if not shadow:
            def update(data): application(data,row['uid']).update(fields)
            transaction(self.root,update)
        return app

    def notify(self,key,category,row,new,plan,completed='尚未向招聘方发送'):
        app=application(read_master(self.root),row['uid']) or {}
        body=f"公司：{row['company']}\n岗位：{app.get('title','待核验')}\nhttps://www.zhipin.com/job_detail/{row['job_id']}.html\n\n"
        body+='相关来信：\n'+'\n'.join(f"{message_time(m)}：{effective_text(m)}" for m in new)
        body+=f"\n\n分析：{plan.get('reason','')}\n处理状态：{completed}\n"
        if plan.get('text'): body+='\n回复内容：\n'+plan['text']+'\n'
        if plan.get('question_for_user'): body+='\n需要你确认：'+plan['question_for_user']+'\n请在工作台查看待办，由本人确认或补充事实。'
        record=self.mail.enqueue(key,category,row['company'],body)
        if not record: return None
        ids=[str(m['message_id']) for m in new]
        if not ids:
            previous=self.events.get(key.removesuffix(':uncertain'))
            ids=previous.get('incoming_ids',[]) if previous else []
        def annotate(data):
            event=next(e for e in data['reply_monitor']['notification_events'] if e.get('event_id')==key)
            event.update(job_id=row['job_id'],conversation_uid=row['uid'],incoming_message_ids=ids,
                observed_at=now(),analysis=plan.get('reason',''),
                uncertain_fields=[plan['question_for_user']] if plan.get('question_for_user') else [])
            if category=='needs_user_input' and plan.get('text'):
                event.update(draft_text=plan['text'],draft_version='workbench-v1:'+record['stable_marker'])
            return dict(event)
        return transaction(self.root,annotate)

    def save_decision(self,key,row,incoming_ids,plan,status,created_at=None):
        """Archive handling decisions without claiming an outward message was sent."""
        action={'action_id':key,'type':'reply_decision','version':'workbench-v1',
                'status':status,'incoming_message_ids':[str(i) for i in incoming_ids],
                'reason':plan.get('reason',''),'question_for_user':plan.get('question_for_user',''),
                'text':plan.get('text',''),'actor':'agent','created_at':created_at or now()}
        def save(data):
            app=application(data,row['uid'])
            if app is None: raise ValueError('未核验当前会话')
            actions=app.setdefault('followup_actions',[])
            existing=next((a for a in actions if a.get('action_id')==key),None)
            if existing: return existing
            actions.append(action)
            return dict(action)
        return transaction(self.root,save)

    def payload(self,row,app,messages,plan,request_id='',request_mode='card'):
        cities=[m.get('jobDesc',{}).get('city','') for m in messages if m.get('jobDesc')]
        city=app.get('city') or next((c.split()[0] for c in cities if c),'')
        payload=dict(kind='resume' if plan['kind']=='resume' else 'text',uid=row['uid'],
            company=row['company'],recruiter=row['name'],job_id=row['job_id'],title=app.get('title',''),city=city,
            text=plan.get('text',''),request_id=request_id,
            known_message_ids=[str(m['message_id']) for m in messages])
        if plan['kind']=='resume':
            payload.update(request_mode=request_mode,attachment_name=get_config().attachment_name)
        return payload

    def resume_result(self,result,row,messages,new,request):
        if result['status'] in ('verified_sent','observed_sent','ignored','awaiting_user','failed','completed','superseded'):
            baseline=next((b for b in read_master(self.root)['reply_monitor'].get('baseline',[]) if str(b['conversation_uid'])==row['uid']),{})
            remaining=any(str(m['message_id'])!=str(request['message_id']) for m in incoming(messages,set(baseline.get('known_message_ids',[]))))
            # A resume receipt never consumes an unrelated question, including
            # one that arrived between our first and final history reads.
            advance(self.root,row,[request])
            if remaining:return {'status':'retry_remaining'}
        return result

    def observe_resume_attempt(self,key,row,action,messages,request):
        attempt=resume_send_attempt(messages,request,get_config().self_uid)
        if not attempt:return None
        # A manual/account-side action can precede the platform receipt. Persist
        # this uncertainty before returning, so restarts only reconcile it.
        action=dict(action,status='uncertain',actor='user_account_not_agent',
            observed_send_action_id=str(attempt['message_id']),
            observed_send_action_timestamp=attempt.get('timestamp'),
            issue='已观察到本人账号发起简历发送，附件送达回执待核验，禁止重复发送')
        archive_history(self.root,row,messages)
        save_action(self.root,row['uid'],action)
        return self.events.update(key,status='uncertain',action=action,reason=action['issue'])

    def process_resume(self,row,app,messages,new,request,known,shadow=False):
        """One durable request identity, irrespective of the changing inbound batch."""
        mode=request_mode(request)
        key=('shadow:' if shadow else '')+resume_key(row,str(request['message_id']))
        plan=dict(kind='resume',text='',reason='招聘方明确索要现有附件简历',evidence=[],notification='none',question_for_user='')
        payload=self.payload(row,app,messages,plan,str(request['message_id']),mode)
        action=dict(action_id=key,type='resume',version='workbench-v1',actor='agent',created_at=now(),
            status='authorized_pending_send',request_id=str(request['message_id']),request_mode=mode,
            attachment_name=get_config().attachment_name,job_id=row['job_id'],conversation_uid=row['uid'],
            incoming_message_ids=[str(request['message_id'])],
            authorization='ongoing_search_authorization.resume_request_authorization')
        previous=next((a for a in app.get('followup_actions',[]) if
            (a.get('type') in ('resume','resume_send') or a.get('action')=='resume_send') and
            a.get('attachment_name',get_config().attachment_name)==get_config().attachment_name and
            (str(a.get('request_id') or a.get('official_request_message_id') or '')==str(request['message_id']) or
             str(request['message_id']) in [str(i) for i in a.get('trigger_message_ids',[])]) and
            a.get('status') in ('dispatching','uncertain','verified_sent','observed_sent','failed')),None)
        status='failed' if previous and previous.get('status')=='failed' else 'uncertain' if previous else 'pending'
        ev=self.events.enqueue(key,uid=row['uid'],row=row,kind='resume',status=status,
            incoming_ids=[str(request['message_id'])],payload=payload,action=action)
        if shadow:
            return self.events.update(key,status='completed',shadow=True,plan=plan)
        if ev['status'] in ('dispatching','uncertain'):
            return self.resume_result(self.reconcile(ev),row,messages,new,request)
        if ev['status'] in ('completed','verified_sent','observed_sent','ignored','awaiting_user','superseded','failed'):
            return self.resume_result(ev,row,messages,new,request)
        auth=read_master(self.root)['ongoing_search_authorization'].get('resume_request_authorization',{})
        if not auth.get('enabled') or auth.get('attachment_name')!=get_config().attachment_name:
            plan.update(kind='escalate',notification='needs_user_input',reason='现有附件简历的发送授权或附件名称未确认',
                        question_for_user='请确认是否允许发送已配置的BOSS现有附件简历。')
            saved=self.save_decision(key,row,[request['message_id']],plan,'awaiting_user')
            self.notify(key,'needs_user_input',row,[request],plan)
            return self.resume_result(self.events.update(key,status='awaiting_user',action=saved),row,messages,new,request)
        # Always read the final full server history before deciding whether an
        # already-sent receipt or a new incoming message changed this request.
        fresh=sorted(self.reader.full_history(row['uid'],set()),key=lambda m:m.get('timestamp',0))
        receipt=delivered_receipt(fresh,request)
        payload=self.payload(row,app,fresh,plan,str(request['message_id']),mode)
        self.events.update(key,status='planned',plan=plan,payload=payload,action=action)
        if receipt:
            return self.resume_result(self.reconcile(self.events.get(key)),row,fresh,new,request)
        observed=self.observe_resume_attempt(key,row,action,fresh,request)
        if observed:return observed
        superseding=superseding_request(fresh,request)
        if superseding:
            reason='招聘方在索要后撤回或改变了简历要求，保留后续消息重新判断'
            action=dict(action,status='superseded',reason=reason,
                        superseded_by_message_id=str(superseding['message_id']))
            save_action(self.root,row['uid'],action)
            return self.resume_result(self.events.update(key,status='superseded',action=action,reason=reason),row,fresh,new,request)
        if any(str(m.get('from_uid'))==get_config().self_uid and m.get('body_type')==1 and effective_text(m) and after_request(m,request) for m in fresh):
            return self.resume_result(self.events.update(key,status='superseded',reason='本人已在请求后参与回复，交由新会话状态处理'),row,fresh,new,request)
        if mode=='text' and auth.get('text_resume_ui_verified') is not True:
            plan.update(kind='escalate',notification='needs_user_input',reason='纯文字简历请求尚没有已核验的官方附件发送入口，功能开关保持关闭',
                        question_for_user='待官方聊天页恢复并完成入口核验后，再处理这条现有简历请求；目前没有点击发送。')
            saved=self.save_decision(key,row,[request['message_id']],plan,'awaiting_user')
            self.notify(key,'needs_user_input',row,[request],plan)
            return self.resume_result(self.events.update(key,status='awaiting_user',action=saved),row,fresh,new,request)
        if {str(m['message_id']) for m in fresh}-{str(m['message_id']) for m in messages}:
            return self.events.update(key,status='planned',reason='发送前会话有新内容，先重新判断')
        if mode=='card' and request.get('dialog',{}).get('operated'):
            action.update(status='failed',issue='请求卡片已处理，但尚无可核验的附件送达回执，禁止重复点击')
            save_action(self.root,row['uid'],action)
            self.notify(key,'needs_user_input',row,[request],{'reason':action['issue'],'question_for_user':'请确认此会话的现有附件是否已送达。'})
            return self.resume_result(self.events.update(key,status='failed',action=action),row,fresh,new,request)
        if not self.allowed():return self.events.get(key)
        save_action(self.root,row['uid'],action)
        self.events.update(key,status='authorized_pending_send',action=action,payload=payload)
        if not self.allowed():return self.events.get(key)
        action.update(status='dispatching',dispatching_at=now())
        save_action(self.root,row['uid'],action)
        self.events.update(key,status='dispatching',action=action)
        self.state.update(in_flight={'event_key':key,'phase':'dispatching','company':row['company'],'since':now()})
        last=self.reader.full_history(row['uid'],set())
        if delivered_receipt(last,request):
            return self.resume_result(self.reconcile(self.events.get(key)),row,last,new,request)
        observed=self.observe_resume_attempt(key,row,action,last,request)
        if observed:return observed
        current_auth=read_master(self.root)['ongoing_search_authorization'].get('resume_request_authorization',{})
        permitted=current_auth.get('enabled') and current_auth.get('attachment_name')==get_config().attachment_name and (mode=='card' or current_auth.get('text_resume_ui_verified') is True)
        if not self.allowed() or not permitted or {str(m['message_id']) for m in last}-{str(m['message_id']) for m in fresh}:
            action['status']='authorized_pending_send'
            save_action(self.root,row['uid'],action)
            return self.events.update(key,status='authorized_pending_send',execution_not_started=True,action=action)
        ui=self.bridge(payload)
        self.events.update(key,ui_result=ui)
        if ui.get('reason') in ('NOT_AUTHORIZED_NOW','RESUME_HISTORY_CHANGED') and ui.get('clicked') is False:
            action['status']='authorized_pending_send'
            save_action(self.root,row['uid'],action)
            return self.events.update(key,status='authorized_pending_send',execution_not_started=True,
                                      action=action,reason=ui.get('reason'))
        if ui.get('reason') in ('OFFICIAL_SAFETY_CHECK','CONNECTION_FAILED','CHAT_PAGE_NOT_UNIQUE','BROWSER_OPERATION_FAILED','IDENTITY_OR_SAFETY_CHANGED'):
            action.update(status='uncertain',issue=ui.get('reason'))
            save_action(self.root,row['uid'],action)
            self.events.update(key,status='uncertain',action=action)
            raise BossBlocked('简历发送遇到连接或安全验证异常，已停止且不会重复发送。','OFFICIAL_SAFETY_CHECK')
        if ui.get('status') in ('blocked','blocked_user_confirmation','not_sent') and not ui.get('clicked'):
            action.update(status='failed',issue=ui.get('reason','当前官方界面未能核验现有附件发送路径'))
            save_action(self.root,row['uid'],action)
            self.notify(key,'needs_user_input',row,[request],{'reason':action['issue'],'question_for_user':'请确认此会话的官方附件发送入口；后台未重复点击。'})
            return self.resume_result(self.events.update(key,status='failed',action=action),row,last,new,request)
        return self.resume_result(self.reconcile(self.events.get(key)),row,last,new,request)

    def process(self,row,shadow=False):
        data=read_master(self.root); app=application(data,row['uid'])
        baseline=next((b for b in data['reply_monitor'].get('baseline',[]) if str(b['conversation_uid'])==row['uid']),{})
        known=set(baseline.get('known_message_ids',[]))
        messages=sorted(self.reader.full_history(row['uid'],known),key=lambda m:(m.get('timestamp',0),str(m['message_id'])))
        if app and str(app.get('job_id')) != row['job_id']:
            raise BossBlocked('同一联系人岗位发生变化，需先核对身份。','IDENTITY_CHANGED')
        if not shadow:
            app=archive_history(self.root,row,messages)
        elif not app:
            app={'company':row['company'],'title':row['title'],'job_id':row['job_id']}
        new=incoming(messages,known)
        if any(str(m.get('from_uid')) != row['uid'] for m in new):
            raise BossBlocked('新增文字的发送者与招聘方身份不一致，需核对来源。','UNKNOWN_MESSAGE_SOURCE')
        if not app or not new:
            if not shadow: advance(self.root,row,messages)
            return {'status':'ignored'}
        # Unsolicited historical invitations are not blanket permission to respond.
        if not app.get('monitoring_enabled') and not any(str(m.get('from_uid'))==get_config().self_uid for m in messages):
            return {'status':'ignored'}
        request=next((m for m in reversed(new) if request_mode(m)),None)
        if request:
            return self.process_resume(row,app,messages,new,request,known,shadow)
        key=('shadow:' if shadow else '')+event_key(row,new)
        ev=self.events.enqueue(key,uid=row['uid'],row=row,kind='reply',incoming_ids=[str(m['message_id']) for m in new])
        if ev['status'] in ('dispatching','uncertain'):
            return self.reconcile(ev)
        if ev['status'] in ('completed','verified_sent','observed_sent','ignored','awaiting_user','superseded','failed'):
            if not shadow: advance(self.root,row,messages)
            return ev
        latest_in=max(m.get('timestamp',0) for m in new)
        has_question=any(m.get('body_type')==1 for m in new)
        if any(str(m.get('from_uid'))==get_config().self_uid and m.get('timestamp',0)>latest_in and
               ((m.get('body_type')==1 and m.get('text')) or (not has_question and m.get('body_type') in (4,12))) for m in messages):
            self.events.update(key,status='superseded',reason='本人或代理已处理来信')
            if not shadow: advance(self.root,row,messages)
            return {'status':'superseded'}
        self.state.update(in_flight={'event_key':key,'phase':'planning','company':row['company'],'since':now()})
        request=None
        if not request: app=self.enrich_job(app,row,shadow)
        context=self.context(app,messages,new)
        try:
            plan={'kind':'resume','text':'','reason':'当前会话招聘方明确请求既有附件简历','evidence':[],
                  'notification':'none','question_for_user':''} if request else self.planner.plan(context)
            if plan['kind']=='resume' and not request:
                plan.update(kind='escalate',text='',notification='needs_user_input',
                    reason='来信不是可独立处理的明确简历请求，可能还包含其他问题或附件要求；尚未发送',
                    question_for_user='请确认对方所需内容；现有简历不能代替回答同条来信中的其他问题。')
            if plan['kind']=='reply':
                if not app.get('description') or not app.get('address'):
                    raise ValueError('完整职位正文或地址未能核验')
                verification=self.verifier.verify(context,plan)
                if not verification['approved']:
                    plan.update(kind='escalate',notification='needs_user_input',reason=verification['reason'],
                                question_for_user='请确认这条来信的事实或安排；建议稿尚未发送。')
        except (ValueError,RuntimeError,OSError,subprocess.SubprocessError):
            plan={'kind':'escalate','text':'','reason':'回复生成或事实核验暂未通过，已保留来信且未发送',
                  'notification':'needs_user_input','question_for_user':'这条消息暂需你协助判断。'}
        self.events.update(key,status='planned',plan=plan)
        if shadow:
            return self.events.update(key,status='completed',shadow=True)
        if plan['kind'] in ('ignore','escalate'):
            decision_status='awaiting_user' if plan['kind']=='escalate' else 'ignored'
            action=self.save_decision(key,row,[m['message_id'] for m in new],plan,decision_status)
            if plan['notification']!='none': self.notify(key,plan['notification'],row,new,plan)
            result=self.events.update(key,status=decision_status,action=action)
            advance(self.root,row,messages)
            return result
        if not self.allowed(): return self.events.update(key,status='planned',reason='当前暂停或尚未切换执行器')
        fresh=sorted(self.reader.full_history(row['uid'],known),key=lambda m:m.get('timestamp',0))
        if {str(m['message_id']) for m in fresh}-{str(m['message_id']) for m in messages}:
            return self.events.update(key,status='superseded',reason='拟稿后会话有新内容，重新判断')
        payload=self.payload(row,app,fresh,plan,str(request['message_id']) if request else '')
        action={'action_id':key,'type':'resume' if plan['kind']=='resume' else 'reply',
                'version':'workbench-v1','text':plan.get('text',''),'actor':'agent','created_at':now(),
                'authorization':'ongoing_search_authorization.reply_authorization/resume_request_authorization',
                'status':'authorized_pending_send'}
        save_action(self.root,row['uid'],action)
        self.events.update(key,status='authorized_pending_send',action=action,payload=payload)
        if not self.allowed(): return self.events.get(key)
        action.update(status='dispatching',dispatching_at=now())
        save_action(self.root,row['uid'],action)
        self.events.update(key,status='dispatching',action=action)
        self.state.update(in_flight={'event_key':key,'phase':'dispatching','company':row['company'],'since':now()})
        if not self.allowed():
            action['status']='authorized_pending_send'
            save_action(self.root,row['uid'],action)
            return self.events.update(key,status='authorized_pending_send',execution_not_started=True,action=action)
        ui=self.bridge(payload)
        self.events.update(key,ui_result=ui)
        if ui.get('reason')=='NOT_AUTHORIZED_NOW' and ui.get('clicked') is False:
            action['status']='authorized_pending_send'
            save_action(self.root,row['uid'],action)
            return self.events.update(key,status='authorized_pending_send',execution_not_started=True,action=action)
        if ui.get('reason') in ('OFFICIAL_SAFETY_CHECK','CONNECTION_FAILED','CHAT_PAGE_NOT_UNIQUE','BROWSER_OPERATION_FAILED','IDENTITY_OR_SAFETY_CHANGED'):
            self.events.update(key,status='uncertain')
            raise BossBlocked('官方页面连接或安全验证异常，已停止发送。','OFFICIAL_SAFETY_CHECK')
        if ui.get('status') in ('blocked','blocked_user_confirmation','not_sent') and not ui.get('clicked'):
            action.update(status='failed',issue=ui.get('reason','页面核验未通过'))
            save_action(self.root,row['uid'],action)
            self.events.update(key,status='failed',action=action)
            self.notify(key,'needs_user_input',row,new,{'reason':action['issue'],'question_for_user':'请在工作台查看此会话。'})
            return action
        result=self.reconcile(self.events.get(key))
        if result['status']=='verified_sent' and plan['notification']!='none':
            self.notify(key,plan['notification'],row,new,plan,completed='已自动回复并核验送达')
        if request and result['status'] in ('verified_sent','observed_sent') and any(m['message_id']!=request['message_id'] for m in new):
            # Sending an attachment does not answer the text arriving beside it.
            advance(self.root,row,[request])
            return {'status':'retry_remaining'}
        advance(self.root,row,messages)
        return result

    def reconcile(self,event):
        payload=event.get('payload'); row=event.get('row')
        if not payload or not row:
            return self.events.update(event['event_key'],status='uncertain',reason='缺少核验信息，禁止重发')
        messages=sorted(self.reader.full_history(row['uid'],set()),key=lambda m:m.get('timestamp',0))
        old=set(payload.get('known_message_ids',[]))
        candidates=[m for m in messages if str(m['message_id']) not in old and
                    (str(m.get('from_uid'))==get_config().self_uid if payload['kind']=='text' else True)]
        if payload['kind']=='text':
            found=next((m for m in candidates if m.get('text')==payload['text']),None)
        else:
            request=next((m for m in messages if str(m['message_id'])==str(payload.get('request_id'))),None)
            found=delivered_receipt(messages,request) if request else None
        if found:
            ui=self.bridge({**payload,'kind':'verify','message_ids':[str(found['message_id'])]})
            if ui.get('reason') in ('OFFICIAL_SAFETY_CHECK','BROWSER_OPERATION_FAILED','CONNECTION_FAILED','CHAT_PAGE_NOT_UNIQUE','IDENTITY_OR_SAFETY_CHANGED'):
                raise BossBlocked('送达核验遇到连接或安全验证异常，停止BOSS操作。','OFFICIAL_SAFETY_CHECK')
            if ui.get('status')=='verified':
                archive_history(self.root,row,messages)
                clicked=event.get('ui_result',{}).get('clicked') is True
                status='verified_sent' if clicked else 'observed_sent'
                action=dict(event.get('action',{}),status=status,message_id=str(found['message_id']),
                            server_message_timestamp=found.get('timestamp'),sent_at=now(),
                            verification={'method':'official_UI+server_history','ui':ui,'observed_at':now()})
                action.setdefault('action_id',event['event_key'])
                if not clicked: action['actor']='user_account_not_agent'
                save_action(self.root,row['uid'],action)
                if clicked and payload['kind']=='text': mark_agent_message(self.root,row['uid'],found,event['event_key'])
                if payload['kind']=='resume':
                    def mark_receipt(data):
                        app=application(data,row['uid'])
                        for message in app.get('conversation_messages',[]):
                            if str(message.get('message_id'))==str(found['message_id']):
                                message.update(actor='platform',kind='platform_event')
                    transaction(self.root,mark_receipt)
                self.state.update(last_action={'kind':payload['kind'],'at':now(),'company':row['company'],'status':status})
                return self.events.update(event['event_key'],status=status,action=action)
        if event.get('action'):
            action=dict(event['action'],status='uncertain',issue='UI或服务器结果未知，禁止重发')
            save_action(self.root,row['uid'],action)
            self.events.update(event['event_key'],action=action)
        self.notify(event['event_key']+':uncertain','needs_user_input',row,[],
                    {'reason':'发送结果暂未完成双重核验，已禁止自动重发','question_for_user':'请检查该会话实际是否送达。'})
        return self.events.update(event['event_key'],status='uncertain',reason='UI或服务器结果未知，禁止重发')
