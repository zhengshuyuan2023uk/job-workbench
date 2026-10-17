#!/usr/bin/env python3
"""Loopback-only workbench. Tasks run only when its buttons are used."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
from urllib.parse import unquote, urlsplit

from store import WorkspaceStore, RevisionConflict
from source_manifest import document_path
from automation_state import AutomationState, RevisionConflict as AutomationRevisionConflict, InvalidControl
from master_record import read_master
from workbench_tasks import WorkbenchTasks

APP_ID = 'findjobs-local-workbench'
STATIC = Path(__file__).resolve().parent / 'static'


def project_id(root):
    return hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:20]


def create_server(root: Path, host: str = '127.0.0.1', port: int = 8765, tasks=None) -> ThreadingHTTPServer:
    if host != '127.0.0.1':
        raise ValueError('工作台仅允许监听127.0.0.1')
    store = WorkspaceStore(root)
    automation = AutomationState(root)
    tasks = tasks if tasks is not None else WorkbenchTasks(root)
    token = secrets.token_urlsafe(32)

    def automation_status():
        monitor = read_master(root).get('reply_monitor', {})
        owner = monitor.get('execution_owner')
        result = automation.status()
        result.update(execution_owner=owner, controls_enabled=False)
        if owner == 'heartbeat-boss':
            issue = monitor.get('last_connection_issue') or {}
            result.update(heartbeat_last_check_at=monitor.get('last_checked_at'),
                          heartbeat_issue=issue.get('message') if issue.get('status') == 'unresolved' else None)
        return result

    class Handler(BaseHTTPRequestHandler):
        server_version = 'LocalWorkbench/1'

        def log_message(self, *args):
            pass

        def respond(self, status, body, content_type='application/json; charset=utf-8'):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode('utf-8')
            elif isinstance(body, str):
                body = body.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def allowed(self, write=False):
            expected = f'127.0.0.1:{self.server.server_port}'
            origin = f'http://{expected}'
            if self.headers.get('Host') != expected or self.headers.get('Origin') not in (None, origin):
                self.respond(403, {'error': '仅允许从本机工作台访问'})
                return False
            if write and (self.headers.get('Origin') != origin or
                          not secrets.compare_digest(self.headers.get('X-Workbench-Token', ''), token) or
                          self.headers.get('Content-Type', '').split(';')[0] != 'application/json'):
                self.respond(403, {'error': '写入验证失败，请刷新工作台后重试'})
                return False
            return True

        def do_GET(self):
            if not self.allowed():
                return
            path = unquote(urlsplit(self.path).path)
            try:
                if path == '/api/health':
                    return self.respond(200, {'app': APP_ID, 'version': 1, 'project_id': project_id(root)})
                if path == '/api/session':
                    return self.respond(200, {'token': token})
                if path == '/api/view':
                    return self.respond(200, store.read_view())
                if path == '/api/preferences':
                    return self.respond(200, store.read_preferences())
                if path == '/api/tasks':
                    return self.respond(200, tasks.status())
                if path == '/api/automation':
                    return self.respond(200, automation_status())
                if path.startswith('/api/documents/'):
                    doc_id = path.removeprefix('/api/documents/')
                    document = document_path(root, doc_id)
                    return self.respond(200, {'id': doc_id, 'text': document.read_text(encoding='utf-8')})
                assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                          '/styles.css': ('styles.css', 'text/css; charset=utf-8'),
                          '/app.js': ('app.js', 'text/javascript; charset=utf-8')}
                if path in assets:
                    name, mime = assets[path]
                    return self.respond(200, (STATIC / name).read_bytes(), mime)
                return self.respond(404, {'error': '页面或资料不存在'})
            except FileNotFoundError:
                return self.respond(404, {'error': '资料不在可读清单中或尚未创建'})
            except (ValueError, OSError):
                return self.respond(503, {'error': '本地资料读取失败，原文件已保留。'})

        def do_PUT(self):
            if not self.allowed(write=True):
                return
            path = unquote(urlsplit(self.path).path)
            if path != '/api/automation/control' and not path.startswith('/api/preferences/'):
                return self.respond(404, {'error': '接口不存在'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length < 1 or length > 100000:
                    raise ValueError('请求大小不正确')
                payload = json.loads(self.rfile.read(length))
                if path == '/api/automation/control':
                    if (not isinstance(payload, dict) or set(payload) != {'action', 'expected_revision'} or
                            payload['action'] not in ('pause', 'resume', 'recheck') or
                            type(payload['expected_revision']) is not int):
                        raise ValueError('控制请求字段不正确')
                    raise InvalidControl('常驻已停用，请使用当前工作台任务按钮。')
                if not isinstance(payload, dict) or set(payload) != {'values', 'expected_revision'}:
                    raise ValueError('请求字段不正确')
                result = store.update_preferences(path.removeprefix('/api/preferences/'), payload['values'], payload['expected_revision'])
                return self.respond(200, result)
            except (RevisionConflict, AutomationRevisionConflict) as error:
                return self.respond(409, {'error': str(error)})
            except (InvalidControl, ValueError, TypeError) as error:
                return self.respond(400, {'error': str(error)})
            except OSError:
                return self.respond(503, {'error': '保存失败，输入仍保留在页面中。'})

        def do_POST(self):
            if not self.allowed(write=True):
                return
            if unquote(urlsplit(self.path).path) != '/api/tasks':
                return self.respond(404, {'error': '接口不存在'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 1 <= length <= 1000:
                    raise ValueError('请求大小不正确')
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict) or set(payload) != {'action'} or payload['action'] not in ('collect', 'followup', 'stop'):
                    raise ValueError('仅支持收集、跟进或停止本轮')
                result = tasks.stop() if payload['action'] == 'stop' else tasks.start(payload['action'])
                return self.respond(202, result)
            except RuntimeError as error:
                return self.respond(409, {'error': str(error)})
            except (TypeError, ValueError) as error:
                return self.respond(400, {'error': str(error)})
            except OSError:
                return self.respond(503, {'error': '任务状态保存失败，未确认执行。'})

        def do_OPTIONS(self):
            self.respond(403, {'error': '不允许跨站访问'})

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.tasks = tasks
    return server


def main():
    # The public launcher always defaults to isolated synthetic demo data.
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from run import main as launch
    launch()


if __name__ == '__main__':
    main()
