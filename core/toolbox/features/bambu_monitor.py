"""Read printer reports through local TLS MQTT or the installed Studio cloud SDK."""
import copy
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import threading
import time


def private_host(host):
    address = ipaddress.ip_address(host)
    if address.version != 4 or not (address.is_private or address in ipaddress.ip_network('100.64.0.0/10')) or address.is_loopback or address.is_link_local or address.is_unspecified:
        raise ValueError('打印机地址必须是局域网 IPv4')
    return str(address)


def discover(serial='', timeout=3):
    """Bounded SSDP search on each local LAN; discovery is not authentication."""
    query = b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1990\r\nMAN: "ssdp:discover"\r\nMX: 1\r\nST: urn:bambulab-com:device:3dprinter:1\r\n\r\n'
    sockets, found = [], []
    try:
        addresses = socket.gethostbyname_ex(socket.gethostname())[2]
        for address in addresses:
            try:
                private_host(address)
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                sock.bind((address, 0))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
                sock.settimeout(.15)
                sockets.append(sock)
                for port in (1990, 2021):
                    sock.sendto(query, ('239.255.255.250', port))
            except OSError:
                continue
        deadline = time.monotonic() + timeout
        while sockets and time.monotonic() < deadline:
            for sock in sockets:
                try:
                    raw, addr = sock.recvfrom(4096)
                    text = raw.decode('utf-8', 'replace')
                    if 'bambu' in text.lower() and (not serial or serial in text):
                        host = private_host(addr[0])
                        if host not in found:
                            found.append(host)
                except (socket.timeout, OSError, ValueError):
                    continue
        return found
    finally:
        for sock in sockets:
            sock.close()


def studio_printer(path=None, *, require_access=True):
    path = Path(path) if path else Path(os.environ.get('APPDATA', '')) / 'BambuStudio/BambuStudio.conf'
    # Studio appends a checksum after the JSON object. Do not modify its file.
    data, _ = json.JSONDecoder().raw_decode(path.read_text('utf-8-sig'))
    serial = data.get('app', {}).get('user_last_selected_machine', '')
    codes = data.get('access_code', {})
    if not serial or (require_access and serial not in codes):
        if len(codes) != 1:
            raise ValueError('请先在 Bambu Studio 选择要连接的打印机')
        serial = next(iter(codes))
    code = str(codes.get(serial, ''))
    if require_access and not re.fullmatch(r'[A-Za-z0-9]{8}', code):
        raise ValueError('Bambu Studio 未保存有效访问码，请在打印机网络设置查看')
    machine = str(data.get('presets', {}).get('machine', ''))
    name = 'Bambu P2S' if 'P2S' in machine else 'Bambu'
    result = {'printer_serial': serial, 'printer_name': name}
    if require_access:
        result['printer_access_code'] = code
    return result


def finite(value, low=0, high=100000):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (ValueError, TypeError):
        return None
    return value if math.isfinite(value) and low <= value <= high else None


def report(data):
    states = {'RUNNING': 'running', 'PREPARE': 'preparing', 'PAUSE': 'paused', 'FINISH': 'done',
              'FAILED': 'error', 'IDLE': 'idle', 'SLICING': 'preparing', 'UNKNOWN': 'unknown'}
    return {'state': states.get(data.get('gcode_state'), 'unknown'),
            'title': str(data.get('subtask_name') or data.get('gcode_file') or '').replace('\\', '/').split('/')[-1][:100],
            'progress': finite(data.get('mc_percent'), high=100),
            'remaining_minutes': finite(data.get('mc_remaining_time')),
            'layer': finite(data.get('layer_num')), 'total_layers': finite(data.get('total_layer_num')),
            'nozzle': finite(data.get('nozzle_temper'), high=500), 'bed': finite(data.get('bed_temper'), high=200),
            'error_code': str(data.get('print_error') or data.get('mc_print_error_code') or '0')[:24]}


class Printer:
    def __init__(self, data_dir=None):
        self.lock = threading.RLock()
        self.client = None
        self.key = None
        self.value = {'available': False, 'connected': False, 'configured': False}
        self.raw = {}
        self.last_request = 0
        self.data_dir = Path(data_dir) if data_dir is not None else None
        self.cloud = None
        self.last_report = 0
        self.cloud_started = 0

    def configure(self, config, decrypt):
        key = tuple(config.get(k) for k in ('printer_enabled', 'printer_connection', 'printer_host', 'printer_serial', 'printer_access_protected'))
        if key == self.key:
            return
        self.close()
        self.key = key
        with self.lock:
            self.value = {'available': False, 'connected': False, 'configured': bool(config.get('printer_serial'))}
            self.raw = {}
        if not config['printer_enabled']:
            return
        if config.get('printer_connection') == 'cloud':
            if self.data_dir is None:
                raise ValueError('未配置云端状态缓存目录')
            from .bambu_cloud import CloudMonitor, ERRORS
            def event(value):
                with self.lock:
                    if self.cloud is not monitor:
                        return
                    kind = value['event']
                    if kind == 'report':
                        self.last_report = time.time()
                        self.raw.update(value['data'])
                        self.value = {**report(self.raw), 'available': bool(self.raw.get('gcode_state')),
                                      'connected': True, 'configured': True, 'source': 'cloud', 'sampled_at': self.last_report}
                    elif kind == 'server':
                        connected = value.get('code') == 0
                        self.value.update(connected=connected, source='cloud',
                            error=None if connected else ('Bambu Studio 登录已过期，请重新登录后重新导入' if value.get('code') == 5 else '云端设备服务暂未连接，请检查网络'))
                        if not connected:
                            self.value['available'] = False
                    elif kind == 'error':
                        self.value.update(connected=False, available=False, source='cloud',
                                          error=ERRORS.get(value.get('error'), value.get('message') or '云端状态连接已断开，请重新导入或重启工具箱'))
                    elif kind == 'authenticated':
                        self.value.update(source='cloud', error=None)
            monitor = CloudMonitor(self.data_dir, event)
            self.cloud = monitor
            self.last_report = 0
            self.cloud_started = time.time()
            with self.lock:
                self.value['source'] = 'cloud'
            monitor.start(config['printer_serial'])
            return
        import paho.mqtt.client as mqtt
        host = private_host(config['printer_host'])
        context = ssl.create_default_context()
        context.check_hostname = False  # Printer certificates identify a device, not its changing LAN IP.
        for path in (Path(__file__).parent / 'bambu_certs').glob('*.cert'):
            context.load_verify_locations(cafile=str(path))
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='wintoolbox-' + os.urandom(6).hex())
        client.tls_set_context(context)
        client.username_pw_set('bblp', decrypt(config['printer_access_protected']))
        client.connect_timeout = 4
        client.reconnect_delay_set(5, 60)
        serial = config['printer_serial']

        def on_connect(c, _u, _f, reason, _p):
            with self.lock:
                self.value['connected'] = reason == 0
                self.value['error'] = None if reason == 0 else '打印机认证失败，请检查访问码'
            if reason == 0:
                c.subscribe(f'device/{serial}/report')
                self.request()

        def on_disconnect(_c, _u, _f, _reason, _p):
            with self.lock:
                self.value['connected'] = False

        def on_connect_fail(_c, _u):
            with self.lock:
                self.value.update(connected=False, error=f'无法连接 {host}:8883，请确认打印机开机、IP 正确，且电脑可通过局域网或 VPN 访问该端口；填写 IP 不会自动建立网络连接')

        def on_message(_c, _u, message):
            if message.topic != f'device/{serial}/report' or len(message.payload) > 131072:
                return
            try:
                data = json.loads(message.payload).get('print')
                if not isinstance(data, dict):
                    return
                with self.lock:
                    self.raw.update(data)
                    self.value = {**report(self.raw), 'available': True, 'connected': True,
                                  'configured': True, 'sampled_at': time.time()}
            except (ValueError, TypeError, AttributeError):
                return

        client.on_connect, client.on_disconnect, client.on_message = on_connect, on_disconnect, on_message
        client.on_connect_fail = on_connect_fail
        self.client = client
        self.serial = serial
        client.connect_async(host, 8883, 30)
        client.loop_start()

    def request(self):
        if self.client:
            # Read-only status request. Deliberately no pause/start/stop or camera commands.
            self.client.publish(f'device/{self.serial}/request', json.dumps({'pushing': {'sequence_id': '0', 'command': 'pushall'}}))
            self.last_request = time.monotonic()

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.value)
        if self.cloud:
            if time.time() - (self.last_report or self.cloud_started) > 120:
                result.update(available=False, error='打印机云端状态超过2分钟未更新，请检查打印机是否在线')
        elif result.get('connected') and time.monotonic() - self.last_request > 60:
            self.request()
        return result

    def close(self):
        cloud, self.cloud = self.cloud, None
        if cloud:
            cloud.close()
        client, self.client = self.client, None
        if client:
            client.disconnect()
            client.loop_stop()
