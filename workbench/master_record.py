"""Atomic transactions on the original case record; no separate business database."""
from config import get_config
import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from automation_state import _lock, _write

MASTER='applications.json'
def now(): return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
def read_master(root):
    path=Path(root)/MASTER
    if path.is_symlink(): raise ValueError('主记录不能是链接')
    data=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data.get('applications'),list): raise ValueError('主记录结构异常')
    return data

def transaction(root, change):
    with _lock(root,'主记录写入.lock'):
        data=read_master(root); result=change(data)
        _write(Path(root)/MASTER,data)
        return result

def application(data,uid):
    return next((a for a in data['applications'] if str(a.get('conversation_uid'))==str(uid)),None)

def archive_history(root,row,messages):
    def change(data):
        app=application(data,row['uid'])
        if app is None:
            # Only adopt an invitation after the account has actually participated.
            if not any(str(m.get('from_uid'))==get_config().self_uid and (m.get('text') or m.get('body_type')==4) for m in messages):
                return None
            card=next((m['jobDesc'] for m in messages if m.get('jobDesc')), {})
            app=dict(company=row['company'],title=card.get('title',''),city=card.get('city','').split(' ')[0],job_id=row['job_id'],
                     conversation_uid=int(row['uid']),status='user_contact_verified',
                     origin='workbench_user_contact_adoption',identity_verified=True,
                     monitoring_enabled=True,monitoring_started_at=now(),
                     source_url='https://www.zhipin.com/job_detail/'+row['job_id']+'.html',
                     conversation_messages=[],job_card_snapshot=card)
            data['applications'].append(app)
        existing={str(m.get('message_id')) for m in app.setdefault('conversation_messages',[])}
        for message in messages:
            if str(message['message_id']) not in existing:
                item=dict(message,observed_at=now())
                item['actor']='user' if str(message.get('from_uid'))==get_config().self_uid else 'recruiter'
                item['kind']='outgoing_text' if item['actor']=='user' else 'incoming_text'
                if message.get('body_type') not in (1,7): item['kind']='platform_event'
                app['conversation_messages'].append(item)
                existing.add(str(message['message_id']))
        app['last_history_checked_at']=now()
        return app
    return transaction(root,change)

def observe(root, rows, *, history_reads=0):
    def change(data):
        monitor=data['reply_monitor']; byuid={str(b['conversation_uid']):b for b in monitor.get('baseline',[])}
        for row in rows:
            b=byuid.get(row['uid'])
            if b:
                b.update(last_observed_at=now(),last_list_observed_at=now(),
                         last_list_read_status=row['msg_status'])
        monitor['last_checked_at']=now()
        monitor['last_successful_check']={'at':now(),'conversation_count':len(rows),
            'covered_monitored_count':sum(r['uid'] in byuid for r in rows),
            'history_reads':history_reads,'executor':'workbench-app-server'}
    transaction(root,change)

def advance(root,row,messages):
    def change(data):
        if not application(data,row['uid']): return
        baselines=data['reply_monitor'].setdefault('baseline',[])
        b=next((b for b in baselines if str(b['conversation_uid'])==row['uid']),None)
        if b is None:
            b={'conversation_uid':int(row['uid']),'job_id':row['job_id'],'known_message_ids':[]}; baselines.append(b)
        b['known_message_ids']=list(dict.fromkeys(b.get('known_message_ids',[])+[str(m['message_id']) for m in messages]))
        b.update(last_list_message=row['last_msg'],last_list_message_timestamp=row['last_ts'],
                 last_list_read_status=row['msg_status'],last_list_observed_at=now(),last_observed_at=now())
        if messages: b['last_message_timestamp']=max(m.get('timestamp',0) for m in messages)
    transaction(root,change)

def save_action(root,uid,action):
    def change(data):
        app=application(data,uid)
        if app is None: raise ValueError('未核验当前会话')
        actions=app.setdefault('followup_actions',[])
        old=next((a for a in actions if a.get('action_id')==action['action_id']),None)
        if old: old.update(action)
        else: actions.append(action)
        app['send_stage']=action['status']
    transaction(root,change)

def mark_agent_message(root,uid,message,action_id):
    def change(data):
        app=application(data,uid)
        for m in app.get('conversation_messages',[]):
            if str(m['message_id'])==str(message['message_id']):
                m.update(actor='agent',action_id=action_id)
    transaction(root,change)
