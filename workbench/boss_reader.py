"""Read-only, fail-closed adapter for the already configured BOSS browser.

No command here sends a message, exports a session, or changes browser/network
settings. CLI output is allow-listed before it can enter application state.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
from pathlib import Path
import subprocess
from typing import Callable

from config import get_config

CLI = 'boss'  # Used only by injected legacy-parser tests; live I/O uses boss_cdp.
MASTER_NAME = 'applications.json'


class BossBlocked(RuntimeError):
    """The read failed; callers must latch a block, never treat it as no replies."""
    def __init__(self, reason: str, code: str = 'BOSS_UNAVAILABLE'):
        super().__init__(reason)
        self.code = code


def _timestamp(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def normalize_row(row: dict) -> dict:
    return {
        'uid': str(row.get('uid') or row.get('conversation_uid') or ''),
        'name': str(row.get('name') or ''),
        'company': str(row.get('company') or row.get('brand_name') or ''),
        'title': str(row.get('title') or ''),
        'job_id': str(row.get('job_id') or row.get('encrypt_job_id') or ''),
        'last_msg': str(row.get('last_msg') or ''),
        'last_ts': _timestamp(row.get('last_ts')),
        'msg_status': str(row.get('msg_status') or ''),
        'initiated_by': str(row.get('initiated_by') or ''),
        'unread': max(0, _timestamp(row.get('unread'))),
    }


def message_fingerprint(row: dict) -> str:
    """Read receipts/unread counters are observations, not new message events."""
    value = normalize_row(row)
    fields = [value['uid'], value['job_id'], html.unescape(value['last_msg']), value['last_ts']]
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def normalize_message(message: dict) -> dict:
    """Keep useful message evidence without arbitrary raw links or tokens."""
    raw = message.get('raw') if isinstance(message.get('raw'), dict) else {}
    body = message.get('body') if isinstance(message.get('body'), dict) else raw.get('body', {})
    body = body if isinstance(body, dict) else {}
    sender = raw.get('from') if isinstance(raw.get('from'), dict) else {}
    result = {
        'message_id': str(raw.get('mid') or message.get('message_id') or raw.get('id') or ''),
        'timestamp': _timestamp(message.get('timestamp') or raw.get('time')),
        'from_uid': str(sender.get('uid') or message.get('from_uid') or ''),
        'speaker': str(message.get('from') or ''),
        'text': str(message.get('text') or body.get('text') or ''),
        'body_type': body.get('type'),
        'status': raw.get('status'),
    }
    for name, fields in (
        ('dialog', ('title', 'text', 'operated', 'type')),
        ('hyperLink', ('text', 'title')),
        ('jobDesc', ('title', 'jobName', 'company', 'brandName', 'city', 'salary', 'experience', 'education', 'bottomText', 'jobId')),
        ('action', ('aid', 'text', 'title')),
    ):
        value = body.get(name)
        if isinstance(value, dict):
            result[name] = {k: value[k] for k in fields if k in value and isinstance(value[k], (str, int, float, bool, type(None)))}
    return result


def baseline_from_master(root: str | Path) -> dict[str, dict]:
    """Import existing list baselines, including previously inspected invitations.

    The explicit reply-monitor baseline is newer/more authoritative than the
    contact index. No absent history is synthesized as a new inbound message.
    Missing/corrupt source raises; it must not silently reset event deduplication.
    """
    source = json.loads((Path(root) / MASTER_NAME).read_text(encoding='utf-8'))
    monitor = source.get('reply_monitor', {})
    result = {}
    index = monitor.get('account_contact_index', {})
    if isinstance(index, dict):
        for uid, entry in index.items():
            if isinstance(entry, dict) and str(uid).isdigit() and 'last_msg' in entry:
                result[str(uid)] = normalize_row({**entry, 'uid': uid})
    entries = monitor.get('baseline', [])
    if not isinstance(entries, list):
        raise ValueError('主记录中的会话基线格式不正确')
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('主记录中的会话基线条目格式不正确')
        uid = str(entry.get('conversation_uid') or '')
        if not uid:
            continue
        # Do not invent an empty-text baseline if this older entry lacks a list
        # observation. A fresh history read is then appropriate.
        if 'last_list_message' not in entry:
            continue
        result[uid] = normalize_row({
            **result.get(uid, {}), 'uid': uid, 'job_id': entry.get('job_id', ''),
            'last_msg': entry['last_list_message'],
            'last_ts': entry.get('last_list_message_timestamp', entry.get('last_message_timestamp', 0)),
            'msg_status': entry.get('last_list_read_status', ''),
        })
    return result


class BossReader:
    def __init__(self, *, runner: Callable = subprocess.run, timeout: float = 45, max_pages: int = 30):
        self.runner = runner
        self.timeout = timeout
        self.max_pages = max_pages

    def _call(self, arguments: list[str]) -> dict:
        if self.runner is subprocess.run:
            raise BossBlocked('旧 CLI 读取入口已停用，请使用工作台官方页面连接。', 'LEGACY_DISABLED')
        env = os.environ.copy()
        for key in ('NO_PROXY', 'no_proxy'):
            env[key] = 'localhost,127.0.0.1,::1'
        command = [CLI, '--json', '--cdp-url', get_config().cdp_url, '--browser-source', 'existing-browser', *arguments]
        try:
            proc = self.runner(command, capture_output=True, text=True, encoding='utf-8', env=env, timeout=self.timeout, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            raise BossBlocked('BOSS 会话读取连接失败或超时；本轮已停止。', 'CONNECTION_FAILED') from None
        try:
            payload = json.loads(proc.stdout)
        except (json.JSONDecodeError, TypeError):
            raise BossBlocked('BOSS 会话读取结果无法确认；本轮已停止。', 'INVALID_RESPONSE') from None
        if not isinstance(payload, dict) or payload.get('ok') is not True or proc.returncode != 0:
            # Never return arbitrary CLI error text: it may contain ephemeral
            # URLs/credentials. Preserve only recognized classification.
            error = payload.get('error', {}) if isinstance(payload, dict) else {}
            code = str(error.get('code', 'BOSS_UNAVAILABLE')) if isinstance(error, dict) else 'BOSS_UNAVAILABLE'
            classified = 'SAFETY_OR_LOGIN' if any(word in code.upper() for word in ('AUTH', 'LOGIN', 'CAPTCHA', 'RISK', 'VERIFY', '36', '37')) else 'BOSS_UNAVAILABLE'
            raise BossBlocked('BOSS 会话读取失败，可能需要登录或官方安全验证；本轮已停止。', classified)
        if not isinstance(payload.get('data'), list):
            raise BossBlocked('BOSS 会话列表结构异常；本轮已停止。', 'INVALID_RESPONSE')
        return payload

    def _read_page(self, page: int) -> dict:
        return self._call(['chat', '--page', str(page)])

    def history(self, uid: str | int, count: int = 50, page: int = 1) -> list[dict]:
        if not str(uid).isdigit() or not 1 <= count <= 100 or not 1 <= page <= 30:
            raise ValueError('无效的会话历史参数')
        data = self._call(['chatmsg', str(uid), '--raw', '--count', str(count), '--page', str(page)])['data']
        if not all(isinstance(item, dict) for item in data):
            raise BossBlocked('BOSS 会话历史结构异常；本轮已停止。', 'INVALID_RESPONSE')
        result = [normalize_message(item) for item in data]
        if any(not item['message_id'] for item in result):
            raise BossBlocked('BOSS 消息缺少可核验标识；本轮已停止。', 'INVALID_RESPONSE')
        return result

    def full_history(self, uid: str | int, known_ids=(), *, max_pages: int = 10) -> list[dict]:
        """Fetch through a known baseline or the end, never silently truncate."""
        known = {str(value) for value in known_ids}
        seen = {}
        for page in range(1, max_pages + 1):
            messages = self.history(uid, count=50, page=page)
            if not messages:
                return sorted(seen.values(), key=lambda m: (m['timestamp'], m['message_id']))
            new_ids = {item['message_id'] for item in messages} - seen.keys()
            if not new_ids:
                raise BossBlocked('BOSS 历史分页重复，无法确认消息覆盖完整。', 'INCOMPLETE_HISTORY')
            for item in messages:
                seen[item['message_id']] = item
            if known.intersection(seen) or len(messages) < 50:
                return sorted(seen.values(), key=lambda m: (m['timestamp'], m['message_id']))
        raise BossBlocked('BOSS 消息历史超出分页安全上限。', 'INCOMPLETE_HISTORY')

    def list_conversations(self) -> list[dict]:
        """Read every page, failing rather than publishing a partial snapshot.

        Installed CLI's chat command currently drops server pagination. In that
        case an empty page is required; a short page alone is not sufficient.
        Future explicit has_more/page_size hints are respected.
        """
        result = []
        seen = {}
        page_signatures = set()
        for page in range(1, self.max_pages + 1):
            payload = self._read_page(page)
            raw_rows = payload['data']
            if not raw_rows:
                return result
            rows = []
            for raw in raw_rows:
                if not isinstance(raw, dict):
                    raise BossBlocked('BOSS 会话条目结构异常；本轮已停止。', 'INVALID_RESPONSE')
                row = normalize_row(raw)
                if not row['uid']:
                    raise BossBlocked('BOSS 会话缺少联系人标识；本轮已停止。', 'INVALID_RESPONSE')
                rows.append(row)
            signature = tuple(message_fingerprint(row) for row in rows)
            if signature in page_signatures:
                raise BossBlocked('BOSS 分页重复，无法确认会话覆盖完整；本轮已停止。', 'INCOMPLETE_PAGINATION')
            page_signatures.add(signature)
            for row in rows:
                previous = seen.get(row['uid'])
                if previous and message_fingerprint(previous) != message_fingerprint(row):
                    raise BossBlocked('BOSS 分页期间会话发生变化，未发布不完整快照。', 'INCONSISTENT_PAGINATION')
                if not previous:
                    seen[row['uid']] = row
                    result.append(row)
            pagination = payload.get('pagination')
            if isinstance(pagination, dict):
                if pagination.get('has_more', pagination.get('hasMore')) is False:
                    return result
                size = pagination.get('page_size', pagination.get('pageSize'))
                if isinstance(size, int) and size > 0 and len(raw_rows) < size:
                    return result
        raise BossBlocked('BOSS 会话超出分页安全上限，未发布部分结果。', 'INCOMPLETE_PAGINATION')


def list_conversations() -> list[dict]:
    return BossReader().list_conversations()
