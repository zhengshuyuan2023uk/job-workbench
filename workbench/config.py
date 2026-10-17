"""One local JSON configuration. Importing this module enables no external work."""
from dataclasses import dataclass, fields
from functools import lru_cache
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    live_enabled: bool = False
    self_uid: str = ''
    self_name: str = ''
    attachment_name: str = ''
    notification_email: str = ''
    notifications_enabled: bool = False
    codex_executable: str = 'codex'
    cdp_url: str = 'http://127.0.0.1:9229'

    def validate(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name in ('live_enabled', 'notifications_enabled'):
                if type(value) is not bool:
                    raise ValueError(f'{field.name} must be true or false')
            elif not isinstance(value, str):
                raise ValueError(f'{field.name} must be a string')
        endpoint = urlsplit(self.cdp_url)
        if (endpoint.scheme != 'http' or endpoint.hostname != '127.0.0.1'
                or endpoint.username or endpoint.password or endpoint.path not in ('', '/')
                or endpoint.query or endpoint.fragment or not endpoint.port):
            raise ValueError('cdp_url must be http://127.0.0.1:PORT')
        if not self.codex_executable.strip():
            raise ValueError('codex_executable is required')
        if self.self_uid and not self.self_uid.isdigit():
            raise ValueError('self_uid must contain only digits')
        if self.notification_email and ('@' not in self.notification_email or any(c.isspace() for c in self.notification_email)):
            raise ValueError('notification_email is invalid')
        if any('\n' in v or '\r' in v for v in (self.self_name, self.attachment_name, self.notification_email)):
            raise ValueError('identity fields cannot contain newlines')
        return self

    def require_live(self):
        self.validate()
        if not self.live_enabled:
            raise ValueError('真实连接未启用；先设置 live_enabled，再显式使用 --live')
        if not all((self.self_uid, self.self_name, self.attachment_name)):
            raise ValueError('真实连接需要配置 self_uid、self_name、attachment_name')
        if self.notifications_enabled and not self.notification_email:
            raise ValueError('启用邮件提醒需要 notification_email')
        return self


# Tests inject a synthetic immutable configuration; application code never sets it.
_override = None


@lru_cache(maxsize=8)
def _load(path):
    if not path:
        return Config()
    source = Path(path).expanduser()
    data = json.loads(source.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or set(data) - {field.name for field in fields(Config)}:
        raise ValueError('配置必须为对象，且只能包含 configuration.md 中的字段')
    return Config(**data).validate()


def get_config():
    return _override if _override is not None else _load(os.environ.get('JOB_WORKBENCH_CONFIG', ''))
