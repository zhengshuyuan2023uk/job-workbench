#!/usr/bin/env python3
"""Start the local demo; real connections require --live and explicit config."""
import argparse
from pathlib import Path
import sys
import webbrowser

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / 'workbench'))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Job Workbench — 本机求职工作台')
    parser.add_argument('--live', action='store_true', help='显式启用已配置的真实连接')
    parser.add_argument('--data-dir', type=Path, help='独立数据目录；默认 .local/demo 或 .local/live')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--open', action='store_true', help='打开默认浏览器')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('端口须为 1 至 65535')
    from config import get_config
    from server import create_server
    root = (args.data_dir or REPO / '.local' / ('live' if args.live else 'demo')).resolve()
    try:
        if args.live:
            get_config().require_live()
            from master_record import read_master
            record = read_master(root)
            if record.get('demo_mode') is True:
                raise ValueError('演示数据不可用于真实模式，请使用独立数据目录')
            if not (root / 'facts.md').is_file():
                raise ValueError('真实模式需要 facts.md；配置方法见 docs/configuration.md')
            if record.get('reply_monitor', {}).get('execution_owner') != 'workbench-app-server':
                raise ValueError('applications.json 的执行权必须为 workbench-app-server')
            from workbench_tasks import WorkbenchTasks
            tasks = WorkbenchTasks(root, deliver_notifications=get_config().notifications_enabled)
        else:
            from demo import initialize_demo, DemoTasks
            initialize_demo(root)
            tasks = DemoTasks(root)
        server = create_server(root, port=args.port, tasks=tasks)
    except (OSError, ValueError) as error:
        parser.exit(1, f'启动失败：{error}\n')
    url = f'http://127.0.0.1:{args.port}'
    mode = '真实模式：仅按钮触发操作' if args.live else '演示模式：全部为虚构数据，不连接外部服务'
    print(f'{mode}\n{url}\n按 Control+C 停止。', flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        tasks.stop()
        tasks.join(12)
        server.server_close()


if __name__ == '__main__':
    main()
