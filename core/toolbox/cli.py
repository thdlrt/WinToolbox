"""JSON CLI client. Never starts another App or writes desktop data directly."""
import argparse
import http.client
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid

from .local_rpc import ENDPOINT_FILE, MAX_REQUEST, MAX_RESPONSE
from .settings import protect


class CliError(Exception):
    pass


def default_data_dir():
    if os.environ.get('WINTOOLBOX_DATA_DIR'):
        return Path(os.environ['WINTOOLBOX_DATA_DIR'])
    bundle = Path(sys.executable).resolve().parent.parent
    if (bundle / 'portable.flag').is_file():
        return bundle / 'data'
    return Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'WinToolbox'


class Client:
    def __init__(self, data_dir, timeout=30, *, unseal=None):
        self.timeout = timeout
        try:
            config = json.loads((Path(data_dir) / ENDPOINT_FILE).read_text('utf-8'))
            if (config.get('protocol') != 1 or config.get('host') != '127.0.0.1'
                    or type(config.get('port')) is not int or not 1 <= config['port'] <= 65535):
                raise ValueError()
            self.port = config['port']
            self.token = unseal(config['token_dpapi']) if unseal else protect(config['token_dpapi'], decrypt=True)
        except FileNotFoundError:
            raise CliError('工具箱尚未运行或版本不支持 CLI。请打开更新后的 WinToolbox，并核对 --data-dir。') from None
        except Exception:
            raise CliError('无法读取本机 CLI 凭据。请以同一 Windows 用户重新打开工具箱。') from None

    def call(self, method, params=None):
        request_id = uuid.uuid4().hex
        raw = json.dumps({'jsonrpc': '2.0', 'id': request_id, 'method': method,
                          'params': {} if params is None else params}, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(raw) > MAX_REQUEST:
            raise CliError('请求超过 1 MiB，请缩小参数或使用文件路径。')
        # http.client makes a direct loopback connection, ignoring proxy env vars.
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=self.timeout)
        try:
            connection.request('POST', '/rpc', body=raw,
                               headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            body = response.read(MAX_RESPONSE + 1)
            if len(body) > MAX_RESPONSE:
                raise CliError('响应过大，请缩小查询范围。')
            if response.status != 200:
                raise CliError(f'本机 CLI 接口拒绝请求（HTTP {response.status}），请重新打开工具箱。')
            value = json.loads(body)
            if value.get('id') != request_id or value.get('jsonrpc') != '2.0':
                raise CliError('本机 CLI 响应不匹配。')
            if 'error' in value:
                raise CliError(value['error']['message'])
            return value['result']
        except (OSError, http.client.HTTPException):
            raise CliError('无法连接正在运行的工具箱，或请求已超时。写入结果未知时请用原 request_id 查询/重试，勿生成重复记录。') from None
        finally:
            connection.close()


def object_input(args):
    if getattr(args, 'input', None):
        text = sys.stdin.read() if args.input == '-' else Path(args.input).read_text('utf-8-sig')
    else:
        text = getattr(args, 'params', '{}')
    value = json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('JSON 不支持非有限数字')))
    if not isinstance(value, dict):
        raise CliError('输入必须为 JSON 对象。')
    return value


def wait_job(client, job, timeout):
    deadline = time.monotonic() + timeout
    while job.get('status') in ('queued', 'running', 'cancelling'):
        if time.monotonic() >= deadline:
            raise CliError('等待任务超时；任务仍可继续。请用 jobs get/wait ' + job['id'] + ' 查询，不要重复提交。')
        time.sleep(.2)
        job = client.call('jobs.get', {'id': job['id']})
    if job.get('status') != 'completed':
        raise CliError(json.dumps({'job_id': job.get('id'), 'status': job.get('status'),
                                   'error': job.get('error'), 'message': job.get('message')}, ensure_ascii=False))
    return job


def parser():
    root = argparse.ArgumentParser(description='操作正在运行的 WinToolbox；输出 UTF-8 JSON，复用桌面校验与同步。')
    root.add_argument('--data-dir', type=Path, default=default_data_dir(), help='工具箱 data 目录；便携入口自动指定')
    root.add_argument('--timeout', type=float, default=30, help='单次 RPC 超时秒数')
    root.add_argument('--wait-timeout', type=float, default=120, help='等待后台任务的最长秒数')
    commands = root.add_subparsers(dest='command', required=True)
    commands.add_parser('status', help='查看版本、数据目录及连接状态')
    methods = commands.add_parser('methods', help='列出可调用的后台 RPC')
    methods.add_argument('--prefix', default='')
    call = commands.add_parser('call', help='调用任意公开后台 RPC')
    call.add_argument('method')
    inputs = call.add_mutually_exclusive_group()
    inputs.add_argument('--params', default='{}', help='JSON 参数；复杂内容建议 --input')
    inputs.add_argument('--input', help='UTF-8 JSON 文件路径，- 表示标准输入')
    call.add_argument('--wait', action='store_true', help='若返回后台任务，等待并返回最终任务记录')
    jobs = commands.add_parser('jobs', help='查询、等待及取消后台任务').add_subparsers(dest='action', required=True)
    jobs.add_parser('list')
    for action in ('get', 'wait', 'cancel'):
        jobs.add_parser(action).add_argument('id')
    expenses = commands.add_parser('expenses', help='报销项目、账目和带附件的原子入账').add_subparsers(dest='action', required=True)
    expenses.add_parser('projects')
    listing = expenses.add_parser('list')
    for name in ('month', 'start-date', 'end-date', 'project-id', 'query', 'currency', 'status'):
        listing.add_argument('--' + name)
    expenses.add_parser('get').add_argument('id')
    save = expenses.add_parser('save', help='JSON 参数调用 expenses.commit，默认等待完成')
    save.add_argument('--input', required=True)
    save.add_argument('--request-id', help='稳定 UUID，重试同一笔时复用；也可放入输入 JSON')
    save.add_argument('--no-wait', action='store_true')
    quota = commands.add_parser('quota', help='AI 额度账户及查询').add_subparsers(dest='action', required=True)
    quota.add_parser('list')
    quota.add_parser('catalogue')
    refresh = quota.add_parser('refresh')
    refresh.add_argument('--ids', nargs='+')
    refresh.add_argument('--no-wait', action='store_true')
    ai = commands.add_parser('ai', help='并行调用额度账户绑定的开发模型').add_subparsers(dest='action', required=True)
    ai.add_parser('list')
    submit = ai.add_parser('submit')
    submit.add_argument('--input', required=True, help='批量任务 JSON 文件或 -')
    submit.add_argument('--no-wait', action='store_true')
    ai.add_parser('get').add_argument('request_id')
    ai.add_parser('models').add_argument('account_id')
    return root


def run(args, client):
    wait = False
    if args.command == 'status':
        method, params = 'app.info', {}
    elif args.command == 'methods':
        method, params = 'cli.methods', {'prefix': args.prefix}
    elif args.command == 'call':
        method, params, wait = args.method, object_input(args), args.wait
    elif args.command == 'jobs':
        method = 'jobs.get' if args.action == 'wait' else 'jobs.' + args.action
        params, wait = ({'id': args.id} if args.action != 'list' else {}), args.action == 'wait'
    elif args.command == 'expenses':
        method, params = 'expenses.' + args.action, {}
        if args.action == 'projects':
            method = 'expenses.projects.list'
        elif args.action == 'list':
            params = {key: getattr(args, key) for key in ('month', 'start_date', 'end_date', 'project_id', 'query', 'currency', 'status')
                      if getattr(args, key) is not None}
        elif args.action == 'get':
            params = {'id': args.id}
        elif args.action == 'save':
            method, params, wait = 'expenses.commit', object_input(args), not args.no_wait
            params['request_id'] = args.request_id or params.get('request_id') or str(uuid.uuid4())
            # Publish before sending so an interrupted caller can recover safely.
            print('request_id=' + params['request_id'], file=sys.stderr, flush=True)
    elif args.command == 'quota':
        method, params = 'ai_quota.' + args.action, {}
        if args.action == 'refresh':
            params, wait = ({'ids': args.ids} if args.ids else {}), not args.no_wait
    elif args.command == 'ai':
        method, params = 'ai_calls.' + args.action, {}
        if args.action == 'submit':
            params, wait = object_input(args), not args.no_wait
        elif args.action == 'get':
            params = {'request_id': args.request_id}
        elif args.action == 'models':
            params, wait = {'account_id': args.account_id}, True
    result = client.call(method, params)
    if wait and isinstance(result, dict) and all(key in result for key in ('id', 'tool', 'status')):
        result = wait_job(client, result, args.wait_timeout)
    return result


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    args = parser().parse_args(argv)
    try:
        if not all(math.isfinite(v) and v > 0 for v in (args.timeout, args.wait_timeout)):
            raise CliError('超时秒数必须大于零。')
        result = run(args, Client(args.data_dir, args.timeout))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2, default=str))
        return 0
    except (CliError, ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
