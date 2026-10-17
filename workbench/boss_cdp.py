"""Direct CDP adapter for the existing official BOSS chat page.

Only read endpoints observed in the official page are used. Writes are clicks
on that page's real controls. No browser launch, cookies export, proxy change,
endpoint send, reconnect or send retry is implemented here.
"""
from __future__ import annotations

from config import get_config
import json
from pathlib import Path
import re
import threading
import time
import urllib.parse
import urllib.request

from boss_reader import BossBlocked, normalize_message, normalize_row, message_fingerprint
from resume_policy import (after_request, delivered_receipt, official_receipt,
                           request_mode, resume_send_attempt, superseding_request)

CHAT = 'https://www.zhipin.com/web/geek/chat'
FRIENDS = '/wapi/zprelation/friend/getGeekFriendList.json'
HISTORY = '/wapi/zpchat/geek/historyMsg'
_SAFETY = r'访问环境异常|安全验证|账号异常|请先登录|请完成验证|操作过于频繁'


def _blocked(code):
    reason = {
        'INCOMPLETE_HISTORY': 'BOSS 会话历史分页不完整，无法确认历史覆盖范围；本轮已停止。',
        'INCOMPLETE_PAGINATION': 'BOSS 会话列表分页不完整，未发布部分列表；本轮已停止。',
        'INCONSISTENT_PAGINATION': 'BOSS 会话列表在分页期间发生变化，未发布不一致列表；本轮已停止。',
    }.get(code, 'BOSS 官方页面操作未通过核验；已停止且不会自动重试。')
    return BossBlocked(reason, code)


def _normal(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def _official_chat(url):
    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme == 'https' and parsed.netloc == 'www.zhipin.com' and parsed.path.rstrip('/') == '/web/geek/chat'


class _CDPSession:
    """One target websocket; transient URLs and protocol errors never escape."""
    def __init__(self, target, stop_event=None):
        import websocket
        config = get_config().require_live()
        self.stop_event = stop_event
        self.target_id = target['id']
        self.seq = 0
        url = urllib.parse.urlsplit(target.get('webSocketDebuggerUrl', ''))
        if (url.scheme != 'ws' or url.hostname != '127.0.0.1'
                or url.username or url.password or url.port != urllib.parse.urlsplit(config.cdp_url).port):
            raise _blocked('INVALID_CDP_TARGET')
        try:
            self.ws = websocket.create_connection(target['webSocketDebuggerUrl'], timeout=5,
                suppress_origin=True, http_no_proxy=['127.0.0.1', 'localhost'])
        except Exception:
            raise _blocked('CONNECTION_FAILED') from None

    def check_stop(self):
        if self.stop_event is not None and self.stop_event.is_set():
            raise _blocked('STOPPED')

    def call(self, method, params=None, *, timeout=10, cleanup=False):
        import websocket
        if not cleanup:
            self.check_stop()
        self.seq += 1
        seq = self.seq
        try:
            self.ws.send(json.dumps({'id': seq, 'method': method, 'params': params or {}}))
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if not cleanup:
                    self.check_stop()
                self.ws.settimeout(min(.25, max(.01, end - time.monotonic())))
                try:
                    answer = json.loads(self.ws.recv())
                except websocket.WebSocketTimeoutException:
                    continue
                if answer.get('id') != seq:
                    continue
                if answer.get('error'):
                    raise _blocked('BROWSER_OPERATION_FAILED')
                return answer.get('result', {})
        except BossBlocked:
            raise
        except Exception:
            raise _blocked('CONNECTION_FAILED') from None
        raise _blocked('CONNECTION_FAILED')

    def evaluate(self, expression):
        result = self.call('Runtime.evaluate', {'expression': expression, 'returnByValue': True,
            'awaitPromise': True, 'timeout': 8000})
        if result.get('exceptionDetails'):
            raise _blocked('BROWSER_OPERATION_FAILED')
        return result.get('result', {}).get('value')

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def _targets():
    try:
        config = get_config().require_live()
        # This bypass applies only to this loopback request, never system config.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(config.cdp_url.rstrip('/') + '/json/list', timeout=3) as response:
            value = json.load(response)
        if not isinstance(value, list):
            raise ValueError()
        return value
    except Exception:
        raise _blocked('CONNECTION_FAILED') from None


_PAGE_CHECK = """(() => ({url:location.href, focus:document.hasFocus(),
  name:document.querySelector('.nav-figure')?.innerText?.trim()||'',
  safety:/""" + _SAFETY + """/.test(document.body.innerText)}))()"""

_SNAPSHOT = r"""(() => {
  const n=v=>String(v||'').replace(/\s+/g,' ').trim();
  const head=document.querySelector('.chat-conversation .top-info-content');
  const base=head?.querySelector('.base-info');
  const company=base?.querySelector('.name-content > span:not(.name-text)') ||
    [...(base?.querySelectorAll('span')||[])].find(e=>!e.className&&e.innerText);
  const selected=document.querySelector('.friend-content.selected');
  let source=null;for(let e=selected;e&&!source;e=e.parentElement)source=e.__vue__?.$props?.source;
  const evidence=[...document.querySelectorAll('.chat-conversation [data-mid]')].map(e=>({
    message_id:e.getAttribute('data-mid'),actor:e.classList.contains('item-myself')?'self':e.classList.contains('item-system')?'system':'other',
    text:[...e.querySelectorAll('.text-content')].map(t=>t.innerText).join('\n')||e.innerText,
    delivery:e.querySelector('.message-status')?.innerText||'',
    resume_receipt:new RegExp('您的附件简历\\s+'+__ATTACHMENT_PATTERN__+'(?:\\.pdf)?\\s+已发送给Boss').test(e.innerText)}));
  return {uid:String(source?.uid||''),job_id:String(source?.encryptJobId||''),
    recruiter:n(head?.querySelector('.name-text')?.innerText),company:n(company?.innerText),
    title:n(head?.querySelector('.position-name')?.innerText),city:n(head?.querySelector('.city')?.innerText),
    message_ids:evidence.map(e=>e.message_id),evidence,composer_text:document.querySelector('#chat-input')?.innerText||''};
})()"""


def _snapshot_script():
    return _SNAPSHOT.replace('__ATTACHMENT_PATTERN__', json.dumps(re.escape(get_config().attachment_name), ensure_ascii=False))


class BossCDPReader:
    def __init__(self, root, stop_event=None, *, max_pages=30, session_factory=_CDPSession,
                 target_provider=_targets, preferred_target_id=None):
        self.root = Path(root)
        self.stop_event = stop_event
        self.max_pages = max_pages
        self.session_factory = session_factory
        self.target_provider = target_provider
        if preferred_target_id is not None and (not isinstance(preferred_target_id, str) or not preferred_target_id.strip()):
            raise ValueError('无效的已核验浏览器页面标识')
        self.preferred_target_id = preferred_target_id
        self.session = None
        self.rows = {}
        self.row_pages = {}
        self.lock = threading.RLock()
        self.failure = None
        self._bridge = None

    @property
    def target_id(self):
        """The observed binding, available for the controller's local record."""
        return self.session.target_id if self.session else None

    def check_stop(self):
        if self.stop_event is not None and self.stop_event.is_set():
            raise _blocked('STOPPED')
        if self.failure:
            raise _blocked(self.failure)

    def _connect(self):
        self.check_stop()
        if self.session:
            return self.session
        targets = [t for t in self.target_provider() if t.get('type') == 'page' and _official_chat(t.get('url', ''))]
        if self.preferred_target_id is not None:
            exact = [t for t in targets if t.get('id') == self.preferred_target_id]
            if len(exact) != 1:
                raise _blocked('CHAT_TARGET_NOT_FOUND')
            self.session = self.session_factory(exact[0], self.stop_event)
            return self.session
        if len(targets) == 1:
            self.session = self.session_factory(targets[0], self.stop_event)
            return self.session
        # The browser may have old chat tabs. Choose only one focused correct
        # account page once; subsequent operations never depend on focus.
        matches = []
        try:
            for target in targets:
                session = self.session_factory(target, self.stop_event)
                try:
                    page = session.evaluate(_PAGE_CHECK)
                    if page.get('focus') and page.get('safety'):
                        self.failure = 'OFFICIAL_SAFETY_CHECK'
                        raise _blocked(self.failure)
                    if page.get('focus') and page.get('name') != get_config().self_name:
                        raise _blocked('SELF_IDENTITY_MISMATCH')
                    if page.get('focus') and page.get('name') == get_config().self_name and not page.get('safety'):
                        matches.append(session)
                    else:
                        session.close()
                except Exception:
                    session.close()
                    raise
            if len(matches) != 1:
                raise _blocked('CHAT_PAGE_NOT_UNIQUE')
            self.session = matches.pop()
            return self.session
        finally:
            for session in matches:
                session.close()

    def _evaluate(self, expression):
        session = self._connect()
        self.check_stop()
        page = session.evaluate(_PAGE_CHECK)
        if not isinstance(page, dict) or not _official_chat(page.get('url', '')):
            raise _blocked('CHAT_PAGE_CHANGED')
        if page.get('safety'):
            self.failure = 'OFFICIAL_SAFETY_CHECK'
            raise _blocked(self.failure)
        if page.get('name') != get_config().self_name:
            raise _blocked('SELF_IDENTITY_MISMATCH')
        self.check_stop()
        value = session.evaluate(expression)
        if isinstance(value, dict) and 'code' in value and value['code'] != 0:
            self.failure = 'OFFICIAL_SAFETY_CHECK' if str(value['code']) in ('36', '37') else 'BOSS_READ_FAILED'
            raise _blocked(self.failure)
        return value

    def _read_page(self, page):
        # Tokens stay within the page. Only these explicit fields cross CDP.
        return self._evaluate("""(async()=>{
          const response=await fetch(%s,{credentials:'include',cache:'no-store'});
          if(!response.ok)return {code:-1};const d=await response.json();
          if(d.code!==0)return {code:d.code};const xs=d.zpData?.result;
          if(xs!==undefined&&!Array.isArray(xs))return {code:-1};
          return {code:0,rows:(xs||[]).map(x=>({uid:String(x.uid||''),name:x.name,company:x.brandName,
            title:x.title,job_id:x.encryptJobId,last_msg:x.lastMsg,last_ts:x.lastTS,
            msg_status:x.lastMessageInfo?.status===2?'已读':x.lastMessageInfo?.status===1?'送达':'',
            unread:x.unreadMsgCount,from_uid:String(x.lastMessageInfo?.fromId||''),to_uid:String(x.lastMessageInfo?.toId||'')}))};
        })()""" % json.dumps(FRIENDS + '?page=' + str(page)))

    def list_conversations(self):
        with self.lock:
            seen, pages, signatures = {}, {}, set()
            for page in range(1, self.max_pages + 1):
                packet = self._read_page(page)
                raw = packet.get('rows') if isinstance(packet, dict) else None
                if not isinstance(raw, list):
                    raise _blocked('INVALID_RESPONSE')
                if not raw:
                    self.rows, self.row_pages = seen, pages
                    return list(seen.values())
                rows = []
                for item in raw:
                    if not isinstance(item, dict) or not str(item.get('uid', '')).isdigit():
                        raise _blocked('INVALID_RESPONSE')
                    participants = {str(item.get('from_uid', '')), str(item.get('to_uid', ''))}
                    if participants != {get_config().self_uid, str(item['uid'])}:
                        raise _blocked('SELF_IDENTITY_MISMATCH')
                    rows.append(normalize_row(item))
                signature = tuple((r['uid'], message_fingerprint(r)) for r in rows)
                if signature in signatures:
                    raise _blocked('INCOMPLETE_PAGINATION')
                signatures.add(signature)
                for row in rows:
                    uid = row['uid']
                    if uid in seen and seen[uid] != row:
                        raise _blocked('INCONSISTENT_PAGINATION')
                    seen[uid], pages[uid] = row, page
            raise _blocked('INCOMPLETE_PAGINATION')

    def _history_page(self, uid, page, max_msg_id='0'):
        if uid not in self.rows:
            self.list_conversations()
        if uid not in self.rows:
            raise _blocked('RECIPIENT_NOT_FOUND')
        args = {'uid': uid, 'page': page, 'maxMsgId': str(max_msg_id),
                'listPage': self.row_pages[uid], 'jobId': self.rows[uid]['job_id']}
        return self._evaluate(r"""(async()=>{
          const a=%s;const d=await (await fetch(%s+'?page='+a.listPage,{credentials:'include',cache:'no-store'})).json();
          if(d.code!==0)return {code:d.code};
          const rows=(d.zpData?.result||[]).filter(x=>String(x.uid)===a.uid&&x.encryptJobId===a.jobId);
          if(rows.length!==1)return {code:-1};const f=rows[0];
          if(!f.encryptBossId||!f.securityId)return {code:-1};
          const q=new URLSearchParams({bossId:f.encryptBossId,maxMsgId:a.maxMsgId,c:'20',page:String(a.page),src:String(f.friendSource||0),securityId:f.securityId});
          const h=await (await fetch(%s+'?'+q,{credentials:'include',cache:'no-store'})).json();
          if(h.code!==0)return {code:h.code};if(!Array.isArray(h.zpData?.messages))return {code:-1};
          const cleanBody=b=>{const o={type:b?.type,text:b?.text};for(const [k,keys] of Object.entries({
            dialog:['title','text','operated','type'],hyperLink:['text','title'],
            jobDesc:['title','jobName','company','brandName','city','salary','experience','education','bottomText','jobId'],
            action:['aid','text','title']})){if(b?.[k])o[k]=Object.fromEntries(keys.filter(x=>x in b[k]).map(x=>[x,b[k][x]]));}return o;};
          return {code:0,hasMore:h.zpData.hasMore,minMsgId:String(h.zpData.minMsgId||'0'),messages:h.zpData.messages.map(m=>({mid:String(m.mid),time:m.time,status:m.status,
            from:{uid:String(m.from?.uid||''),name:m.from?.name||''},to:{uid:String(m.to?.uid||''),name:m.to?.name||''},body:cleanBody(m.body)}))};
        })()""" % (json.dumps(args), json.dumps(FRIENDS), json.dumps(HISTORY)))

    def full_history(self, uid, known_ids=(), *, max_pages=30):
        uid = str(uid)
        if not uid.isdigit() or uid == get_config().self_uid:
            raise ValueError('无效的会话 UID')
        with self.lock:
            known, seen = {str(i) for i in known_ids}, {}
            cursor = '0'
            for page in range(1, max_pages + 1):
                packet = self._history_page(uid, page, cursor)
                messages = packet.get('messages') if isinstance(packet, dict) else None
                if not isinstance(messages, list) or type(packet.get('hasMore')) is not bool:
                    raise _blocked('INVALID_RESPONSE')
                fresh = set()
                for raw in messages:
                    if not isinstance(raw, dict):
                        raise _blocked('INVALID_RESPONSE')
                    sender, recipient = raw.get('from', {}), raw.get('to', {})
                    if {str(sender.get('uid')), str(recipient.get('uid'))} != {get_config().self_uid, uid}:
                        raise _blocked('HISTORY_IDENTITY_MISMATCH')
                    person = sender if str(sender.get('uid')) == get_config().self_uid else recipient
                    if person.get('name') != get_config().self_name:
                        raise _blocked('SELF_IDENTITY_MISMATCH')
                    item = normalize_message({'raw': raw, 'body': raw.get('body', {}),
                        'from': '本人' if str(sender.get('uid')) == get_config().self_uid else '招聘方'})
                    if not item['message_id'].isdigit():
                        raise _blocked('INVALID_RESPONSE')
                    if item['message_id'] not in seen:
                        fresh.add(item['message_id'])
                    seen[item['message_id']] = item
                if known.intersection(seen) or packet['hasMore'] is False:
                    return sorted(seen.values(), key=lambda m: (m['timestamp'], int(m['message_id'])))
                if not fresh:
                    raise _blocked('INCOMPLETE_HISTORY')
                # Official message-list.getHistoryMessage (observed 2026-10-16)
                # sends msgMinId as maxMsgId, then updates it from the server's
                # minMsgId or the smallest returned mid. page alone repeats the
                # latest batch on this endpoint.
                returned_cursor = str(packet.get('minMsgId') or '0')
                if not returned_cursor.isdigit():
                    raise _blocked('INVALID_RESPONSE')
                next_cursor = int(returned_cursor) or min(int(m['mid']) for m in messages)
                if next_cursor <= 0 or (cursor != '0' and next_cursor >= int(cursor)):
                    raise _blocked('INCOMPLETE_HISTORY')
                cursor = str(next_cursor)
            raise _blocked('INCOMPLETE_HISTORY')

    def snapshot(self):
        with self.lock:
            return self._evaluate(_snapshot_script())

    def bridge(self, payload):
        # Read-only by default. A controller must explicitly supply an
        # authorization callback to BossCDPBridge for dispatch operations.
        if self._bridge is None:
            self._bridge = BossCDPBridge(self.root, self, self.stop_event)
        return self._bridge(payload)

    def close(self):
        with self.lock:
            if self.session:
                self.session.close()
                self.session = None


def validate_action(payload):
    if not isinstance(payload, dict):
        raise _blocked('INVALID_ACTION')
    a = dict(payload)
    if a.get('kind') == 'job_detail':
        if not re.fullmatch(r'[A-Za-z0-9_-]{10,100}', str(a.get('job_id', ''))):
            raise _blocked('INVALID_JOB_ID')
        return a
    if a.get('kind') not in ('inspect', 'verify', 'verify_text', 'verify_resume', 'text', 'resume'):
        raise _blocked('INVALID_KIND')
    for key in ('uid', 'job_id', 'company', 'recruiter', 'title', 'city'):
        if not isinstance(a.get(key), str) or not a[key].strip():
            raise _blocked('MISSING_IDENTITY')
    if not a['uid'].isdigit() or a['uid'] == get_config().self_uid:
        raise _blocked('INVALID_UID')
    ids = a.get('known_message_ids')
    if not isinstance(ids, list) or not ids or any(not str(i).isdigit() for i in ids):
        raise _blocked('MISSING_IDENTITY_BASELINE')
    a['known_message_ids'] = list(dict.fromkeys(map(str, ids)))
    if a['kind'] == 'text' and (not isinstance(a.get('text'), str) or not a['text'].strip() or len(a['text']) > 3000):
        raise _blocked('INVALID_TEXT')
    if a['kind'] in ('resume', 'verify_resume') and str(a.get('request_id')) not in a['known_message_ids']:
        raise _blocked('INVALID_REQUEST_ID')
    if a['kind'].startswith('verify') and (not isinstance(a.get('message_ids'), list) or not a['message_ids'] or any(not str(i).isdigit() for i in a['message_ids'])):
        raise _blocked('INVALID_VERIFY_IDS')
    return a


def identity_matches(action, snapshot):
    return (all(_normal(snapshot.get(k)) == _normal(action.get(k)) for k in
                ('uid', 'job_id', 'company', 'recruiter', 'title', 'city')) and
            bool(set(action['known_message_ids']) & set(snapshot.get('message_ids', []))))


class BossCDPBridge:
    def __init__(self, root, reader=None, stop_event=None, authorize=None):
        self.root = Path(root)
        self.reader = reader or BossCDPReader(root, stop_event)
        self.stop_event = stop_event or self.reader.stop_event
        self.authorize = authorize
        self.attempted = set()

    def _authorized(self):
        self.reader.check_stop()
        if self.stop_event is not None and self.stop_event.is_set():
            raise _blocked('STOPPED')
        if not self.authorize or self.authorize() is not True:
            raise _blocked('NOT_AUTHORIZED_NOW')

    def _select(self, a):
        # Read the official virtual-row props for exact UID/job identity, then
        # click only the normal row. No internal send method is called.
        for attempt in range(20):
            result = self.reader._evaluate(r"""(()=>{const a=%s;
              const source=e=>{for(let n=e;n;n=n.parentElement){if(n.__vue__?.$props?.source)return n.__vue__.$props.source;}return {};};
              const rows=[...document.querySelectorAll('.friend-content')];
              const hits=rows.filter(e=>{const s=source(e);return String(s.uid)===a.uid&&s.encryptJobId===a.job_id&&s.name===a.recruiter&&s.brandName===a.company;});
              if(hits.length===1){if(!hits[0].classList.contains('selected'))hits[0].click();return {found:true};}
              if(hits.length>1)return {error:true};
              let scroll=document.querySelector('.user-list-content');
              while(scroll&&!(scroll.scrollHeight>scroll.clientHeight&&['auto','scroll'].includes(getComputedStyle(scroll).overflowY)))scroll=scroll.parentElement;
              if(!scroll)return {end:true};const before=scroll.scrollTop;
              scroll.scrollTop=%s===0?0:scroll.scrollTop+Math.max(100,scroll.clientHeight*.8);
              return {end:%s>0&&before===scroll.scrollTop};})()""" % (json.dumps(a, ensure_ascii=False), attempt, attempt))
            if result.get('error') or result.get('end'):
                raise _blocked('RECIPIENT_NOT_UNIQUE')
            if result.get('found'):
                break
            self._pause(.08)
        else:
            raise _blocked('RECIPIENT_NOT_FOUND')
        end = time.monotonic() + 6
        while time.monotonic() < end:
            snap = self.reader.snapshot()
            if identity_matches(a, snap):
                return snap
            self._pause(.1)
        raise _blocked('IDENTITY_MISMATCH')

    def _pause(self, seconds):
        if self.stop_event:
            self.stop_event.wait(seconds)
        else:
            time.sleep(seconds)
        self.reader.check_stop()

    def _attachment(self):
        session = self.reader._connect()
        target = session.call('Target.createTarget', {'url': 'https://www.zhipin.com/web/geek/resume'})
        page = None
        try:
            targets = self.reader.target_provider()
            exact = [t for t in targets if t.get('id') == target['targetId']]
            if len(exact) != 1:
                raise _blocked('RESUME_IDENTITY_NOT_VERIFIED')
            page = self.reader.session_factory(exact[0], self.stop_event)
            end = time.monotonic() + 8
            while time.monotonic() < end:
                data = page.evaluate(r"""(()=>({url:location.origin+location.pathname,
                  safety:/访问环境异常|安全验证|账号异常|请先登录|请完成验证/.test(document.body.innerText),
                  name:document.querySelector('.nav-figure')?.innerText.trim()||'',
                  attachments:[...document.querySelectorAll('.resume-attachment .annex-item')].map(e=>({
                    name:e.querySelector('.annex-content .basis a')?.innerText.trim()||'',
                    metadata:e.querySelector('.annex-content .desc')?.innerText.trim()||''}))}))()""")
                if data.get('safety'):
                    self.reader.failure = 'OFFICIAL_SAFETY_CHECK'
                    raise _blocked('OFFICIAL_SAFETY_CHECK')
                if data.get('attachments'):
                    if data.get('url') != 'https://www.zhipin.com/web/geek/resume' or data.get('name') != get_config().self_name or len(data['attachments']) != 1 or data['attachments'][0]['name'] != get_config().attachment_name + '.pdf':
                        raise _blocked('RESUME_IDENTITY_NOT_VERIFIED')
                    return {**data['attachments'][0], 'count': 1, 'source': 'official_resume_attachment_ui'}
                self._pause(.15)
            raise _blocked('RESUME_IDENTITY_NOT_VERIFIED')
        finally:
            if page:
                page.close()
            # Close only the temporary target we created, even after Stop.
            session.call('Target.closeTarget', {'targetId': target['targetId']}, cleanup=True)

    def _job_detail(self, a):
        url = 'https://www.zhipin.com/job_detail/' + a['job_id'] + '.html'
        data = self.reader._evaluate(r"""(async()=>{const url=%s;const r=await fetch(url,{credentials:'include',cache:'no-store'});
          if(!r.ok||r.url.split(/[?#]/)[0]!==url)return {error:'JOB_URL_CHANGED'};
          const doc=new DOMParser().parseFromString(await r.text(),'text/html');
          if(/访问环境异常|安全验证|请先登录|请完成验证/.test(doc.body.innerText||doc.body.textContent))return {error:'OFFICIAL_SAFETY_CHECK'};
          const t=s=>doc.querySelector(s)?.textContent.trim()||'';
          return {title:t('.job-primary .info-primary h1'),salary:t('.job-primary .salary'),city:t('.job-primary .text-city'),
            company:t('.sider-company .company-info'),description:t('.job-sec-text'),address:t('.company-address .location-address')};})()""" % json.dumps(url))
        if data.get('error'):
            if data['error'] == 'OFFICIAL_SAFETY_CHECK':
                self.reader.failure = data['error']
            raise _blocked(data['error'])
        if any(not data.get(k) for k in ('title', 'salary', 'city', 'company', 'description', 'address')):
            raise _blocked('JOB_DETAIL_INCOMPLETE')
        if any(a.get(k) and _normal(a[k]) != _normal(data[k]) for k in ('company', 'title')):
            raise _blocked('JOB_DETAIL_IDENTITY_MISMATCH')
        return {'status': 'job_detail', 'clicked': False, 'data': {**data, 'job_id': a['job_id'],
            'official_url': url, 'source': 'official_job_detail_html'}}

    def _click(self, a, *, confirmation=False):
        # The stop/owner callback is checked immediately before this one atomic
        # browser task; the task rechecks identity and complete visible history.
        self._authorized()
        return self.reader._evaluate(r"""(()=>{const a=%s;const snapshot=%s;
          const n=v=>String(v||'').replace(/\s+/g,' ').trim();
          if(location.origin+location.pathname!=='https://www.zhipin.com/web/geek/chat'||
             n(document.querySelector('.nav-figure')?.innerText)!==%s||
             /访问环境异常|安全验证|账号异常|请先登录|请完成验证|操作过于频繁/.test(document.body.innerText))return {clicked:false,reason:'IDENTITY_OR_SAFETY_CHANGED'};
          if(!['uid','job_id','company','recruiter','title','city'].every(k=>n(snapshot[k])===n(a[k]))||
             !a.known_message_ids.some(id=>snapshot.message_ids.includes(id)))return {clicked:false,reason:'IDENTITY_OR_SAFETY_CHANGED'};
          if(snapshot.message_ids.some(id=>!a.known_message_ids.includes(id)))return {clicked:false,reason:'HISTORY_CHANGED'};
          if(a.kind==='resume'&&!%s&&[...document.querySelectorAll('.panel-resume.sentence-popover')].some(e=>e.getClientRects().length))return {clicked:false,reason:'RESUME_DIALOG_ALREADY_OPEN'};
          let b;if(a.kind==='text'){
            if(n(snapshot.composer_text)!==n(a.text))return {clicked:false,reason:'COMPOSER_CHANGED'};
            if(snapshot.evidence.some(m=>m.actor==='self'&&n(m.text)===n(a.text)))return {status:'duplicate',clicked:false};
            b=document.querySelector('.chat-conversation button[type="send"]');
          }else if(%s){
            const panels=[...document.querySelectorAll('.panel-resume.sentence-popover')].filter(e=>e.getClientRects().length);
            if(panels.length!==1)return {clicked:false,reason:'RESUME_CONFIRMATION_UNVERIFIED'};
            const panel=panels[0],wrapper=panel.closest('.toolbar-btn-content'),v=wrapper?.__vue__;
            if(!wrapper?.querySelector('.toolbar-btn[d-c="62009"]')||String(v?.conversation$?.uid)!==a.uid||
               !v?.resumeId||v.resumeId!==v.info?.resumeInfo?.resumeId||
               n(panel.querySelector('.title')?.innerText)!=='确定向 Boss 发送简历吗？'||
               n(panel.querySelector('.content')?.innerText)!=='Boss确认后，该附件简历将直接发送至对方邮箱'||
               /在线简历|上传|替换/.test(panel.innerText))return {clicked:false,reason:'RESUME_CONFIRMATION_UNVERIFIED'};
            const bs=[...panel.querySelectorAll('.btn-sure-v2')].filter(e=>n(e.innerText)==='确定');if(bs.length===1)b=bs[0];
          }else if(a.request_mode==='text'){
            const bs=[...document.querySelectorAll('.chat-conversation .toolbar-btn[d-c="62009"]')].filter(e=>n(e.innerText)==='发简历');if(bs.length===1)b=bs[0];
          }else{
            const card=document.querySelector('.chat-conversation [data-mid="'+a.request_id+'"]');
            if(!card?.classList.contains('item-friend')||!card.innerText.includes('我想要一份您的附件简历，您是否同意'))return {clicked:false,reason:'RESUME_REQUEST_MISMATCH'};
            const bs=[...card.querySelectorAll('.card-btn')].filter(e=>n(e.innerText)==='同意');if(bs.length===1)b=bs[0];
          }
          if(!b||b.disabled||['disabled','unable'].some(c=>b.classList.contains(c))||b.getAttribute('aria-disabled')==='true'||!b.getClientRects().length)return {clicked:false,reason:'CONTROL_DISABLED'};
          b.click();return {clicked:true};})()""" % (json.dumps(a, ensure_ascii=False), _snapshot_script(), json.dumps(get_config().self_name, ensure_ascii=False),
                                                   str(confirmation).lower(), str(confirmation).lower()))

    def __call__(self, payload):
        clicked, in_progress = False, False
        try:
            a = validate_action(payload)
            with self.reader.lock:
                if a['kind'] == 'job_detail':
                    return self._job_detail(a)
                history = self.reader.full_history(a['uid'], ())
                row = self.reader.rows.get(a['uid'], {})
                if any(row.get(k) != a[v] for k, v in (('job_id', 'job_id'), ('company', 'company'), ('name', 'recruiter'))):
                    raise _blocked('IDENTITY_MISMATCH')
                snap = self._select(a)
                identity = {k: snap[k] for k in ('uid', 'job_id', 'company', 'recruiter', 'title', 'city')}
                if a['kind'] == 'inspect':
                    return {'status': 'inspected', 'clicked': False, 'identity': identity, 'evidence': snap['evidence'],
                        **({'attachment': self._attachment()} if a.get('inspect_resume') else {})}
                if a['kind'].startswith('verify'):
                    ids = set(map(str, a['message_ids']))
                    server = [m for m in history if m['message_id'] in ids]
                    evidence = [m for m in snap['evidence'] if m['message_id'] in ids]
                    resume = bool(a.get('request_id'))
                    request = next((m for m in history if m['message_id'] == str(a.get('request_id'))), None)
                    valid = len(server) == len(evidence) == len(ids) and all(
                        bool(request) and official_receipt(m) and after_request(m, request) if resume
                        else m['from_uid'] == get_config().self_uid and m['text'] == a.get('text') for m in server)
                    valid = valid and all(m['resume_receipt'] if resume else m['actor'] == 'self' and
                        _normal(m['text']) == _normal(a.get('text')) and
                        _normal(m['delivery']) in ('送达', '已读', '[送达]', '[已读]') for m in evidence)
                    return {'status': 'verified' if valid else 'uncertain', 'clicked': False,
                            'identity': identity, 'evidence': evidence, **({} if valid else {'reason': 'DELIVERY_NOT_VERIFIED'})}
                known = set(a['known_message_ids'])
                if a['kind'] == 'text':
                    duplicate = [m for m in history if m['from_uid'] == get_config().self_uid and m['text'] == a['text']]
                    if duplicate:
                        return {'status': 'duplicate', 'clicked': False, 'identity': identity, 'evidence': duplicate}
                    attempt = (a['uid'], 'text', a['text'])
                else:
                    request = next((m for m in history if m['message_id'] == str(a['request_id'])), None)
                    if not request or request['from_uid'] != a['uid'] or not request_mode(request):
                        raise _blocked('RESUME_REQUEST_MISMATCH')
                    a['request_mode'] = request_mode(request)
                    receipt = delivered_receipt(history, request)
                    if receipt:
                        return {'status': 'duplicate', 'clicked': False, 'identity': identity, 'evidence': [receipt]}
                    if resume_send_attempt(history, request, get_config().self_uid) or request.get('dialog', {}).get('operated'):
                        return {'status': 'uncertain', 'clicked': False, 'reason': 'RESUME_ALREADY_ATTEMPTED'}
                    if superseding_request(history, request):
                        return {'status': 'not_sent', 'clicked': False, 'reason': 'RESUME_REQUEST_SUPERSEDED'}
                    attempt = (a['uid'], 'resume', a['request_id'])
                if attempt in self.attempted:
                    return {'status': 'uncertain', 'clicked': False, 'reason': 'PREVIOUS_ATTEMPT_UNKNOWN'}
                if {m['message_id'] for m in history} != known:
                    return {'status': 'not_sent', 'clicked': False, 'reason': 'HISTORY_CHANGED'}
                self._authorized()
                if a['kind'] == 'resume' and self.reader._evaluate("[...document.querySelectorAll('.panel-resume.sentence-popover')].filter(e=>e.getClientRects().length).length"):
                    raise _blocked('RESUME_DIALOG_ALREADY_OPEN')
                attachment = self._attachment() if a['kind'] == 'resume' else None
                if a['kind'] == 'text':
                    if _normal(snap['composer_text']) and _normal(snap['composer_text']) != _normal(a['text']):
                        raise _blocked('COMPOSER_NOT_EMPTY')
                    if not _normal(snap['composer_text']):
                        self.reader._evaluate("document.querySelector('#chat-input')?.focus()")
                        self.reader._connect().call('Input.insertText', {'text': a['text']})
                # Refresh full server history immediately before dispatch; an
                # intervening manual action or inbound invalidates this action.
                latest = self.reader.full_history(a['uid'], ())
                if {m['message_id'] for m in latest} != known:
                    return {'status': 'not_sent', 'clicked': False, 'reason': 'HISTORY_CHANGED'}
                self._authorized()
                self.attempted.add(attempt)
                in_progress = True
                result = self._click(a)
                if not isinstance(result, dict) or type(result.get('clicked')) is not bool:
                    raise _blocked('CLICK_RESULT_UNKNOWN')
                in_progress = False
                if not result.get('clicked'):
                    self.attempted.discard(attempt)
                    return {'status': result.get('status', 'not_sent'), 'clicked': False,
                            'reason': result.get('reason', 'CONTROL_NOT_CLICKED'), 'identity': identity}
                clicked = True
                if a['kind'] == 'resume':
                    self._pause(.35)
                    # Toolbar can send directly OR show an official confirmation.
                    # Never click the toolbar twice. Confirm only its exact panel.
                    panel = self.reader._evaluate("[...document.querySelectorAll('.panel-resume.sentence-popover')].filter(e=>e.getClientRects().length).length")
                    if panel:
                        current = self.reader.full_history(a['uid'], ())
                        if {m['message_id'] for m in current} != known:
                            return {'status': 'uncertain', 'clicked': True, 'reason': 'HISTORY_CHANGED'}
                        self._authorized()
                        in_progress = True
                        result = self._click(a, confirmation=True)
                        if not isinstance(result, dict) or type(result.get('clicked')) is not bool:
                            raise _blocked('CLICK_RESULT_UNKNOWN')
                        in_progress = False
                        if not result.get('clicked'):
                            return {'status': 'uncertain', 'clicked': True, 'reason': result.get('reason', 'RESUME_CONFIRMATION_UNVERIFIED')}
                return {'status': 'clicked', 'clicked': True, 'identity': identity, 'evidence': [],
                        **({'attachment': attachment} if attachment else {})}
        except BossBlocked as exc:
            return {'status': 'uncertain' if clicked or in_progress else 'blocked',
                    'clicked': None if in_progress else clicked, 'reason': exc.code}
        except Exception:
            return {'status': 'uncertain' if clicked or in_progress else 'blocked',
                    'clicked': None if in_progress else clicked, 'reason': 'BROWSER_OPERATION_FAILED'}

    bridge = __call__
