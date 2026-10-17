"""Private, durable runtime state. This module never contacts BOSS or sends messages.

The event journal stores execution intent; the existing master JSON remains the
business/case record. Uncertain dispatches require reconciliation, never a retry.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile


class RevisionConflict(ValueError):
    pass


class InvalidControl(ValueError):
    pass


class StateCorrupt(ValueError):
    pass


class RunnerAlreadyActive(RuntimeError):
    pass


class EventConflict(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _directory(root):
    root = Path(root).resolve()
    directory = root / 'state'
    for path in (directory,):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('自动回复状态路径不可用')
        path.mkdir(exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    return directory


def _path(root, name):
    path = _directory(root) / name
    if path.is_symlink():
        raise ValueError('自动回复文件不可为链接')
    return path


@contextmanager
def _lock(root, name='自动回复事务.lock', nonblocking=False):
    path = _path(root, name)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        except BlockingIOError as error:
            raise RunnerAlreadyActive('已有常驻执行器运行，拒绝启动第二个发送方') from error
        yield
    finally:
        os.close(fd)


@contextmanager
def exclusive_runner_lock(root):
    """Hold for the entire runner lifetime, including reconciliation and sends."""
    with _lock(root, '自动回复执行器.lock', nonblocking=True):
        yield


def _read(path, default):
    if not path.exists():
        return deepcopy(default)
    try:
        result = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(result, dict):
            raise ValueError('必须为对象')
        return result
    except (ValueError, UnicodeError) as error:
        raise StateCorrupt(f'{path.name} 损坏；保留原文件，停止写入') from error


def _write(path, value):
    # Serialise first, so invalid values cannot produce a partial temporary file.
    content = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    fd, temporary = tempfile.mkstemp(prefix='.' + path.stem + '-', dir=path.parent)
    # Failed temporary files are intentionally kept for recovery.
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


_DEFAULT = dict(
    schema_version=1, revision=0, mode='shadow', paused=True, status='stopped',
    last_heartbeat_at=None, last_check_at=None, next_check_at=None,
    pending_count=0, in_flight=None, issue=None, last_action=None,
    worker_pid=None, started_at=None, active_until=None,
    detection_interval_seconds=None, last_latency_seconds=None,
    recheck_requested_at=None, control_updated_at=None,
)
_TIMES = {
    'last_heartbeat_at', 'last_check_at', 'next_check_at', 'started_at',
    'active_until', 'recheck_requested_at', 'control_updated_at',
}
_NESTED = {
    'in_flight': {'event_key', 'phase', 'kind', 'since', 'company', 'job_id'},
    'issue': {'code', 'message', 'first_seen_at', 'last_seen_at', 'requires_user_action'},
    'last_action': {'kind', 'type', 'at', 'company', 'job_id', 'status', 'event_key'},
}
_STATUSES = {'starting', 'running', 'pausing', 'paused', 'blocked', 'stopped', 'offline', 'unknown', 'error'}


def _validate_fields(fields):
    if not isinstance(fields, dict) or set(fields) - (_DEFAULT.keys() - {'schema_version', 'revision'}):
        raise ValueError('不支持的自动回复状态字段')
    for key, value in fields.items():
        valid = True
        if key == 'mode':
            valid = value in ('shadow', 'live')
        elif key == 'paused':
            valid = type(value) is bool
        elif key == 'status':
            valid = isinstance(value, str) and value in _STATUSES
        elif key in _TIMES:
            valid = value is None or isinstance(value, str)
            if valid and value is not None:
                try:
                    valid = datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is not None
                except ValueError:
                    valid = False
        elif key in ('pending_count', 'worker_pid'):
            valid = (key == 'worker_pid' and value is None) or (type(value) is int and value >= 0)
        elif key in ('detection_interval_seconds', 'last_latency_seconds'):
            valid = value is None or (type(value) in (int, float) and value >= 0 and value < float('inf'))
        elif key in _NESTED:
            valid = value is None or (isinstance(value, dict) and not set(value) - _NESTED[key])
            if valid and isinstance(value, dict):
                valid = all(v is None or type(v) in (str, int, float, bool) for v in value.values())
                valid = valid and all(not isinstance(v, str) or len(v) <= 1000 for v in value.values())
        if not valid:
            raise ValueError(f'无效的自动回复状态字段：{key}')


class AutomationState:
    """Small public status snapshot, with revision-checked local controls."""

    def __init__(self, root, stale_after_seconds=90):
        self.root = Path(root).resolve()
        self.stale_after_seconds = stale_after_seconds

    def _read(self):
        value = _read(_path(self.root, '自动回复状态.json'), _DEFAULT)
        try:
            if set(value) - _DEFAULT.keys() or value.get('schema_version') != 1:
                raise ValueError('未知状态格式')
            if type(value.get('revision')) is not int or value['revision'] < 0:
                raise ValueError('无效版本')
            _validate_fields({key: val for key, val in value.items() if key not in ('schema_version', 'revision')})
            if not {'mode', 'paused', 'status'}.issubset(value):
                raise ValueError('缺少必要字段')
        except (ValueError, TypeError) as error:
            raise StateCorrupt('自动回复状态损坏；保留原文件，停止写入') from error
        return {**deepcopy(_DEFAULT), **value}

    def _snapshot(self, value):
        result = deepcopy(value)
        result['stored_status'] = value['status']
        timestamp = value.get('last_heartbeat_at')
        result['heartbeat_stale'] = False
        if timestamp:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(timestamp.replace('Z', '+00:00'))).total_seconds()
            if age > self.stale_after_seconds or age < -60:
                result['status'] = 'offline'
                result['heartbeat_stale'] = True
        result['health'] = result['status']
        return result

    def status(self):
        with _lock(self.root):
            return self._snapshot(self._read())

    def update(self, **fields):
        _validate_fields(fields)
        with _lock(self.root):
            value = self._read()
            value.update(deepcopy(fields))
            value['revision'] += 1
            _write(_path(self.root, '自动回复状态.json'), value)
            return self._snapshot(value)

    def control(self, action, expected_revision):
        if action not in ('pause', 'resume', 'recheck'):
            raise InvalidControl('仅支持暂停、恢复或只读连接复查')
        if type(expected_revision) is not int or expected_revision < 0:
            raise InvalidControl('需要有效的当前版本号')
        with _lock(self.root):
            value = self._read()
            if value['revision'] != expected_revision:
                raise RevisionConflict('状态已经变化，请刷新后重试')
            if action == 'pause':
                value['paused'] = True
                value['status'] = 'pausing' if value['in_flight'] else ('blocked' if value['issue'] else 'paused')
            elif action == 'resume':
                if value['status'] == 'blocked' or value['issue']:
                    raise InvalidControl('连接故障尚未核验恢复，请完成官方验证后请求只读复查')
                value['paused'] = False
                value['status'] = 'starting'
            else:
                # A recheck never implicitly promotes shadow mode or permits sends.
                value['paused'] = True
                value['recheck_requested_at'] = _now()
                value['status'] = 'blocked' if value['issue'] else 'paused'
            value['control_updated_at'] = _now()
            value['revision'] += 1
            _write(_path(self.root, '自动回复状态.json'), value)
            return self._snapshot(value)


_TERMINAL_EVENTS = {
    'verified_sent', 'observed_sent', 'completed', 'done', 'cancelled', 'ignored',
    'awaiting_user', 'failed', 'superseded', 'notified',
}
_RECONCILIATION_EVENTS = {'dispatching', 'uncertain'}
_UNSTARTED_EVENTS = {'pending', 'authorized_pending_send', 'planning', 'planned', 'ready'}


class EventStore:
    """Durable unique intents. Pending includes unresolved dispatches for readback."""

    def __init__(self, root):
        self.root = Path(root).resolve()

    def _read(self):
        value = _read(_path(self.root, '自动回复事件.json'), {'schema_version': 1, 'revision': 0, 'events': {}})
        if (value.get('schema_version') != 1 or type(value.get('revision')) is not int
                or value['revision'] < 0 or not isinstance(value.get('events'), dict)):
            raise StateCorrupt('自动回复事件文件损坏；保留原文件，停止写入')
        for key, event in value['events'].items():
            if (not isinstance(event, dict) or event.get('event_key') != key
                    or not isinstance(event.get('status'), str)):
                raise StateCorrupt('自动回复事件格式损坏；保留原文件，停止写入')
        return value

    def _write(self, value):
        value['revision'] += 1
        _write(_path(self.root, '自动回复事件.json'), value)

    def enqueue(self, event_key, **fields):
        if not isinstance(event_key, str) or not event_key or len(event_key) > 1000:
            raise ValueError('需要有效且稳定的事件标识')
        if {'event_key', 'created_at', 'updated_at'} & fields.keys():
            raise ValueError('事件身份与时间由事件库生成')
        if 'status' in fields and (not isinstance(fields['status'], str) or not fields['status']):
            raise ValueError('无效事件状态')
        with _lock(self.root):
            value = self._read()
            if event_key in value['events']:
                return deepcopy(value['events'][event_key])
            timestamp = _now()
            event = dict(event_key=event_key, status='pending', created_at=timestamp, updated_at=timestamp)
            event.update(deepcopy(fields))
            value['events'][event_key] = event
            self._write(value)
            return deepcopy(event)

    def get(self, event_key):
        with _lock(self.root):
            return deepcopy(self._read()['events'].get(event_key))

    def update(self, event_key, *, expected_status=None, **fields):
        if {'event_key', 'created_at', 'updated_at'} & fields.keys():
            raise ValueError('事件身份与时间不可覆盖')
        with _lock(self.root):
            value = self._read()
            event = value['events'].get(event_key)
            if event is None:
                raise KeyError(event_key)
            if expected_status is not None and event['status'] != expected_status:
                raise EventConflict('事件已被其他操作处理')
            old, new = event['status'], fields.get('status', event['status'])
            if not isinstance(new, str) or not new:
                raise ValueError('无效事件状态')
            confirmed_no_click = (old == 'dispatching' and new == 'authorized_pending_send'
                                  and fields.get('execution_not_started') is True)
            if ((old in _RECONCILIATION_EVENTS and new not in (_RECONCILIATION_EVENTS | _TERMINAL_EVENTS) and not confirmed_no_click)
                    or (old == 'uncertain' and new == 'dispatching')
                    or (old in _TERMINAL_EVENTS and new not in _TERMINAL_EVENTS)):
                raise EventConflict('不可重新投递已执行或结果未知的事件；先回读核验')
            event.update(deepcopy(fields), updated_at=_now())
            self._write(value)
            return deepcopy(event)

    def pending(self):
        with _lock(self.root):
            events = self._read()['events'].values()
            return deepcopy([event for event in events if event['status'] not in _TERMINAL_EVENTS])
