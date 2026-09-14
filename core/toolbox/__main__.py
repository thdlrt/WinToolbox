import argparse
import json
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .app import App


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(Path(os.environ.get("LOCALAPPDATA", Path.home())) / "WinToolbox"))
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    writer_lock = threading.Lock()
    def send(value):
        with writer_lock:
            print(json.dumps(value, ensure_ascii=False, allow_nan=False, default=str), flush=True)
    app = App(args.data_dir, emit=lambda event: send({"jsonrpc":"2.0","method":"event","params":event}))
    pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="rpc")
    def handle(message):
        id = message.get("id")
        try:
            if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
                raise ValueError("无效 JSON-RPC 请求")
            result = app.call(message["method"], message.get("params", {}))
            if "id" in message:
                send({"jsonrpc":"2.0","id":id,"result":result})
        except Exception as exc:
            code = -32601 if isinstance(exc, LookupError) else -32602 if isinstance(exc, (ValueError, KeyError, TypeError)) else -32000
            if "id" in message:
                send({"jsonrpc":"2.0","id":id,"error":{"code":code,"message":str(exc),"data":{"type":type(exc).__name__}}})
    try:
        for line in sys.stdin:
            try:
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("请求须为对象")
            except (ValueError, json.JSONDecodeError):
                send({"jsonrpc":"2.0","id":None,"error":{"code":-32700,"message":"JSON 解析失败"}})
                continue
            pool.submit(handle, message)
    finally:
        pool.shutdown(wait=True)
        app.close()


if __name__ == "__main__":
    main()
