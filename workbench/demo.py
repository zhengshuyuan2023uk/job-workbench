"""Offline demo runner. Only local fixture reads, writes and interruptible waits."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import threading
import uuid
from zoneinfo import ZoneInfo

from demo_data import NOTICE, DOCUMENT_TEXT, build_demo_record, demo_items
from source_manifest import SOURCES, DOCUMENTS


def _now():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('演示文件不可为符号链接')
    temporary = path.with_name('.'+path.name+'.demo-tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    temporary.replace(path)


def initialize_demo(root, *, reset=False):
    root = Path(root)
    if root.is_symlink():
        raise ValueError('演示目录不可为符号链接')
    root.mkdir(parents=True, exist_ok=True)
    main = root / SOURCES['applications']
    if main.exists():
        if main.is_symlink() or json.loads(main.read_text(encoding='utf-8')).get('demo_mode') is not True:
            raise ValueError('目标目录已有非演示记录；拒绝覆盖')
        if not reset:
            return root
    for key, filename in SOURCES.items():
        _write(root / filename, build_demo_record() if key == 'applications' else {})
    for ident, filename, _, purpose in DOCUMENTS:
        path = root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise ValueError('演示资料不可为符号链接')
        path.write_text(DOCUMENT_TEXT.get(ident, '# '+purpose+'\n\n'+NOTICE+'\n\n这是演示资料。请在岗位详情中查看虚构消息、案例与个人备注。')+'\n', encoding='utf-8')
    return root


class DemoTasks:
    """Implements the existing task API without loading any live connector."""
    def __init__(self, root, *, step_delay=1.0):
        self.root = initialize_demo(root)
        self.path = self.root / SOURCES['applications']
        self.step_delay = step_delay
        self.mutex = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        with self.mutex:
            record = self._read()
            state = record['reply_monitor']['workbench_tasks']
            if state['status'] in ('running', 'stopping'):
                state.update(status='stopped', phase='stopped', finished_at=_now(),
                    message='上次本地演示已中断。已完成步骤保留，不会重复模拟。')
                self._save(record)

    def _read(self):
        if self.path.is_symlink():
            raise ValueError('演示文件不可为符号链接')
        record = json.loads(self.path.read_text(encoding='utf-8'))
        if record.get('demo_mode') is not True:
            raise ValueError('此执行器只接受虚构演示数据')
        return record

    def _save(self, record):
        _write(self.path, record)

    def status(self):
        with self.mutex:
            result = deepcopy(self._read()['reply_monitor']['workbench_tasks'])
            items = result.get('items', [])
            result['demo'] = True
            result['counts'] = dict(result.get('counts', {}),
                ready=sum(i['status']=='ready' for i in items),
                sent=sum(i['status']=='simulated_sent' for i in items),
                needs_user=sum(i['status']=='awaiting_user' for i in items),
                skipped=sum(i['status']=='ignored' for i in items),
                uncertain=sum(i['status']=='uncertain' for i in items))
            return result

    def start(self, mode):
        if mode not in ('collect', 'followup'):
            raise ValueError('演示仅支持收集与跟进')
        with self.mutex:
            if self.thread and self.thread.is_alive():
                raise RuntimeError('本轮演示正在执行，不能重复启动')
            if mode == 'followup' and not self.status()['counts']['ready']:
                raise ValueError('没有可模拟的待办；请先收集并分析')
            record = self._read()
            record['reply_monitor']['workbench_tasks'].update(demo=True, status='running', run_id=uuid.uuid4().hex,
                mode=mode, phase='collecting' if mode=='collect' else 'simulating', started_at=_now(),
                finished_at=None, error=None, message='正在读取本地虚构会话…' if mode=='collect' else '正在本地模拟跟进，不会发送真实消息。')
            self._save(record)
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._run, args=(mode,), daemon=True, name='offline-demo')
            self.thread.start()
            return self.status()

    def stop(self):
        self.stop_event.set()
        with self.mutex:
            if self.thread and self.thread.is_alive():
                record = self._read()
                record['reply_monitor']['workbench_tasks'].update(status='stopping', message='正在停止剩余模拟步骤；已完成步骤保留。')
                self._save(record)
            return self.status()

    def join(self, timeout=None):
        if self.thread:
            self.thread.join(timeout)

    def _run(self, mode):
        try:
            with self.mutex:
                record = self._read()
                previous = deepcopy(record['reply_monitor']['workbench_tasks'].get('items', []))
                plan = demo_items(record) if mode == 'collect' else deepcopy(record['reply_monitor']['workbench_tasks']['items'])
            for index, item in enumerate(plan):
                if mode == 'followup' and item['status'] != 'ready':
                    continue
                if self.stop_event.wait(self.step_delay):
                    break
                with self.mutex:
                    if self.stop_event.is_set():
                        break
                    record = self._read()
                    state = record['reply_monitor']['workbench_tasks']
                    if mode == 'collect':
                        processed = {entry['action_id'] for entry in plan[:index+1]}
                        state['items'] = deepcopy(plan[:index+1]) + [entry for entry in previous if entry['action_id'] not in processed]
                        state['counts'] = {'conversations': index+1, 'changed': index+1}
                        state['message'] = f"已分析 {index+1}/{len(plan)} 段本地虚构会话：{item['company']}"
                    else:
                        self._simulate(record, item)
                        state['message'] = '已完成一项本地模拟：'+item['company']+'；未联系真实招聘方。'
                    record['reply_monitor']['last_checked_at'] = _now()
                    self._save(record)
            with self.mutex:
                record = self._read()
                stopped = self.stop_event.is_set()
                record['reply_monitor']['workbench_tasks'].update(status='stopped' if stopped else 'completed',
                    phase='stopped' if stopped else 'done', finished_at=_now(),
                    message='本轮演示已停止，未完成事项保留。' if stopped else
                    '分析完成：3 项可跟进、1 项需本人决定、1 项低薪跳过、1 项未知。所有数据均为虚构。' if mode=='collect' and not record['demo_completed_actions'] else
                    '本轮本地模拟已完成。已处理事项不会重复模拟，需本人决定与未知事项继续保留。')
                self._save(record)
        except Exception as error:
            with self.mutex:
                record = self._read()
                record['reply_monitor']['workbench_tasks'].update(status='failed', phase='error',
                    finished_at=_now(), error='本地演示未完成：'+str(error), message='演示遇到本地文件错误，没有执行外部操作。')
                self._save(record)

    def _simulate(self, record, item):
        key = item['action_id']
        completed = record['demo_completed_actions']
        if key not in completed:
            app = next(a for a in record['applications'] if a['conversation_uid'] == item['uid'])
            message = '[本地模拟附件] 演示简历.pdf；没有上传或发送文件。' if item['kind']=='resume' else '[本地模拟回复] '+item['text']
            app['conversation_messages'].append(dict(message_id=key+':result', conversation_uid=item['uid'],
                actor='agent', speaker='本地模拟', kind='outgoing_text', body_type=1, timestamp_iso=_now(), text=message))
            completed.append(key)
        for current in record['reply_monitor']['workbench_tasks']['items']:
            if current['action_id'] == key:
                current.update(status='simulated_sent', reason='已完成本地模拟；没有向任何招聘方发送消息或附件。')
