"""Browser regression harness: delayed saves, synthetic data only. Not a production option."""
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import create_server

root = Path(__file__).resolve().parents[1] / '验证/交互样例'
server = create_server(root, port=8767)
original_put = server.RequestHandlerClass.do_PUT

def delayed_put(handler):
    time.sleep(2)
    original_put(handler)

server.RequestHandlerClass.do_PUT = delayed_put
print('独立交互测试：8767，保存延迟2秒', flush=True)
try:
    server.serve_forever()
except KeyboardInterrupt:
    pass
finally:
    server.server_close()
