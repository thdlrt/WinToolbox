"""Skill CLI without starting or reopening the resident engine/database."""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from .features.netease import NetEase, convert
from .jobs import Cancelled


class CommandJob:
    def __init__(self, root, params):
        self.params = params
        self.work_dir = root / 'netease/cli-jobs' / uuid.uuid4().hex
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.cancel_event = threading.Event()
        self.artifacts = []

    def check_cancelled(self):
        if self.cancel_event.is_set():
            raise Cancelled('任务已取消')

    def progress(self, percent, message):
        self.check_cancelled()
        print(f'{percent:.0f}% {message}', file=sys.stderr, flush=True)

    def artifact(self, path, kind='file', label=None):
        self.artifacts.append({'path': str(path), 'kind': kind, 'label': label or Path(path).name})

    def run_process(self, args, timeout=None):
        with (self.work_dir / 'process.log').open('ab') as log:
            process = subprocess.Popen([str(x) for x in args], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                process.wait(timeout=timeout or 600)
            except BaseException:
                process.kill()
                process.wait(timeout=10)
                raise
        if process.returncode:
            raise ValueError(f'{Path(args[0]).name} 执行失败，退出码 {process.returncode}')
        return ''


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='网易云歌单、下载与 NCM 转换')
    parser.add_argument('--data-dir', required=True)
    subs = parser.add_subparsers(dest='command', required=True)
    for name in ['status', 'disconnect', 'login']:
        sub = subs.add_parser(name)
        if name == 'login':
            sub.add_argument('--wait', action='store_true')
    sub = subs.add_parser('qr-check'); sub.add_argument('--key', required=True)
    sub = subs.add_parser('cookie-import'); sub.add_argument('--path', required=True)
    for name in ['playlists', 'likes', 'tracks', 'export']:
        sub = subs.add_parser(name)
        sub.add_argument('--uid', type=int)
        if name in ['tracks', 'export']:
            sub.add_argument('--id', type=int)
        if name == 'export':
            sub.add_argument('--kind', choices=['likes', 'playlist', 'playlists'], default='likes')
            sub.add_argument('--output-dir', required=True)
    for name in ['convert', 'download']:
        sub = subs.add_parser(name)
        if name == 'convert':
            sub.add_argument('paths', nargs='+')
        else:
            sub.add_argument('--ids', required=True, help='逗号分隔的歌曲 ID')
        sub.add_argument('--output-dir', required=True)
        sub.add_argument('--format', choices=['mp3', 'original'], default='mp3')
        sub.add_argument('--bitrate', type=int, choices=[128, 192, 256, 320], default=320)
    args = vars(parser.parse_args())
    root = Path(args.pop('data_dir')).resolve()
    command = args.pop('command')
    service = NetEase(SimpleNamespace(data_dir=root))
    try:
        if command == 'login':
            value = service.qr_start({})
            import qrcode
            path = root / 'netease/login-qr.png'
            qrcode.make(value['url']).save(path)
            if args['wait']:
                print(json.dumps({'qr_path': str(path), 'expires_seconds': 180}, ensure_ascii=False), flush=True)
                until = time.monotonic() + 180
                key = value['key']
                while time.monotonic() < until:
                    value = service.qr_check({'key': key})
                    if value['code'] in (800, 803, 8821):
                        break
                    time.sleep(2.5)
            else:
                value = {'key': value['key'], 'qr_path': str(path), 'expires_seconds': 180}
        elif command in ('convert', 'download', 'export'):
            if command == 'download':
                args['ids'] = [int(value) for value in args['ids'].split(',')]
            job = CommandJob(root, args)
            try:
                result = convert(job) if command == 'convert' else getattr(service, command)(job)
            except Exception:
                print(json.dumps({'artifacts': job.artifacts}, ensure_ascii=False), flush=True)
                raise
            value = {'result': result, 'artifacts': job.artifacts}
        else:
            method = {'qr-check': 'qr_check', 'cookie-import': 'import_cookie'}.get(command, command)
            value = getattr(service, method)(args)
        print(json.dumps(value, ensure_ascii=False), flush=True)
    except (KeyboardInterrupt, Exception) as exc:
        print(json.dumps({'error': '已取消' if isinstance(exc, KeyboardInterrupt) else str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
