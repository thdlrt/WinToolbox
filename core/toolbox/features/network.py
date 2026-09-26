"""Bounded, cancellable DNS/TCP/TLS/HTTP diagnostics; only profiles synchronize."""
import contextlib
import datetime
import hashlib
import http.client
import ipaddress
import json
import queue
import socket
import ssl
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

from ..settings import atomic_json


def target_options(params):
    target = params.get('target', '')
    if not isinstance(target, str) or not 1 <= len(target.strip()) <= 2048:
        raise ValueError('请填写域名、IP 或 HTTP/HTTPS 地址')
    target = target.strip()
    if any(ord(c) < 33 for c in target) or '\\' in target:
        raise ValueError('目标地址不能包含空白或控制字符')
    scheme, path = '', '/'
    if '://' in target:
        p = urlsplit(target)
        if p.scheme not in ('http', 'https') or not p.hostname or p.username is not None or p.password is not None or p.query or p.fragment:
            raise ValueError('仅支持 HTTP/HTTPS，地址中不要包含账号、密码、查询参数或片段')
        host, scheme, path = p.hostname, p.scheme, quote(p.path or '/', safe='/%:@!$&\'()*+,;=-._~')
        url_port = p.port
    else:
        host, url_port = target, None
        if host.startswith('[') and host.endswith(']'):
            host = host[1:-1]
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if any(c in host for c in '/:@?#[]'):
                raise ValueError('域名和端口请分别填写，或填写完整 HTTP/HTTPS 地址')
    try:
        host = host.encode('idna').decode('ascii')
    except UnicodeError:
        raise ValueError('域名格式无效') from None
    port = params.get('port')
    if port in (None, ''):
        port = url_port or (80 if scheme == 'http' else 443)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('端口需为 1 至 65535 的整数')
    timeout_ms, attempts = params.get('timeout_ms', 5000), params.get('attempts', 3)
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 1000 <= timeout_ms <= 15000:
        raise ValueError('超时需为 1000 至 15000 毫秒')
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 5:
        raise ValueError('连接次数需为 1 至 5 次')
    return dict(target=target, host=host, port=port, scheme=scheme, path=path, timeout_ms=timeout_ms, attempts=attempts)


def resolve(host, port, timeout, check):
    # OS DNS does not honor socket timeouts. A daemon worker bounds the job wait.
    result = queue.Queue(maxsize=1)
    def work():
        try:
            result.put((socket.getaddrinfo(host, port, type=socket.SOCK_STREAM), None))
        except OSError as exc:
            result.put((None, exc))
    threading.Thread(target=work, name='network-dns', daemon=True).start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        check()
        try:
            values, error = result.get(timeout=min(.1, max(.001, deadline - time.monotonic())))
            if error:
                raise error
            unique = []
            for row in values:
                if row not in unique:
                    unique.append(row)
            if not unique:
                raise OSError('DNS 未返回地址')
            return unique[:8]
        except queue.Empty:
            pass
    raise TimeoutError('DNS 解析超时')


def connect(addresses, timeout, check):
    deadline = time.monotonic() + timeout
    last = TimeoutError('TCP 连接超时')
    for family, kind, proto, _, address in addresses:
        check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(remaining)
            sock.connect(address)
            sock.settimeout(timeout)
            return sock
        except OSError as exc:
            last = exc
            sock.close()
    raise last


def error_detail(exc):
    if isinstance(exc, ssl.SSLCertVerificationError):
        return 'TLS 证书验证失败：' + str(exc.verify_message)
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return '请求超时'
    if isinstance(exc, socket.gaierror):
        return 'DNS 无法解析此主机'
    if isinstance(exc, ConnectionRefusedError):
        return '目标拒绝连接，请检查服务和端口'
    return f'{type(exc).__name__}: {str(exc)[:300]}'


def diagnose(params, job):
    cfg = target_options(params)
    report = {key: cfg[key] for key in ('target', 'host', 'port')}
    report.update(id=uuid.uuid4().hex, profile_id=params.get('profile_id'),
                  started_at=datetime.datetime.now(datetime.timezone.utc).isoformat(), steps=[],
                  route='当前进程网络路径；不自动使用浏览器或系统 HTTP/SOCKS 代理')
    timeout = cfg['timeout_ms'] / 1000
    def add(name, status, start, detail, **extra):
        report['steps'].append(dict(name=name, status=status, duration_ms=round((time.monotonic()-start)*1000, 2), detail=detail, **extra))
    def skip(name, detail):
        add(name, 'skipped', time.monotonic(), detail)
    start = time.monotonic()
    job.progress(5, '正在解析域名')
    try:
        addresses = resolve(cfg['host'], cfg['port'], timeout, job.check_cancelled)
        ips = list(dict.fromkeys(row[4][0] for row in addresses))
        add('dns', 'ok', start, '解析成功', addresses=ips)
    except OSError as exc:
        add('dns', 'error', start, error_detail(exc))
        for name in ('tcp', 'tls', 'http'):
            skip(name, 'DNS 失败，未执行')
        report['summary'] = '域名解析失败；请检查域名、DNS 或当前网络。'
        return report
    samples, failures, start = [], [], time.monotonic()
    for i in range(cfg['attempts']):
        job.progress(15 + i * 35 / cfg['attempts'], f"正在测试 TCP 连接 {i+1}/{cfg['attempts']}")
        sample_start = time.monotonic()
        try:
            with connect(addresses, timeout, job.check_cancelled):
                samples.append(round((time.monotonic() - sample_start) * 1000, 2))
        except OSError as exc:
            failures.append(error_detail(exc))
    add('tcp', 'ok' if len(samples) == cfg['attempts'] else 'error', start,
        f"连接成功 {len(samples)}/{cfg['attempts']}" + ('；' + '；'.join(dict.fromkeys(failures)) if failures else ''),
        samples_ms=samples, success_count=len(samples), attempt_count=cfg['attempts'],
        average_ms=round(sum(samples)/len(samples), 2) if samples else None)
    if not samples:
        skip('tls', 'TCP 失败，未执行')
        skip('http', 'TCP 失败，未执行')
        report['summary'] = '域名可解析，但端口无法连接；请检查服务、路由或防火墙。'
        return report
    if not cfg['scheme']:
        skip('tls', '填写 HTTPS 地址可检查 TLS 证书')
        skip('http', '填写 HTTP/HTTPS 地址可检查响应状态')
    else:
        sock = None
        try:
            sock = connect(addresses, timeout, job.check_cancelled)
            if cfg['scheme'] == 'https':
                start = time.monotonic()
                job.progress(65, '正在验证 TLS 证书')
                try:
                    sock = ssl.create_default_context().wrap_socket(sock, server_hostname=cfg['host'])
                    cert = sock.getpeercert()
                    add('tls', 'ok', start, f"{sock.version()}；证书有效期至 {cert.get('notAfter', '未知')}")
                except OSError as exc:
                    add('tls', 'error', start, error_detail(exc))
                    skip('http', 'TLS 验证失败，未发送 HTTP 请求')
                    report['summary'] = '端口可连接，但 TLS 验证失败；请检查证书域名、有效期和设备时间。'
                    return report
            else:
                skip('tls', '当前为 HTTP 地址')
            job.check_cancelled()
            job.progress(80, '正在检查 HTTP 响应')
            start = time.monotonic()
            authority = ('[' + cfg['host'] + ']') if ':' in cfg['host'] else cfg['host']
            authority += ':' + str(cfg['port'])
            def head(connection, method):
                connection.sendall(f"{method} {cfg['path']} HTTP/1.1\r\nHost: {authority}\r\nUser-Agent: WinToolbox-Diagnostics/1\r\nConnection: close\r\n\r\n".encode('ascii'))
                response = http.client.HTTPResponse(connection, method=method)
                response.begin()
                code = response.status
                response.close()
                return code
            code = head(sock, 'HEAD')
            if code == 405:
                sock.close()
                sock = connect(addresses, timeout, job.check_cancelled)
                if cfg['scheme'] == 'https':
                    sock = ssl.create_default_context().wrap_socket(sock, server_hostname=cfg['host'])
                code = head(sock, 'GET')
            detail = f'HTTP {code}'
            if 300 <= code < 400:
                detail += '；服务可达，返回重定向，未跟随'
            elif code in (401, 403):
                detail += '；服务可达，需要认证或访问权限'
            elif code >= 400:
                detail += '；服务已响应，请检查路径或服务状态'
            add('http', 'ok' if code < 400 else 'error', start, detail, http_status=code)
        except (OSError, http.client.HTTPException) as exc:
            if not any(s['name'] == 'tls' for s in report['steps']):
                skip('tls', '建立 HTTP 连接失败')
            add('http', 'error', time.monotonic(), error_detail(exc))
        finally:
            if sock:
                sock.close()
    report['summary'] = '已完成；检查项正常。' if all(s['status'] != 'error' for s in report['steps']) else '已完成；请查看失败检查项。'
    return report


def register(app):
    history = app.data_dir / 'network' / 'reports'
    history.mkdir(parents=True, exist_ok=True)
    def profile_view(value):
        heads = value.get('heads', [])
        return {**value, 'revision': hashlib.sha256(json.dumps(sorted(heads)).encode()).hexdigest()}
    def profiles(_):
        return {'profiles': [profile_view(v) for v in app.ledger.list_entities('network_profile') if not v.get('deleted')]}
    def save(params):
        cfg = target_options(params)
        name = params.get('name', '').strip()
        if not name or len(name) > 100:
            raise ValueError('请输入 1 至 100 字的诊断名称')
        item_id = params.get('id') or uuid.uuid4().hex
        previous = app.ledger.get('network_profile', item_id) if params.get('id') else None
        if previous and params.get('revision') != profile_view(previous)['revision']:
            raise ValueError('诊断配置已更新，请刷新后再保存')
        values = {k: cfg[k] for k in ('target', 'port', 'timeout_ms', 'attempts')}
        values['name'] = name
        if previous:
            values = {k: v for k, v in values.items() if previous.get(k) != v}
        result = app.ledger.patch('network_profile', item_id, values, parents=previous['heads'] if previous else [])
        app.ledger_auto_sync()
        app.emit('network.changed')
        return profile_view(result)
    def delete(params):
        old = app.ledger.get('network_profile', params['id'])
        if not old or params.get('revision') != profile_view(old)['revision']:
            raise ValueError('诊断配置已更新，请刷新后再删除')
        app.ledger.patch('network_profile', old['id'], {}, parents=old['heads'], deleted=True)
        app.ledger_auto_sync()
        app.emit('network.changed')
        return {'ok': True}
    def run(job):
        report = diagnose(job.params, job)
        job.check_cancelled()
        atomic_json(history / (report['id'] + '.json'), report)
        paths = sorted(history.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in paths[20:]:
            path.unlink()
        job.artifact(history / (report['id'] + '.json'), label='网络诊断报告')
        return report
    def submit(params):
        if params.get('profile_id'):
            profile = app.ledger.get('network_profile', params['profile_id'])
            if not profile or profile.get('deleted'):
                raise ValueError('诊断配置不存在')
            params = {**profile, 'profile_id': profile['id']}
        cfg = target_options(params)
        return app.jobs.submit('network.run', {**{k: cfg[k] for k in ('target', 'port', 'timeout_ms', 'attempts')}, 'profile_id': params.get('profile_id')})
    def listing(_):
        result = []
        for path in sorted(history.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
            with contextlib.suppress(OSError, ValueError):
                result.append(json.loads(path.read_text('utf-8')))
        return {'reports': result}
    def export(params):
        item_id = params.get('id', '')
        if not isinstance(item_id, str) or len(item_id) != 32 or any(c not in '0123456789abcdef' for c in item_id):
            raise ValueError('报告 ID 无效')
        report = json.loads((history / (item_id + '.json')).read_text('utf-8'))
        destination = Path(params['path']).absolute()
        if destination.suffix.lower() != '.json':
            raise ValueError('请保存为 JSON 文件')
        atomic_json(destination, report)
        return {'path': str(destination)}
    app.register('network.profiles.list', profiles)
    app.register('network.profiles.save', save)
    app.register('network.profiles.delete', delete)
    app.register('network.run', submit)
    app.register('network.history', listing)
    app.register('network.export', export)
    app.jobs.register('network.run', run)
