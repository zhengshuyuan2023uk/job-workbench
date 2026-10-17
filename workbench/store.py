"""Read complete source snapshots; personal state never writes to source files."""
import copy
from datetime import datetime
import json
import os
import tempfile
import fcntl
from pathlib import Path
import threading
from data_model import build_view, TZ
from source_manifest import SOURCES, document_index


class RevisionConflict(ValueError):
    pass


class WorkspaceStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self._view = None
        self._lock = threading.RLock()

    def read_view(self) -> dict:
        with self._lock:
            try:
                paths = {key: self.root / name for key, name in SOURCES.items()}
                if any(p.is_symlink() or not p.resolve().is_relative_to(self.root) for p in paths.values()):
                    raise ValueError('数据源路径越界')
                before = {key: (p.stat().st_mtime_ns, p.stat().st_size) for key, p in paths.items()}
                sources = {key: json.loads(p.read_text(encoding='utf-8')) for key, p in paths.items()}
                if not all(isinstance(source, dict) for source in sources.values()):
                    raise ValueError('数据源应为对象')
                after = {key: (p.stat().st_mtime_ns, p.stat().st_size) for key, p in paths.items()}
                if before != after:
                    raise ValueError('源文件正在更新')
                self._view = build_view(sources, datetime.now(TZ))
                self._view['meta']['demo_mode'] = sources['applications'].get('demo_mode') is True
                self._view['documents'] = document_index(self.root)
            except (OSError, ValueError, TypeError, KeyError) as error:
                result = copy.deepcopy(self._view) if self._view else dict(schema_version=1, summary=None,
                    meta=dict(available=False, loaded_at=None, boss_checked_at=None), jobs=[], tasks=[], constraints={}, documents=[])
                result['meta'].update(stale=True, read_errors=[f'本地记录读取失败（{type(error).__name__}）；保留上次完整结果。'])
                return result
            return copy.deepcopy(self._view)

    def _preference_path(self):
        path = self.root / 'state/个人管理.json'
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise ValueError('个人状态路径不可用')
        return path

    def read_preferences(self) -> dict:
        with self._lock:
            path = self._preference_path()
            if not path.exists():
                return {'revision': 0, 'jobs': {}}
            value = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value, dict) or type(value.get('revision')) is not int or value['revision'] < 0 or not isinstance(value.get('jobs'), dict):
                raise ValueError('个人状态损坏，请保留原文件并恢复备份')
            for jid, fields in value['jobs'].items():
                if not isinstance(jid, str) or not isinstance(fields, dict):
                    raise ValueError('个人状态格式无效')
                self._validate_values({k: v for k, v in fields.items() if k != 'updated_at'})
            return value

    @staticmethod
    def _validate_values(values):
        if not isinstance(values, dict) or set(values) - {'favorite', 'priority', 'note'}:
            raise ValueError('只支持收藏、优先级和备注')
        if 'favorite' in values and type(values['favorite']) is not bool:
            raise ValueError('收藏应为布尔值')
        if 'priority' in values and values['priority'] not in ('高', '普通', '低'):
            raise ValueError('优先级应为高、普通或低')
        if 'note' in values and (not isinstance(values['note'], str) or len(values['note']) > 10000):
            raise ValueError('备注最多10000字')

    def update_preferences(self, job_id: str, values: dict, expected_revision: int) -> dict:
        self._validate_values(values)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('缺少有效版本号')
        with self._lock:
            view = self.read_view()
            if not view['meta']['available'] or job_id not in {j['job_id'] for j in view['jobs']}:
                raise ValueError('岗位不在当前记录中')
            path = self._preference_path()
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path.parent, 0o700)
            lock_path = path.parent / '写入.lock'
            if lock_path.is_symlink():
                raise ValueError('锁文件不可用')
            with lock_path.open('a') as lock:
                os.chmod(lock_path, 0o600)
                fcntl.flock(lock, fcntl.LOCK_EX)
                current = self.read_preferences()
                if expected_revision != current['revision']:
                    raise RevisionConflict('其他窗口已保存修改。请读取最新内容后再合并保存。')
                fields = current['jobs'].get(job_id, {'favorite': False, 'priority': '普通', 'note': ''})
                fields.update(values, updated_at=datetime.now(TZ).isoformat())
                current['jobs'][job_id] = fields
                current['revision'] += 1
                fd, temporary = tempfile.mkstemp(prefix='.保存-', dir=path.parent)
                try:
                    with os.fdopen(fd, 'w', encoding='utf-8') as f:
                        json.dump(current, f, ensure_ascii=False, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(temporary, path)
                except Exception:
                    # Leave failed write material for recovery; never overwrite a corrupt original.
                    raise
                return current
