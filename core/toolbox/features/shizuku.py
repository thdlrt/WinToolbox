"""Pair wireless ADB and start only the explicitly selected phone's Shizuku."""
import contextlib
import ipaddress
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from ..jobs import Cancelled


PACKAGE = 'moe.shizuku.privileged.api'
START_SCRIPT = '/sdcard/Android/data/moe.shizuku.privileged.api/start.sh'
PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network(value) for value in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
POLL_SECONDS = .5
POLL_ATTEMPTS = 16


def target_address(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9.]+:[0-9]{1,5}', value, re.ASCII):
        raise ValueError('请填写手机私网 IPv4 地址和端口，例如 192.168.1.10:37123')
    host, port = value.rsplit(':', 1)
    try:
        ip = ipaddress.IPv4Address(host)
        number = int(port)
        if not any(ip in network for network in PRIVATE_NETWORKS) or ip.packed[-1] in (0, 255) or not 1 <= number <= 65535:
            raise ValueError()
    except ValueError:
        raise ValueError('仅支持私网手机单播 IPv4 地址，端口范围为 1 到 65535') from None
    return f'{ip}:{number}'


def adb_path():
    name = 'adb.exe' if os.name == 'nt' else 'adb'
    explicit = os.getenv('WINTOOLBOX_ADB')
    tools = os.getenv('WINTOOLBOX_TOOLS')
    candidates = [Path(explicit)] if explicit else []
    if tools:
        candidates.append(Path(tools) / 'adb' / name)
    # Source checkout and packaged core/toolbox/features share this relative root.
    candidates.append(Path(__file__).resolve().parents[3] / 'tools' / 'adb' / name)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    return shutil.which('adb')


def redact(value, secret=None):
    result = value.replace(secret, '[验证码已隐藏]') if secret else value
    if secret:
        result = re.sub(r'(?<!\d)\d{6}(?!\d)', '[验证码已隐藏]', result)
    return result


def run_adb(job, arguments, *, input_text=None, timeout=20):
    """Bounded in-memory output, hidden client, stdin secret, cancellable lifetime.

    Do not use Job.run_process: its persistent stdout log may capture pairing
    prompts. Stop only this client process, never the shared adb server.
    """
    binary = adb_path()
    if not binary:
        raise RuntimeError('未找到 ADB，请更新包含 Android 平台工具的 WinToolbox')
    job.check_cancelled()
    environment = dict(os.environ)
    for name in ('ADB_SERVER_SOCKET', 'ANDROID_ADB_SERVER_PORT', 'ANDROID_SERIAL', 'ADB_TRACE', 'ADB_LOG_PATH'):
        environment.pop(name, None)
    process = None
    reader = None
    output = deque(maxlen=32)  # At most 64 KiB, never a persistent command log.
    try:
        process = subprocess.Popen([binary, *arguments], stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        def drain():
            try:
                while block := process.stdout.read(2048):
                    output.append(block)
            except (OSError, ValueError):
                pass
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        if input_text is not None:
            process.stdin.write((input_text + '\n').encode('utf-8'))
            process.stdin.flush()
            process.stdin.close()
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            job.check_cancelled()
            if time.monotonic() >= deadline:
                raise TimeoutError('ADB 操作超时，请检查手机无线调试是否开启、端口是否仍有效')
            job.cancel_event.wait(.05)
        job.check_cancelled()
        reader.join(timeout=1)
        return process.returncode, redact(b''.join(output).decode('utf-8', errors='replace').strip(), input_text)
    except TimeoutError:
        raise
    except OSError:
        raise RuntimeError('无法运行 ADB，请检查 Android 平台工具文件是否完整') from None
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=3)
            if reader:
                reader.join(timeout=1)
            for stream in (process.stdin, process.stdout):
                if stream:
                    with contextlib.suppress(OSError):
                        stream.close()


def parse_discovery(mdns, attached):
    discovered = {}
    services = {}
    for line in mdns.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        name, service, address = fields[:3]
        if service.rstrip('.') not in ('_adb-tls-connect._tcp', '_adb-tls-pairing._tcp'):
            continue
        try:
            address = target_address(address)
        except ValueError:
            continue
        kind = 'pairing' if 'pairing' in service else 'connect'
        row = {'address': address, 'service': service, 'label': name[:180], 'kind': kind}
        discovered[(address, kind)] = row
        services[(name + '.' + service).rstrip('.')] = row
    for line in attached.splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[1] not in ('device', 'offline', 'unauthorized', 'recovery', 'sideload'):
            continue
        serial, state = fields[:2]
        try:
            address = target_address(serial)
        except ValueError:
            existing = services.get(serial.rstrip('.'))
            if existing and existing['kind'] == 'connect':
                existing['state'] = state
            continue
        label = next((part[6:].replace('_', ' ') for part in fields[2:] if part.startswith('model:')), address)
        existing = discovered.setdefault((address, 'connect'), {'address': address, 'label': label[:180], 'kind': 'connect'})
        existing['state'] = state
        if label != address:
            existing['label'] = label[:180]
    return sorted(discovered.values(), key=lambda row: (row['kind'], row['address']))


def register(app):
    secrets = {}
    secret_lock = threading.Lock()
    operation_lock = threading.Lock()

    def status(_):
        path = adb_path()
        result = {'adb_available': bool(path)}
        if path:
            result['adb_path'] = path
        previous = app.settings.get().get('preferences', {}).get('shizuku_target')
        with contextlib.suppress(ValueError):
            if previous:
                result['last_target'] = target_address(previous)
        return result

    def discover(job):
        job.progress(10, '正在读取局域网无线调试广播')
        mdns_code, mdns = run_adb(job, ['mdns', 'services'], timeout=12)
        job.progress(55, '正在读取已连接的 ADB 设备')
        devices_code, devices = run_adb(job, ['devices', '-l'], timeout=12)
        if mdns_code and devices_code:
            raise RuntimeError('无法读取 ADB 设备列表，请检查平台工具和本机 ADB 服务')
        rows = parse_discovery(mdns if not mdns_code else '', devices if not devices_code else '')
        result = {'devices': rows}
        if mdns_code:
            result['notice'] = 'mDNS 发现不可用，可手动填写手机无线调试页面的 IP 地址和连接端口'
        elif not rows:
            result['notice'] = '未发现手机，请确认电脑与手机在同一局域网，并在手机开启无线调试'
        return result

    def start_target(job, target):
        target = target_address(target)
        job.progress(40, '正在连接手机无线调试')
        code, output = run_adb(job, ['connect', target], timeout=20)
        if code or not re.search(r'\b(?:connected|already connected) to\b', output, re.I):
            raise RuntimeError('连接失败，请核对无线调试连接端口；配对端口不能用于启动。' + (' ' + output[-1800:] if output else ''))
        code, output = run_adb(job, ['-s', target, 'get-state'], timeout=10)
        if code or output.strip() != 'device':
            raise RuntimeError('手机尚未允许 ADB 调试，请检查手机授权或重新配对')
        with app.data_lock:
            app.settings.update({'preferences': {'shizuku_target': target}})
        job.progress(55, '正在检查手机上的 Shizuku')
        code, output = run_adb(job, ['-s', target, 'shell', 'pm', 'path', PACKAGE], timeout=10)
        if code or not any(line.strip().startswith('package:') for line in output.splitlines()):
            raise RuntimeError('手机未安装 Shizuku，请先安装并打开 Shizuku 应用')
        code, output = run_adb(job, ['-s', target, 'shell', 'pidof', 'shizuku_server'], timeout=10)
        if not code and re.fullmatch(r'[1-9]\d*(?:\s+[1-9]\d*)*', output):
            job.mark_committed()
            return {'target': target, 'running': True, 'message': 'Shizuku 已在运行'}
        job.progress(70, '正在执行 Shizuku 官方启动脚本')
        code, output = run_adb(job, ['-s', target, 'shell', 'sh', START_SCRIPT], timeout=25)
        if code:
            raise RuntimeError('Shizuku 启动脚本失败，请先在手机打开 Shizuku 应用。' + (' ' + output[-1800:] if output else ''))
        for index in range(POLL_ATTEMPTS):
            job.check_cancelled()
            code, output = run_adb(job, ['-s', target, 'shell', 'pidof', 'shizuku_server'], timeout=5)
            if not code and re.fullmatch(r'[1-9]\d*(?:\s+[1-9]\d*)*', output):
                job.mark_committed()
                return {'target': target, 'running': True, 'message': 'Shizuku 已启动，已确认服务进程运行'}
            job.progress(75 + index / POLL_ATTEMPTS * 20, '正在确认 Shizuku 服务进程')
            job.cancel_event.wait(POLL_SECONDS)
        return {'target': target, 'running': False, 'message': '启动命令已执行，但未检测到 Shizuku 服务进程。请查看手机 Shizuku 状态及启动日志'}

    def start(job):
        target = target_address(job.params.get('target'))
        if not operation_lock.acquire(blocking=False):
            raise ValueError('已有手机配对或启动任务正在运行，请等待完成')
        try:
            return start_target(job, target)
        finally:
            operation_lock.release()

    def pair(job):
        pair_target = target_address(job.params.get('pair_target'))
        explicit = target_address(job.params['target']) if job.params.get('target') else None
        if explicit and (explicit.split(':')[0] != pair_target.split(':')[0] or explicit == pair_target):
            raise ValueError('连接地址必须是同一手机 IP，且使用不同于配对端口的连接端口')
        with secret_lock:
            saved = secrets.pop(job.params.get('credential_token'), None)
        if not saved or time.monotonic() - saved[1] > 300:
            raise ValueError('配对码不保存在任务记录中，请重新输入手机显示的配对码')
        if not operation_lock.acquire(blocking=False):
            raise ValueError('已有手机配对或启动任务正在运行，请等待完成')
        code = saved[0]
        try:
            job.progress(10, '正在通过手机配对码建立 ADB 信任')
            result_code, output = run_adb(job, ['pair', pair_target], input_text=code, timeout=35)
            if result_code or 'successfully paired' not in output.casefold():
                raise ValueError('配对失败，请保持手机配对窗口开启，核对配对端口及六位配对码。' + (' ' + redact(output, code)[-1800:] if output else ''))
            target = explicit
            try:
                if target is None:
                    mdns_code, mdns = run_adb(job, ['mdns', 'services'], timeout=12)
                    matches = {row['address'] for row in parse_discovery(mdns, '') if row['kind'] == 'connect'
                               and row['address'].split(':')[0] == pair_target.split(':')[0] and row['address'] != pair_target} if not mdns_code else set()
                    if len(matches) == 1:
                        target = next(iter(matches))
                if target is None:
                    job.mark_committed()
                    return {'paired': True, 'running': False, 'message': '配对成功。请返回手机“无线调试”主页，填写该手机的连接端口后点击启动'}
                return {'paired': True, **start_target(job, target)}
            except Cancelled:
                raise
            except Exception as exc:
                job.mark_committed()
                return {'paired': True, **({'target': target} if target else {}), 'running': False, 'message': '配对成功，但尚未启动：' + redact(str(exc), code)[-1800:]}
        finally:
            operation_lock.release()

    def submit_pair(params):
        pair_target = target_address(params.get('pair_target'))
        code = params.get('code')
        if not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code, re.ASCII):
            raise ValueError('请输入手机显示的六位数字配对码')
        target = target_address(params['target']) if params.get('target') else None
        if target and (target.split(':')[0] != pair_target.split(':')[0] or target == pair_target):
            raise ValueError('连接地址必须是同一手机 IP，且使用不同于配对端口的连接端口')
        token = uuid.uuid4().hex
        with secret_lock:
            now = time.monotonic()
            for previous in list(secrets):
                if now - secrets[previous][1] > 300:
                    secrets.pop(previous)
            if len(secrets) >= 32:
                raise ValueError('待配对任务过多，请稍后重试')
            secrets[token] = (code, now)
        try:
            return app.jobs.submit('shizuku.pair', {'pair_target': pair_target, 'credential_token': token, **({'target': target} if target else {})})
        except BaseException:
            with secret_lock:
                secrets.pop(token, None)
            raise

    app.jobs.register('shizuku.discover', discover)
    app.jobs.register('shizuku.start', start)
    app.jobs.register('shizuku.pair', pair)
    app.register('shizuku.status', status)
    app.register('shizuku.discover', lambda _: app.jobs.submit('shizuku.discover', {}))
    app.register('shizuku.start', lambda p: app.jobs.submit('shizuku.start', {'target': target_address(p.get('target'))}))
    app.register('shizuku.pair', submit_pair)
