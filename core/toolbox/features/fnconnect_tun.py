import contextlib
import json
import ipaddress
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse


def tools_dir():
    root=Path(os.getenv('WINTOOLBOX_TOOLS',str(Path(__file__).resolve().parents[3]/'.build')))
    return root/'fnconnect'


def tun_config(origin, scope, networks=None, transport_ips=None):
    networks=networks or ["192.168.100.0/24"]
    cidrs=[ipaddress.IPv4Network(c) for c in networks]
    routes=[str(s) for net in cidrs for s in (net.subnets(prefixlen_diff=1) if net.prefixlen<32 else [net])]
    host=urlparse(origin).hostname
    addresses=set(transport_ips or [row[4][0] for row in socket.getaddrinfo(host,443,family=socket.AF_INET,type=socket.SOCK_STREAM)])
    rules=[f'{"IP-CIDR6" if ":" in ip else "IP-CIDR"},{ip}/{128 if ":" in ip else 32},DIRECT,no-resolve' for ip in sorted(addresses)]
    rules.append(f'DOMAIN,{host},DIRECT')
    if scope=='lan':
        for cidr in cidrs: rules += [f'AND,((IP-CIDR,{cidr}),(NETWORK,UDP)),REJECT',f'IP-CIDR,{cidr},HOME,no-resolve']
        rules.append('MATCH,DIRECT')
    else:
        rules += ['NETWORK,udp,REJECT','IP-CIDR6,::/0,REJECT,no-resolve','MATCH,HOME']
    return {'mixed-port':0,'allow-lan':False,'mode':'rule','log-level':'warning','ipv6':scope=='all',
      'tun':{'enable':True,'stack':'mixed','device':'LanBridge','inet6-address':['fdfe:dcba:9876::1/126'] if scope=='all' else [],'auto-route':True,'auto-detect-interface':True,'strict-route':True,'dns-hijack':['any:53','tcp://any:53'],'route-address':routes+(['0.0.0.0/1','128.0.0.0/1','::/1','8000::/1'] if scope=='all' else []),'mtu':1400},
      'hosts':{host:sorted(addresses)},
      'dns':{'fake-ip-filter':[host],'enable':True,'listen':'127.0.0.1:18795','ipv6':False,'enhanced-mode':'fake-ip','fake-ip-range':'198.18.0.1/16','default-nameserver':['1.1.1.1'],'nameserver':['tcp://192.168.100.1:53#HOME']},
      'proxies':[{'name':'HOME','type':'socks5','server':'127.0.0.1','port':18792,'udp':False}],'rules':rules}


class TunController:
    def __init__(self,data_dir):
        from ..settings import atomic_json
        import secrets
        self.preference=Path(data_dir)/'fnconnect-tun.json'
        self.session=secrets.token_hex(16)
        self.lock=threading.RLock()
        self.event=None
        self.launcher=None
        self.phase='stopped'
        self.error=''
        self.service=False
        try: self.enabled=json.loads(self.preference.read_text('utf-8')).get('enabled',False) is True
        except (OSError,ValueError): self.enabled=False

    def remember(self,enabled):
        from ..settings import atomic_json
        with self.lock:
            atomic_json(self.preference,{'enabled':enabled})
            self.enabled=enabled

    def status(self):
        from .fnconnect_service import request
        try:
            state=request('status');self.service=True
            if self.phase not in ('awaiting-admin','starting'):
                self.phase=state['phase'];self.error=state.get('error','')
        except (OSError,ValueError,RuntimeError):
            self.service=False
            if self.phase=='running': self.phase='error';self.error='TUN 辅助服务已断开'
        return {'phase':self.phase,'error':self.error,'service':self.service,'enabled':self.enabled,
                'available':os.name=='nt' and (tools_dir()/'WinToolboxTun.exe').is_file()}

    def start(self,origin,scope,networks=None,transport_ips=None):
        from .fnconnect_service import request,install
        with self.lock:
            if not self.status()['available']: raise ValueError('本机未安装 TUN 核心，请更新免安装版')
            if self.event and not self.event.is_set(): raise ValueError('TUN 已启动或正在等待授权')
            self.remember(True)
            event=threading.Event();self.event=event;self.error=''
        try:
            if not self.service:
                self.phase='awaiting-admin'
                self.launcher=install(tools_dir())
                started=time.monotonic()
                while self.launcher.poll() is None:
                    if event.wait(.2): return self.status()
                    if time.monotonic()-started>300: raise ValueError('管理员授权超时，请重试安装 TUN 服务')
                if self.launcher.returncode: raise ValueError('TUN 服务安装未完成，请重试并确认首次管理员授权')
                for _ in range(30):
                    try: request('status');self.service=True;break
                    except OSError: time.sleep(.2)
                if not self.service: raise ValueError('TUN 服务未能启动，请重新安装')
            with self.lock:
                if event.is_set(): return self.status()
                self.phase='starting'
                addresses=transport_ips or list({row[4][0] for row in socket.getaddrinfo(urlparse(origin).hostname,443,family=socket.AF_INET,type=socket.SOCK_STREAM)})
                state=request('start',self.session,host=urlparse(origin).hostname,scope=scope,networks=networks or ['192.168.100.0/24'],transport_ips=addresses)
                self.phase=state['phase'];self.error=state.get('error','')
            def refresh():
                while not event.wait(2):
                    try: request('heartbeat',self.session)
                    except (OSError,ValueError,RuntimeError) as exc:
                        self.error=str(exc);self.phase='error';event.set();return
            threading.Thread(target=refresh,daemon=True).start()
            return self.status()
        except BaseException as exc:
            event.set();self.phase='error';self.error=str(exc)
            raise

    def stop(self,disable=False):
        from .fnconnect_service import request
        with self.lock:
            if disable: self.remember(False)
            if self.event: self.event.set()
            try: request('stop',self.session)
            except (OSError,ValueError,RuntimeError): pass
            self.phase='stopped';self.error=''
        return self.status()
