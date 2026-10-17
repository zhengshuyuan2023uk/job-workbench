"""Explicit local sources. Never expose the project directory as static files."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SOURCES = {'applications': 'applications.json'}

# Optional, explicitly allow-listed documents. Missing documents remain absent.
DOCUMENTS = [
    ('resume', 'resume.md', '个人资料', '当前简历母版'),
    ('facts', 'facts.md', '个人资料', '确认的事实、表达边界与作品来源'),
    ('learning', 'learning.md', '复盘与报告', '沟通经验与实际结果'),
]
BACKUPS = []


def document_path(root: Path, doc_id: str) -> Path:
    name = next((name for ident, name, _, _ in DOCUMENTS if ident == doc_id), None)
    if not name:
        raise FileNotFoundError('资料不在可读清单中')
    path = root / name
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise FileNotFoundError('资料不可用')
    return path


def document_index(root: Path) -> list:
    result = []
    for ident, name, category, purpose in DOCUMENTS:
        try:
            path = document_path(root, ident)
            modified = datetime.fromtimestamp(path.stat().st_mtime, ZoneInfo('Asia/Shanghai')).isoformat()
            readable = True
        except FileNotFoundError:
            continue
        result.append(dict(id=ident, title=Path(name).name, path=name, category=category,
                           purpose=purpose, modified_at=modified, readable=readable,
                           retention_note='建议保留，作为求职依据。'))
    for ident, name, purpose in BACKUPS:
        result.append(dict(id=ident, title=name, path=name, category='归档', purpose=purpose,
                           modified_at=None, readable=False, retention_note='建议保留；删除将失去恢复参考。'))
    for ident, name in SOURCES.items():
        result.append(dict(id='source-' + ident, title=name, path=name, category='数据',
                           purpose='原始记录；工作台按核验口径读取', modified_at=None,
                           readable=False, retention_note='持续保留，勿复制成第二份可写主记录。'))
    return result
