"""Read-only projection of job evidence. Raw API/account objects never leave here."""
import re
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote
from resume_policy import official_receipt, request_mode

TZ = ZoneInfo('Asia/Shanghai')


def rows(value):
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def words(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(words(x) for x in value if isinstance(x, (str, list)))
    return ''


def date_value(value):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value / (1000 if value > 10**11 else 1), TZ)
        if isinstance(value, str) and value:
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)
    except (ValueError, OverflowError, OSError):
        pass
    return None


def iso(value):
    d = date_value(value)
    return d.isoformat() if d else None


def salary(value):
    text = words(value)
    if any(x in text for x in ['+', '＋', '提成', '综合', '底薪', '奖金']):
        return None, 'mixed'
    if re.search(r'年薪|[/／每]年|万元?每年', text):
        return None, 'year'
    if any(x in text for x in ['天', '/日', '日薪']):
        return None, 'day'
    if any(x in text for x in ['小时', '/时', '时薪']):
        return None, 'hour'
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(?:[-–~至]\s*\d+(?:\.\d+)?)?\s*([kK万千])(?:元)?(?:[/／]月)?(?:\s*[·•]\s*\d+薪)?\s*', text)
    if match:
        return int(float(match[1]) * (10000 if match[2] == '万' else 1000)), 'month'
    match = re.search(r'(\d+)\s*(?:[-–~]\s*\d+)?\s*元/月', text)
    return (int(match[1]), 'month') if match else (None, 'unknown')


def reading(app):
    candidates = []
    mapping = {'已读': 'read', '未读': 'unread', 'read': 'read', 'unread': 'unread'}
    if date_value(app.get('server_verified_at')) and app.get('server_read_status') in mapping:
        candidates.append((date_value(app['server_verified_at']), mapping[app['server_read_status']]))
    for obs in rows(app.get('conversation_observations')):
        if obs.get('conversation_uid') and str(obs['conversation_uid']) != str(app.get('conversation_uid')):
            continue
        if obs.get('message_id') and app.get('ui_message_id') and str(obs['message_id']) != str(app['ui_message_id']):
            continue
        dt = date_value(obs.get('observed_at'))
        if dt and obs.get('read_status') in mapping:
            candidates.append((dt, mapping[obs['read_status']]))
    if not candidates:
        return 'unknown', None
    latest = max(t for t, _ in candidates)
    values = {s for t, s in candidates if t == latest}
    return (next(iter(values)) if len(values) == 1 else 'unknown'), latest.isoformat()


def evidence_rows(sources):
    """Yield lower-detail evidence before full descriptions and screening decisions."""
    for name, source in sources.items():
        if name == 'applications':
            continue
        for record in rows(source.get('jobs')):
            yield name, record, 'research'
        for search in rows(source.get('searches')):
            for record in rows(search.get('data')):
                yield name, record, 'research'
        for key in ('job_details', 'reused_job_details'):
            for record in rows(source.get(key)):
                yield name, record, 'research'
        for key in ('ranked_recommendations', 'shortlist'):
            for record in rows(source.get(key)):
                yield name, record, 'candidate'
        for batch in rows(source.get('followup_research')):
            for record in rows(batch.get('search_results')):
                yield name, record, 'research'
            for record in rows(batch.get('full_job_descriptions')):
                yield name, record, 'candidate'


def blank_job(job_id):
    return dict(job_id=job_id, company=None, title=None, city=None, district=None, salary_text=None,
                salary_monthly_min=None, salary_unit='unknown', direction=None, scope='research',
                contact_status='not_contacted', read_status='unknown', observed_at=None,
                official_url='https://www.zhipin.com/job_detail/' + quote(job_id, safe='') + '.html',
                description=None, address=None, fit_notes=[], gaps=[], sources=[], messages=[], drafts=[],
                reviews=[], field_evidence={}, opening_version=None, sent_kind=None, sent_version=None,
                sent_text=None, sent_at=None, contacted_at=None, origin=None,
                identity_verified=False, monitored=False)


def add_unique(items, value):
    if value and value not in items:
        items.append(value)


def merge_evidence(job, record, source, scope):
    description = words(record.get('description')) or '\n\n'.join(words(x) for x in (record.get('descriptions') or []) if isinstance(x, str))
    verified = iso(record.get('verified_at') or record.get('server_verified_at'))
    observed = verified or iso(record.get('observed_at') or record.get('source_timestamp') or record.get('retrieved_at'))
    rank = 3 if verified else 2 if description else 1
    fields = {k: words(record.get(k)) for k in ('company', 'title', 'city', 'district', 'address', 'direction')}
    fields.update(salary_text=words(record.get('salary')), description=description)
    for key, value in fields.items():
        if not value:
            continue
        previous = job['field_evidence'].get(key)
        evidence = dict(source=source, rank=rank, observed_at=observed)
        if previous and job[key] != value and key != 'description':
            label = {'city':'城市', 'salary_text':'薪资', 'title':'职位', 'company':'公司', 'district':'区县', 'address':'地址', 'direction':'方向'}[key]
            add_unique(job['gaps'], f'{label}来源存在差异：{previous["source"]}「{job[key]}」与 {source}「{value}」，请核对最新职位正文。')
        old_order = (previous['rank'], date_value(previous['observed_at']) or datetime.min.replace(tzinfo=TZ)) if previous else (0, datetime.min.replace(tzinfo=TZ))
        new_order = (rank, date_value(observed) or datetime.min.replace(tzinfo=TZ))
        if not previous or new_order > old_order:
            job[key] = value
            job['field_evidence'][key] = evidence
    job['salary_monthly_min'], job['salary_unit'] = salary(job['salary_text'])
    for key in ('fit', 'match', 'category', 'decision', 'label', 'tier'):
        add_unique(job['fit_notes'], words(record.get(key)))
    add_unique(job['gaps'], words(record.get('gap')))
    add_unique(job['sources'], source)
    if scope == 'candidate' and job['scope'] != 'contacted':
        job['scope'] = scope


def normalized_messages(app):
    seen, result = set(), []
    for message in rows(app.get('conversation_messages')):
        text = words(message.get('text')) or words(message.get('rendered_text'))
        role = message.get('speaker') or message.get('direction') or '来源待核验'
        kind = message.get('kind') or 'unknown'
        mid = str(message['message_id']) if message.get('message_id') is not None else None
        when = iso(message.get('timestamp_iso') or message.get('timestamp'))
        uid = message.get('conversation_uid') or app.get('conversation_uid')
        identity = (str(uid) if uid is not None else None, mid or (role, kind, when, text))
        if identity in seen:
            continue
        seen.add(identity)
        actor = message.get('actor') if message.get('actor') in ('user', 'agent', 'platform') else None
        result.append(dict(message_id=mid, role=role, kind=kind, actor=actor, text=text,
                           message_at=when, observed_at=iso(message.get('observed_at')),
                           conversation_uid=str(uid) if uid is not None else None,
                           response_required=message.get('response_required', True),
                           identity_incomplete=uid is None or mid is None))
    return sorted(result, key=lambda m: date_value(m['message_at'] or m['observed_at']) or datetime.max.replace(tzinfo=TZ))


def merge_applications(records):
    ordered = sorted(records, key=lambda a: date_value(a.get('server_verified_at')) or datetime.min.replace(tzinfo=TZ))
    merged = {}
    for record in ordered:
        merged.update(deepcopy(record))
    for field in ('conversation_messages', 'reply_reviews', 'followup_actions'):
        merged[field] = []
        for record in records:
            for value in rows(record.get(field)):
                value = deepcopy(value)
                if field in ('conversation_messages', 'followup_actions'):
                    value.setdefault('conversation_uid', record.get('conversation_uid'))
                add_unique(merged[field], value)
    merged['_records'] = records
    return merged


def merged_reading(app):
    readings = [reading(a) for a in app['_records']]
    candidates = [(date_value(when), status) for status, when in readings if when]
    if not candidates:
        return 'unknown', None
    latest = max(dt for dt, _ in candidates)
    values = {status for dt, status in candidates if dt == latest}
    return (next(iter(values)) if len(values) == 1 else 'unknown'), latest.isoformat()


def task(kind, job_id, title, detail='', **kwargs):
    return dict(id=f'{kind}:{job_id}:{kwargs.get("event_id", "")}', kind=kind, job_id=job_id,
                title=title, detail=detail, observed_at=kwargs.get('observed_at'),
                incoming_message_ids=kwargs.get('incoming_message_ids', []),
                conversation_uid=kwargs.get('conversation_uid'),
                draft_version=kwargs.get('draft_version'), draft_text=kwargs.get('draft_text'),
                uncertain_fields=kwargs.get('uncertain_fields', []))


def receipt_handled_requests(app):
    """Resolve only standalone resume requests followed by an official receipt."""
    messages = rows(app.get('conversation_messages'))
    handled = set()
    for request in messages:
        if request.get('kind') != 'incoming_text' or request.get('message_id') is None:
            continue
        text = re.sub(r'[\s，,。.!！?？~～、]', '', words(request.get('text')))
        standalone = request_mode(request) or re.fullmatch(
            r'(?:您好|你好)?(?:附件|个人)?简历(?:方便|可以|能否)?(?:发|发送)(?:一下|下|一份)?(?:给我|过来)?(?:吗|吧)?', text)
        uid = request.get('conversation_uid') or app.get('conversation_uid')
        when = date_value(request.get('timestamp_iso') or request.get('timestamp'))
        if not standalone or uid is None or not when:
            continue
        for receipt in messages:
            if str(receipt.get('conversation_uid') or app.get('conversation_uid')) != str(uid):
                continue
            platform_receipt = (receipt.get('kind') == 'platform_event' and receipt.get('actor') == 'platform'
                                and words(receipt.get('text')).strip() in (
                                    '对方已同意，您的附件简历已发送给对方', '对方已查看了您的附件简历'))
            received_at = date_value(receipt.get('timestamp_iso') or receipt.get('timestamp'))
            if not (official_receipt(receipt) or platform_receipt) or not received_at or received_at < when:
                continue
            request_id, receipt_id = str(request['message_id']), str(receipt.get('message_id', ''))
            if request_id.isdigit() and receipt_id.isdigit() and int(receipt_id) > int(request_id):
                handled.add((str(uid), request_id))
                break
    return handled


def project_workbench_tasks(tasks, jobs, apps, monitor):
    """Current decisions replace only the same conversation's exact inbound IDs."""
    workbench = monitor.get('workbench_tasks')
    if not isinstance(workbench, dict):
        return tasks
    done = {'ignored', 'verified_sent', 'observed_sent', 'superseded'}
    pending = {'ready', 'authorized_pending_send', 'awaiting_user', 'uncertain', 'dispatching', 'pending'}
    covered, current = set(), []
    for item in rows(workbench.get('items')):
        row = item.get('row') if isinstance(item.get('row'), dict) else {}
        jid, uid = str(row.get('job_id') or ''), str(item.get('uid') or '')
        status = item.get('status')
        app = apps.get(jid)
        uids = {str(a['conversation_uid']) for a in app['_records'] if a.get('conversation_uid') is not None} if app else set()
        if not app or uid not in uids or (row.get('uid') is not None and str(row['uid']) != uid):
            if status in ('uncertain', 'dispatching', 'pending'):
                matches = [key for key, value in apps.items() if any(str(a.get('conversation_uid')) == uid for a in value['_records'])]
                current.append(task('uncertain_send', matches[0] if len(matches) == 1 else None,
                                    '会话身份或处理结果待核验', words(item.get('reason')) or '无法确定此事项对应的岗位，未消除任何原待办。',
                                    event_id=item.get('action_id'), conversation_uid=uid))
            continue
        if status not in done | pending:
            continue
        ids = sorted({str(mid) for mid in item.get('incoming_ids', []) if mid is not None}) if isinstance(item.get('incoming_ids'), list) else []
        covered.update((jid, uid, mid) for mid in ids)
        if status in done:
            continue
        kind = 'user_input' if status == 'awaiting_user' else 'incoming' if status in ('ready', 'authorized_pending_send') else 'uncertain_send'
        title = {'user_input': '需要你补充信息或确认', 'incoming': '已分析，可一键跟进', 'uncertain_send': '处理结果待核验'}[kind]
        reason, question = words(item.get('reason')), words(item.get('question_for_user'))
        detail = '\n'.join(value for value in (reason, question) if value)
        current.append(task(kind, jid, title, detail, event_id=item.get('action_id'),
                            conversation_uid=uid, incoming_message_ids=ids, draft_text=words(item.get('text')) or None,
                            uncertain_fields=[question] if question else [], observed_at=iso(workbench.get('finished_at') or workbench.get('started_at'))))
    projected = []
    for original in tasks:
        entry = deepcopy(original)
        jid, uid, ids = entry['job_id'], entry.get('conversation_uid'), entry['incoming_message_ids']
        if uid is None and jid in apps:
            uids = {str(a['conversation_uid']) for a in apps[jid]['_records'] if a.get('conversation_uid') is not None}
            uid = next(iter(uids)) if len(uids) == 1 else None
        if ids and uid is not None and entry['kind'] in ('incoming', 'user_input', 'reply', 'uncertain_send'):
            remaining = [mid for mid in ids if (jid, str(uid), str(mid)) not in covered]
            if not remaining:
                continue
            if len(remaining) != len(ids):
                entry['incoming_message_ids'] = remaining
                if entry['kind'] == 'incoming':
                    entry['detail'] = '\n\n'.join(m['text'] for m in jobs[jid]['messages']
                                                  if m['conversation_uid'] == str(uid) and m['message_id'] in remaining)
        projected.append(entry)
    for entry in current:
        add_unique(projected, entry)
    return projected


def draft_record(record, source, kind='opening'):
    return dict(kind=kind, version=record.get('version') or record.get('opening_version') or record.get('draft_version'),
                text=words(record.get('text') or record.get('message') or record.get('draft')),
                status=record.get('status') or 'unknown', incoming_message_ids=record.get('incoming_message_ids') or [], source=source)


def daily_contacts(main, apps, now):
    today = now.astimezone(TZ).date().isoformat()
    audit = (main.get('daily_contact_audits') or {}).get(today) or {}
    confirmed, uncertain = {}, {}
    for a in apps:
        if a.get('counts_as_new_outbound_contact') is False:
            continue
        dt = date_value(a.get('attempted_at'))
        if not dt or dt.date().isoformat() != today:
            continue
        identity = str(a.get('conversation_uid') or a['job_id'])
        verified_contact = a.get('status') == 'verified_sent' or (a.get('status') == 'user_contact_verified' and a.get('identity_verified') is True)
        target = confirmed if verified_contact or a.get('default_greeting_observed') else uncertain
        target[identity] = a
    for entry in rows(audit.get('preexisting_evidence')):
        if entry.get('init_cards') and entry.get('uid') is not None:
            confirmed[str(entry['uid'])] = entry
    for identity in confirmed:
        uncertain.pop(identity, None)
    total = audit.get('total_confirmed_account_contacts')
    total = total if isinstance(total, int) and not isinstance(total, bool) else 0
    cutoff = date_value(audit.get('observed_at'))
    newer = sum(1 for a in confirmed.values() if cutoff and date_value(a.get('attempted_at')) and date_value(a['attempted_at']) > cutoff)
    known = max(len(confirmed), total + newer)
    return dict(date=today, confirmed=known, uncertain=len(uncertain), minimum_count=known + len(uncertain),
                cap=(main.get('ongoing_search_authorization') or {}).get('daily_new_contact_cap'),
                complete=False, note='至少已确认；含此前账号联系，不等于代理定制发送。' if total else '按已记录的发起联系计算；账号总量尚未完整核对。')


def build_view(sources: dict, now: datetime) -> dict:
    now = now.astimezone(TZ)
    main = sources.get('applications') or {}
    monitor = main.get('reply_monitor') or {}
    jobs, tasks = {}, []
    decision_events, handled_by_job = [], {}
    for source, record, scope in evidence_rows(sources):
        if not record.get('job_id'):
            continue
        jid = str(record['job_id'])
        merge_evidence(jobs.setdefault(jid, blank_job(jid)), record, source, scope)
    # Canonical actual-contact data has precedence over research snapshots.
    app_groups = {}
    for app in rows(main.get('applications')):
        if not app.get('job_id'):
            continue
        jid = str(app['job_id'])
        app_groups.setdefault(jid, []).append(app)
    app_by_id = {jid: merge_applications(records) for jid, records in app_groups.items()}
    for jid, app in app_by_id.items():
        job = jobs.setdefault(jid, blank_job(jid))
        for record in app['_records']:
            merge_evidence(job, record, 'applications', 'contacted')
        verified_sent = app.get('status') == 'verified_sent'
        user_contact = app.get('status') == 'user_contact_verified'
        job.update(scope='contacted', contact_status=app.get('status') or 'unknown',
                   origin=app.get('origin'), identity_verified=app.get('identity_verified') is True,
                   opening_version=app.get('opening_version'),
                   sent_kind=None if user_contact else 'reply' if app.get('origin') == 'recruiter_initiated' else 'opening',
                   sent_version=None if user_contact else (app.get('reply_version') or app.get('opening_version')) if app.get('origin') == 'recruiter_initiated' else app.get('opening_version'),
                   sent_at=None if user_contact else iso(app.get('sent_at') or app.get('server_message_timestamp') or app.get('custom_attempted_at')),
                   contacted_at=iso(app.get('first_contact_at') or app.get('attempted_at') or app.get('sent_at') or app.get('server_message_timestamp')),
                   monitored=app.get('monitoring_enabled') is True and (verified_sent or (user_contact and app.get('identity_verified') is True)))
        job['read_status'], job['observed_at'] = merged_reading(app)
        uids = {str(a['conversation_uid']) for a in app['_records'] if a.get('conversation_uid') is not None}
        identity_conflict = len(uids) > 1
        if identity_conflict:
            job['read_status'] = 'unknown'
            add_unique(job['gaps'], '同一岗位存在不同会话身份，消息已保留；阅读和回复归属待核验。')
            tasks.append(task('unknown_event', jid, '会话身份待核验', job['gaps'][-1]))
        job['messages'] = normalized_messages(app)
        # Completed decisions and sends cover only their exact conversation and IDs.
        decisions = {}
        ordered_decisions = sorted(rows(app.get('followup_actions')), key=lambda a:
            date_value(a.get('updated_at') or a.get('created_at')) or datetime.min.replace(tzinfo=TZ))
        for action in ordered_decisions:
            if action.get('type') not in ('reply_decision', 'reply', 'resume', 'ignore', 'escalate') or action.get('status') not in ('ignored', 'awaiting_user', 'verified_sent', 'observed_sent', 'superseded'):
                continue
            uid = action.get('conversation_uid') or app.get('conversation_uid')
            ids = action.get('incoming_message_ids', action.get('trigger_message_ids'))
            if uid is None or not isinstance(ids, list):
                continue
            for mid in ids:
                decisions[(str(uid), str(mid))] = action
        handled_keys = {key for key, action in decisions.items() if
                        action.get('status') in ('ignored', 'verified_sent', 'observed_sent', 'superseded')}
        handled_keys.update(receipt_handled_requests(app))
        handled_by_job[jid] = handled_keys
        for action in ordered_decisions:
            ids = sorted({mid for (uid, mid), current in decisions.items() if current is action})
            if action.get('status') != 'awaiting_user' or not ids:
                continue
            event = dict(event_id=action.get('action_id'), job_id=jid,
                         conversation_uid=action.get('conversation_uid') or app.get('conversation_uid'),
                         incoming_message_ids=ids, requires_user_review=True,
                         analysis=words(action.get('reason')),
                         uncertain_fields=[words(action['question_for_user'])] if action.get('question_for_user') else [],
                         observed_at=action.get('updated_at') or action.get('created_at'))
            if words(action.get('text')):
                event.update(draft_text=words(action['text']), draft_version=action.get('version') or 'resident-v1')
            decision_events.append(event)
        if app.get('status') == 'verified_sent':
            job['sent_text'] = words(app.get('verified_text') or app.get('message'))
        for original in app['_records']:
            if isinstance(original.get('case_review'), dict):
                add_unique(job['reviews'], {k: words(original['case_review'].get(k)) for k in ('facts', 'interpretation', 'next_trial', 'confounder')})
            if original.get('status') == 'verified_sent' and words(original.get('verified_text') or original.get('message')):
                is_reply = original.get('origin') == 'recruiter_initiated'
                add_unique(job['drafts'], dict(kind='reply' if is_reply else 'opening',
                    version=(original.get('reply_version') or original.get('opening_version')) if is_reply else original.get('opening_version'),
                    text=words(original.get('verified_text') or original.get('message')),
                    status='verified_sent', incoming_message_ids=[], source='applications'))
        for review in rows(app.get('reply_reviews')):
            job['reviews'].append({k: words(review.get(k)) for k in ('analysis', 'evidence', 'recommendation', 'status', 'reviewed_at')})
            if words(review.get('draft')):
                job['drafts'].append(draft_record(review, 'reply_reviews', 'reply'))
        if app.get('status') in ('pending', 'authorized_pending_send', 'dispatching', 'uncertain'):
            tasks.append(task('uncertain_send', jid, '联系结果待核验', '先核对历史，避免重复发送。'))
        incoming = [m for m in job['messages'] if m['kind'] == 'incoming_text' and m['text'].strip()
                    and m['response_required'] is not False and (m['conversation_uid'], m['message_id']) not in handled_keys]
        if incoming:
            latest_out = None if identity_conflict else max((date_value(m['message_at']) for m in job['messages'] if m['kind'] == 'outgoing_text' and m['message_at']), default=None)
            unanswered = [m for m in incoming if not latest_out or not date_value(m['message_at']) or date_value(m['message_at']) > latest_out]
            for uid in dict.fromkeys(m['conversation_uid'] for m in unanswered):
                messages = [m for m in unanswered if m['conversation_uid'] == uid]
                tasks.append(task('incoming', jid, '招聘方来信待处理', '\n\n'.join(m['text'] for m in messages),
                                  conversation_uid=uid, event_id=uid or '',
                                  incoming_message_ids=[m['message_id'] for m in messages if m['message_id']]))
    for source, data in sources.items():
        for record in rows(data.get('candidate_drafts')) + rows(data.get('shortlist')):
            jid = str(record.get('job_id', ''))
            if jid in jobs and words(record.get('text') or record.get('message')):
                draft = draft_record(record, source)
                if not any(d['version'] == draft['version'] and d['text'] == draft['text'] for d in jobs[jid]['drafts']):
                    jobs[jid]['drafts'].append(draft)
    events = monitor.get('notification_events', [])
    if not isinstance(events, list):
        if events is not None:
            tasks.append(task('unknown_event', None, '通知事件集合待核对', 'reply_monitor.notification_events 不是列表，不能认定为零待办。', event_id='collection'))
        events = []
    else:
        events = list(events)
    # The email and its underlying resident decision describe the same work.
    # A decision remains visible even before an optional notification exists.
    for event in decision_events:
        if not any(isinstance(existing, dict) and (
                (event.get('event_id') and existing.get('event_id') == event['event_id']) or
                (existing.get('job_id') == event['job_id'] and
                 str(existing.get('conversation_uid', event['conversation_uid'])) == str(event['conversation_uid']) and
                 isinstance(existing.get('incoming_message_ids'), list) and
                 set(map(str, existing.get('incoming_message_ids') or [])) == set(event['incoming_message_ids'])))
                for existing in events):
            events.append(event)
    groups = {}
    execution_titles = {'authorized_pending_send': '回复已授权，待发送',
                        'dispatching': '回复发送中，待核验', 'uncertain': '回复发送结果待核验'}
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            tasks.append(task('unknown_event', None, '通知事件待核对', f'reply_monitor.notification_events[{index}] 结构未识别。', event_id=index))
            continue
        jid = str(event.get('job_id') or '')
        text = words(event.get('draft_text') or event.get('draft'))
        ids = event.get('incoming_message_ids') or event.get('new_message_ids') or []
        ids = [str(x) for x in ids] if isinstance(ids, list) else []
        version = event.get('draft_version')
        if event.get('send_status') in ('pending', 'dispatching', 'uncertain'):
            tasks.append(task('uncertain_notification', jid or None, '邮件通知结果待核验', '先查已发送邮件，避免重复提醒。', event_id=index))
        # Progress and connection emails need no reply draft or conversation identity.
        if (event.get('requires_user_review') is False and not text and not version
                and event.get('reply_status') not in (*execution_titles, 'verified_sent')):
            continue
        if jid in jobs and ids:
            uid = event.get('conversation_uid')
            if uid is None:
                uids = {str(a['conversation_uid']) for a in app_by_id.get(jid, {}).get('_records', []) if a.get('conversation_uid') is not None}
                uid = next(iter(uids)) if len(uids) == 1 else None
            groups.setdefault((jid, str(uid) if uid is not None else None, tuple(sorted(set(ids)))), []).append((index, event, text, version))
        else:
            tasks.append(task('unknown_event', jid or None, '通知事件待核对', f'notification_events[{index}] 缺少岗位或来信身份。', event_id=index))
    for (jid, uid, ids), group in groups.items():
        job = jobs[jid]
        outgoing_ids = {m['message_id'] for m in job['messages'] if m['kind'] == 'outgoing_text' and m['text'] and m['message_at'] and m['conversation_uid'] == uid}
        resolved = {(uid, mid) for mid in ids}.issubset(handled_by_job.get(jid, set())) or any(
            e.get('reply_status') == 'verified_sent' and str(e.get('reply_message_id')) in outgoing_ids for _, e, _, _ in group)
        eligible, needs_input = [], False
        for index, event, text, version in group:
            superseded = str(event.get('status', '')).startswith('superseded') or str(event.get('reply_status', '')).startswith('superseded') or bool(event.get('superseded_by'))
            execution = event.get('reply_status') in execution_titles
            requires_review = event.get('requires_user_review') is not False and not execution
            if text and version:
                status = ('resolved' if resolved else 'superseded' if superseded else
                          'authorized_pending_send' if execution else 'pending_review' if requires_review else 'notification_only')
                draft = dict(kind='reply', version=version, text=text, status=status,
                             requires_user_review=requires_review and not resolved and not superseded,
                             execution_status=event.get('reply_status') if execution else None,
                             incoming_message_ids=list(ids), source='notification_events')
                add_unique(job['drafts'], draft)
                if not resolved and not superseded and (requires_review or execution):
                    eligible.append((index, event, draft))
            elif not resolved and not superseded and event.get('requires_user_review') is True and not execution:
                if not needs_input:
                    tasks.append(task('user_input', jid, '需要你补充信息或确认', words(event.get('analysis')) or words(event.get('subject')),
                                      event_id=index, incoming_message_ids=list(ids), conversation_uid=uid,
                                      draft_text=text or None,
                                      uncertain_fields=event.get('uncertain_fields') or [], observed_at=iso(event.get('observed_at'))))
                needs_input = True
            elif not resolved and not superseded:
                tasks.append(task('unknown_event', jid, '通知事件待核对', f'notification_events[{index}] 缺少草稿版本或实际回复证据。', event_id=index))
        # Event order alone cannot establish which differing draft was approved.
        distinct = {(str(d['version']), d['text']) for _, _, d in eligible}
        if len(distinct) > 1:
            timed = [(date_value(e.get('updated_at') or e.get('observed_at')), i, e, d) for i, e, d in eligible]
            known = [x for x in timed if x[0]]
            newest = max((x[0] for x in known), default=None)
            winners = [x for x in known if x[0] == newest]
            if len({(str(x[3]['version']), x[3]['text']) for x in winners}) == 1 and len(known) == len(timed):
                winner = winners[-1][1:]
                for _, _, d in eligible:
                    if (d['version'], d['text']) != (winner[2]['version'], winner[2]['text']):
                        d['status'] = 'superseded'
                eligible = [winner]
            else:
                for _, _, d in eligible:
                    d['status'] = 'version_conflict'
                tasks.append(task('unknown_event', jid, '回复版本待核对', '同一来信存在多个有效草稿且无法确定最新版本，暂不列为可审阅回复。', event_id='versions:'+','.join(ids)))
                eligible = []
        if resolved or eligible or needs_input:
            tasks = [t for t in tasks if not (t['job_id'] == jid and t.get('conversation_uid') == uid and t['kind'] == 'incoming' and t['incoming_message_ids'] and set(t['incoming_message_ids']).issubset(ids))]
        if eligible:
            index, event, draft = eligible[-1]
            executing = draft['execution_status'] in execution_titles
            tasks.append(task('uncertain_send' if executing else 'reply', jid,
                              execution_titles[draft['execution_status']] if executing else '回复草稿待审阅',
                              '先核对历史，避免重复发送。' if executing else words(event.get('analysis')) or words(event.get('subject')), event_id=index,
                              conversation_uid=uid,
                              draft_text=draft['text'], draft_version=draft['version'], incoming_message_ids=list(ids),
                              observed_at=iso(event.get('observed_at'))))
    if monitor.get('last_connection_issue') and not (isinstance(monitor['last_connection_issue'],dict) and monitor['last_connection_issue'].get('status')=='resolved'):
        issue = monitor['last_connection_issue']
        tasks.append(task('connection', None, '最近记录有连接问题', words(issue) or words(issue.get('message') if isinstance(issue, dict) else None) or '请回到原对话查看详情。'))
    # A draft in a reply review without complete event identity stays visible for verification.
    for job in jobs.values():
        for draft in job['drafts']:
            if draft['kind'] == 'reply' and draft['source'] == 'reply_reviews' and draft['status'] not in ('verified_sent', 'superseded'):
                tasks.append(task('unknown_event', job['job_id'], '回复稿待核对', '缺少关联事件时先确认来信及版本。', draft_text=draft['text'], draft_version=draft['version']))
    tasks = project_workbench_tasks(tasks, jobs, app_by_id, monitor)
    sent = [j for j in jobs.values() if j['contact_status'] == 'verified_sent']
    monitored = [j for j in jobs.values() if j['monitored']]
    checked = date_value(monitor.get('last_checked_at'))
    interval = monitor.get('interval_minutes')
    stale = checked is None or not isinstance(interval, (int, float)) or (now - checked).total_seconds() > interval * 120
    failures = sum(1 for data in sources.values() for q in rows(data.get('searches')) if q.get('ok') is False)
    summary = dict(verified_sent=len(sent), monitored=len(monitored),
                   read=sum(j['read_status'] == 'read' for j in monitored), unread=sum(j['read_status'] == 'unread' for j in monitored),
                   read_unknown=sum(j['read_status'] == 'unknown' for j in monitored),
                   pending_replies=len({(t['job_id'], t.get('conversation_uid'), tuple(sorted(t['incoming_message_ids'])) or t['id'])
                                        for t in tasks if t['kind'] in ('reply', 'user_input')}),
                   daily_contacts=daily_contacts(main, [a for group in app_groups.values() for a in group], now))
    return dict(schema_version=1, meta=dict(available=True, loaded_at=now.isoformat(), boss_checked_at=iso(monitor.get('last_checked_at')),
                stale=stale, read_errors=[], search_failures=failures, recorded_monitor_status=monitor.get('status')),
                summary=summary, constraints={k: (main.get('ongoing_search_authorization') or {}).get(k) for k in (
                'city_priority', 'excluded_districts', 'minimum_monthly_cny', 'maximum_monthly_cny', 'daily_new_contact_cap', 'new_contact_hours_local')},
                jobs=sorted(jobs.values(), key=lambda j: (j['scope'] != 'contacted', j['company'] or '', j['title'] or '')),
                tasks=tasks, documents=[])
