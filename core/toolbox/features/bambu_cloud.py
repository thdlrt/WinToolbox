"""Read-only cloud reports through the owner's installed official Studio SDK."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading

PREFIX = 'WT_BAMBU_CLOUD '
REPORT_FIELDS = ('gcode_state', 'subtask_name', 'gcode_file', 'mc_percent', 'mc_remaining_time',
                 'layer_num', 'total_layer_num', 'nozzle_temper', 'bed_temper', 'print_error', 'mc_print_error_code')
ERRORS = {'sdk-load': '无法加载 Bambu Studio 网络组件，请安装或更新 Bambu Studio',
          'sdk-create': 'Bambu Studio 网络组件初始化失败', 'sdk-initialize': 'Bambu Studio 网络组件配置失败',
          'login-required': '请先在 Bambu Studio 登录，再点击从 Bambu Studio 导入',
          'sdk-call': 'Bambu Studio 网络组件不兼容，请更新 Bambu Studio 后重新导入'}


def studio_paths():
    root = Path(os.environ.get('APPDATA', '')) / 'BambuStudio'
    plugins = root / 'plugins'
    dll = plugins / 'bambu_networking.dll'
    if not dll.is_file():
        raise ValueError('请先在 Bambu Studio 安装网络插件并登录')
    config = root / 'BambuNetworkEngine.conf'
    if not config.is_file():
        raise ValueError('请先在 Bambu Studio 登录')
    certificates = Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Bambu Studio/resources/cert'
    if not (certificates / 'slicer_base64.cer').is_file():
        raise ValueError('未找到 Bambu Studio 云端证书，请修复 Bambu Studio 安装')
    data, _ = json.JSONDecoder().raw_decode((root / 'BambuStudio.conf').read_text('utf-8-sig'))
    region = data.get('app', {}).get('region', '')
    return dll, config, certificates, 'CN' if region in ('China', 'CN') else 'US'


def parse_event(line):
    if not line.startswith(PREFIX) or len(line) > 132000:
        return None
    try:
        value = json.loads(line[len(PREFIX):])
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    kind = value.get('event')
    if kind == 'report':
        payload = value.get('payload')
        data = payload.get('print') if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return None
        # Unknown SDK fields and any credentials stay out of application state.
        fields = {k: v for k, v in data.items() if k in REPORT_FIELDS and
                  (v is None or type(v) in (str, int, float))}
        for key in ('gcode_state', 'subtask_name', 'gcode_file'):
            if key in fields:
                if not isinstance(fields[key], str):
                    fields.pop(key)
                else:
                    fields[key] = fields[key][:512]
        return {'event': 'report', 'data': fields}
    if kind in ('authenticated', 'server', 'connect', 'request', 'error'):
        return {k: value[k] for k in ('event', 'code', 'reason', 'error') if k in value}
    return None


class CloudMonitor:
    def __init__(self, data_dir, on_event):
        self.data_dir = Path(data_dir)
        self.on_event = on_event
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.process = None
        self.thread = None

    def start(self, serial):
        self.thread = threading.Thread(target=self._run, args=(serial,), name='bambu-cloud-monitor', daemon=True)
        self.thread.start()

    def _run(self, serial):
        try:
            dll, original, certs, country = studio_paths()
            executable = Path(os.environ.get('WINTOOLBOX_TOOLS', str(Path(__file__).resolve().parents[3] / '.build'))) / 'bambu-cloud/bambu-cloud-monitor.exe'
            if not executable.is_file():
                raise ValueError('云端状态适配器尚未构建，请更新工具箱')
            session = self.data_dir / 'bambu-cloud/session'
            logs = self.data_dir / 'bambu-cloud/log'
            session.mkdir(parents=True, exist_ok=True)
            logs.mkdir(parents=True, exist_ok=True)
            # SDK may update its config; always use a private encrypted copy.
            shutil.copyfile(original, session / original.name)
            with self.lock:
                if self.stop.is_set():
                    return
                process = subprocess.Popen([str(executable), str(dll), str(session), str(certs), country, serial, str(logs)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, encoding='utf-8', errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self.process = process
            while not self.stop.is_set():
                line = process.stdout.readline(132001)
                if not line:
                    break
                if len(line) > 132000:
                    raise ValueError('云端状态响应超过限制')
                event = parse_event(line)
                if event:
                    self.on_event(event)
            if not self.stop.is_set():
                self.on_event({'event': 'error', 'error': 'cloud-disconnected'})
        except Exception as exc:
            if not self.stop.is_set():
                self.on_event({'event': 'error', 'error': 'adapter', 'message': str(exc) if isinstance(exc, ValueError) else '云端状态连接异常，请重新导入或重启工具箱'})
        finally:
            self._stop_process()

    def _stop_process(self):
        with self.lock:
            process, self.process = self.process, None
        if process is None:
            return
        try:
            if process.poll() is None:
                process.stdin.write('quit\n')
                process.stdin.flush()
                process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            if process.poll() is None:
                process.kill()  # Only this adapter's isolated child, never Studio.
                process.wait(timeout=3)
        finally:
            if process.stdin:
                process.stdin.close()
            if process.stdout:
                process.stdout.close()

    def close(self):
        self.stop.set()
        self._stop_process()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
