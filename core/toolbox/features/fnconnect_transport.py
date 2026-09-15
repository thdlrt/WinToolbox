"""FN Connect TCP client. Credentials and NAS session cookies remain in memory."""
import base64
import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
import re
import queue
import secrets
import socket
import socketserver
import threading
import time
from urllib.parse import urlparse

import httpx
import httpcore
import ssl
import websocket
from cryptography.hazmat.primitives import padding as sympadding, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PRIVATE = tuple(ipaddress.ip_network(s) for s in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'))


def nas_origin(value):
    u = urlparse(str(value))
    local = False
    try:
        ip = ipaddress.ip_address(u.hostname or '')
        local = any(ip in n for n in PRIVATE)
    except ValueError:
        pass
    if u.username or u.password or u.path not in ('', '/') or u.query or u.fragment:
        raise ValueError('请填写 NAS 根地址')
    if not (u.scheme == 'https' and re.fullmatch(r'[a-zA-Z0-9_-]+\.fnos\.net', u.hostname or '')) and not (local and u.scheme in ('http','https')):
        raise ValueError('仅接受设备专属 FN Connect HTTPS 域名或局域网 NAS 地址')
    return f'{u.scheme}://{u.netloc}'


class PinnedBackend(httpcore.SyncBackend):
    def __init__(self, host, addresses):
        self.host, self.addresses = host, addresses
    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        name=host.decode() if isinstance(host,bytes) else host
        choices=self.addresses if name==self.host else [host]
        error=None
        for address in choices:
            try:return super().connect_tcp(address,port,timeout,local_address,socket_options)
            except Exception as exc:error=exc
        raise error


class NasSession:
    def __init__(self, origin):
        self.origin = nas_origin(origin)
        u=urlparse(self.origin)
        self.host=u.hostname
        self.addresses=list(dict.fromkeys(r[4][0] for r in socket.getaddrinfo(self.host,u.port or 443,family=socket.AF_INET,type=socket.SOCK_STREAM)))
        transport=httpx.HTTPTransport(retries=1)
        transport._pool._network_backend=PinnedBackend(self.host,self.addresses)
        self.http = httpx.Client(transport=transport,timeout=15, follow_redirects=False, trust_env=False, headers={'Origin':self.origin})
        self.key = base64.b64encode(secrets.token_bytes(24))
        self.iv = secrets.token_bytes(16)
        self.secret = None
        self.ws = None
        self.lock = threading.RLock()
        self.closed = False
        self.tls_context=ssl.create_default_context()
        self.tls_session=None
        self.half_close=False
        self.auth_stop=threading.Event()
        self.auth_dead=False
        self.pending_id=None
        self.replies=queue.Queue(maxsize=8)

    def connect_ws(self,path):
        u=urlparse(self.origin)
        error=None
        for address in self.addresses:
            sock=None
            try:
                sock=socket.create_connection((address,u.port or (443 if u.scheme=='https' else 80)),timeout=15)
                if u.scheme=='https':sock=self.tls_context.wrap_socket(sock,server_hostname=self.host,session=self.tls_session)
                ws=websocket.create_connection(self.origin.replace('http','ws',1)+path,socket=sock,timeout=15,cookie=self.cookie(),origin=self.origin)
                if u.scheme=='https':self.tls_session=sock.session
                return ws
            except Exception as exc:
                error=exc
                if sock:
                    with contextlib.suppress(Exception):sock.close()
        raise error

    def cookie(self):
        req = self.http.build_request('GET', self.origin+'/app/lanbridge/tunnel/tcp')
        return req.headers.get('cookie','')

    def bootstrap(self):
        for _ in range(4):
            r = self.http.get(self.origin+'/', headers={'Referer':'https://fnos.net/'})
            if not r.is_redirect:
                break
            if urlparse(r.headers.get('location','')).netloc not in ('',urlparse(self.origin).netloc):
                raise ValueError('飞牛入口跳转异常，请使用设备专属 fnos.net 域名')
        if self.ws:
            self.auth_stop.set()
            with contextlib.suppress(Exception): self.ws.abort();self.ws.close(timeout=1)
        self.ws = self.connect_ws('/websocket?type=main')
        self.auth_stop=threading.Event();self.auth_dead=False;self.replies=queue.Queue(maxsize=8)
        threading.Thread(target=self._read_auth,args=(self.ws,self.replies,self.auth_stop),daemon=True).start()
        pub = self.call({'req':'util.crypto.getRSAPub'})
        self.si, self.pub = pub['si'], pub['pub']

    def _read_auth(self,ws,inbox,event):
        try:
            while not event.is_set():
                try: message=ws.recv()
                except websocket.WebSocketTimeoutException: continue
                if not message: raise ValueError('飞牛认证连接已关闭')
                try: value=json.loads(message)
                except (ValueError,TypeError): continue
                if value.get('reqid')!=self.pending_id or self.pending_id is None: continue
                try: inbox.put_nowait(value)
                except queue.Full: pass
        except Exception:
            if self.ws is ws: self.auth_dead=True
            with contextlib.suppress(queue.Full): inbox.put_nowait(ValueError('飞牛认证连接已关闭'))

    def call(self, body, encrypted=False):
        with self.lock:
            ident = secrets.token_hex(12)
            body = {**body,'reqid':ident}
            wire = json.dumps(body,separators=(',',':'),ensure_ascii=False).encode()
            if encrypted:
                pad = sympadding.PKCS7(128).padder()
                enc = Cipher(algorithms.AES(self.key),modes.CBC(self.iv)).encryptor()
                aes = enc.update(pad.update(wire)+pad.finalize())+enc.finalize()
                pub = serialization.load_pem_public_key(self.pub.encode())
                wire = json.dumps({'req':'encrypted','iv':base64.b64encode(self.iv).decode(),'rsa':base64.b64encode(pub.encrypt(self.key,padding.PKCS1v15())).decode(),'aes':base64.b64encode(aes).decode()})
            else:
                wire = wire.decode()
                if self.secret and not body['req'].startswith('util.crypto.'):
                    wire = base64.b64encode(hmac.new(self.secret,wire.encode(),hashlib.sha256).digest()).decode()+wire
            self.pending_id=ident
            try:
                self.ws.send(wire)
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    try: r=self.replies.get(timeout=max(.01,deadline-time.monotonic()))
                    except queue.Empty: break
                    if isinstance(r,Exception): raise r
                    if r.get('reqid')!=ident: continue
                    if r.get('result')=='fail': raise ValueError('飞牛认证失败，错误码 '+str(r.get('errno',r.get('code','未知'))))
                    return r
                raise ValueError('飞牛认证超时')
            finally: self.pending_id=None

    def login(self, username, password):
        self.bootstrap()
        r = self.call({'req':'user.login','user':username,'password':password,'stay':True,'deviceType':'PC','deviceName':'WinToolbox','did':secrets.token_hex(16),'ver':2,'si':self.si},True)
        if not r.get('ticket'): raise ValueError('当前试验版尚不支持双重验证，请使用普通飞牛管理员登录')
        if r.get('secret'):
            dec = Cipher(algorithms.AES(self.key),modes.CBC(self.iv)).decryptor()
            unpad = sympadding.PKCS7(128).unpadder()
            self.secret = unpad.update(dec.update(base64.b64decode(r['secret']))+dec.finalize())+unpad.finalize()
        self.ticket(r['ticket'])
        result = self.http.get(self.origin+'/app/lanbridge/api/tunnel')
        try: info=result.json()
        except ValueError: raise ValueError('NAS 局域网桥未升级或没有管理员权限') from None
        if info.get('protocol') != 'lanbridge-tcp-v1': raise ValueError('NAS 隧道版本不匹配')
        self.half_close=info.get('tcpHalfClose',False) is True
        self.policy=info.get('policy')
        if not isinstance(self.policy,dict) or not isinstance(self.policy.get('networks'),list) or not self.policy['networks']:
            raise ValueError('NAS 局域网桥没有返回有效的访问范围，请升级插件后重试')
        if not self.policy.get('enabled'): raise ValueError('NAS 已关闭客户端隧道，请在飞牛插件中启用')
        self.bootstrap()
        self.call({'req':'user.authToken','main':True,'si':self.si})

    def ticket(self, ticket):
        r=self.http.post(self.origin+'/app/ticket',json={'ticket':ticket})
        if r.status_code!=200: raise ValueError('飞牛登录票据交换失败')

    def maintain(self):
        with self.lock:
            if self.closed: return
            if not self.ws or not self.ws.connected or getattr(self,'auth_dead',False):
                self.bootstrap()
                if self.http.get(self.origin+'/app/token').status_code==200:self.call({'req':'user.authToken','main':True,'si':self.si})
            r=self.http.get(self.origin+'/app/token')
            if r.status_code==401:
                self.bootstrap()  # Refresh the WebSocket handshake cookies before tokenLogin.
                result=self.call({'req':'user.tokenLogin','ver':2,'si':self.si,'deviceType':'PC','deviceName':'WinToolbox'})
                if not result.get('ticket'): raise ValueError('飞牛登录已失效')
                self.ticket(result['ticket'])
                self.bootstrap()
            elif r.status_code!=200: raise ValueError('无法检查飞牛登录状态')
            self.call({'req':'user.authToken','main':True,'si':self.si})
            self.call({'req':'user.active'})

    def close(self):
        self.closed=True
        self.auth_stop.set()
        if self.ws:
            with contextlib.suppress(Exception): self.ws.abort();self.ws.close(timeout=1)
        self.http.close()
        self.secret=None
        self.key=b''


class SocksServer(socketserver.ThreadingTCPServer):
    allow_reuse_address=os.name != "nt"
    daemon_threads=True

    def server_bind(self):
        if os.name == "nt": self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        super().server_bind()


class FnClient:
    def __init__(self, session_factory=NasSession, port=18792):
        self.port=port
        self.session_factory=session_factory
        self.session=None
        self.server=None
        self.forward_servers={}
        self.stop_event=threading.Event()
        self.sockets=set()
        self.lock=threading.RLock()
        self.state={'phase':'disconnected','connections':0,'tx':0,'rx':0}
        self.on_failure=lambda:None

    def status(self):
        with self.lock: return dict(self.state)

    def start(self, origin, username, password, scope='lan'):
        if scope not in ('lan','all'): raise ValueError('代理范围无效')
        with self.lock:
            if self.state['phase'] not in ('disconnected','error'): raise ValueError('请先断开当前连接')
            self.state={'phase':'connecting','origin':nas_origin(origin),'scope':scope,'connections':0,'tx':0,'rx':0}
        self.stop_event=threading.Event()
        session=self.session_factory(origin)
        self.session=session
        try:
            session.login(username,password)
            policy=getattr(session,'policy',None)
            policy_valid=isinstance(policy,dict) and isinstance(policy.get('networks'),list) and bool(policy['networks'])
            if not policy_valid: policy={'allowPublic':True,'networks':['192.168.100.0/24']}
            if scope=='all' and not policy.get('allowPublic'): raise ValueError('NAS 未允许全局出口，请先在飞牛插件中开启')
            self.state['networks']=policy.get('networks',['192.168.100.0/24'])
            self.state['forward_policy_valid']=policy_valid
            self.state['transport_ips']=getattr(session,'addresses',[])
            if self.stop_event.is_set(): raise ValueError('连接已取消')
            owner=self
            class Handler(socketserver.BaseRequestHandler):
                def handle(self): owner.handle(self.request)
            self.server=SocksServer(('127.0.0.1',self.port),Handler)
            self.state.update(phase='connected',socks_port=self.server.server_address[1],transport='FN Connect 中继' if origin.startswith('https:') else '局域网直连（测试）')
            threading.Thread(target=self.server.serve_forever,daemon=True).start()
            threading.Thread(target=self.keepalive,args=(self.stop_event,),daemon=True).start()
            return self.status()
        except Exception as e:
            self.stop()
            self.state.update(phase='error',error=str(e))
            raise

    def keepalive(self, event):
        while not event.wait(60):
            try:
                try:self.session.maintain()
                except Exception:
                    if event.wait(2):return
                    self.session.bootstrap()
                    self.session.maintain()
            except Exception:
                self.stop()
                self.state.update(phase='error',error='飞牛会话或网络已断开，请重新连接')
                self.on_failure()
                return

    def open(self, host, port, scope=None):
        if self.state['phase']!='connected': raise ValueError('隧道尚未连接')
        s=self.session
        ws=s.connect_ws('/app/lanbridge/tunnel/tcp') if hasattr(s,'connect_ws') else websocket.create_connection(s.origin.replace('http','ws',1)+'/app/lanbridge/tunnel/tcp',timeout=15,cookie=s.cookie(),origin=s.origin,http_no_proxy=['*'])
        with self.lock: self.sockets.add(ws)
        try:
            ws.send(json.dumps({'v':1,'host':host,'port':port,'scope':scope or self.state['scope'],'halfClose':getattr(s,'half_close',False)}))
            ack=json.loads(ws.recv())
            if not ack.get('ok'): raise ValueError('隧道拒绝连接')
            ws.half_close=ack.get('halfClose',False) is True
            ws.settimeout(300)
            return ws
        except Exception:
            ws.close(timeout=1)
            with self.lock: self.sockets.discard(ws)
            raise

    def start_forward(self, ident, local_port, host, port):
        if self.state['phase']!='connected': raise ValueError('请先连接飞牛')
        if self.state.get('forward_policy_valid') is not True: raise ValueError('NAS 局域网桥未提供端口映射访问范围')
        try: target=ipaddress.ip_address(host)
        except ValueError as exc: raise ValueError('目标必须填写局域网 IPv4 地址') from exc
        networks=[]
        for value in self.state.get('networks',[]):
            try: networks.append(ipaddress.ip_network(value,strict=False))
            except ValueError: continue
        if target.version!=4 or target.is_loopback or target.is_link_local or target.is_multicast or target.is_unspecified or not any(target in network and target not in (network.network_address,network.broadcast_address) for network in networks):
            raise ValueError('目标不在 NAS 允许的局域网范围内')
        local_port=int(local_port);port=int(port)
        reserved={18792,18795,self.port,self.state.get('socks_port')}
        if not 1024<=local_port<=65535 or local_port in reserved: raise ValueError('本机端口需为 1024-65535，且不能占用工具箱端口')
        if not 1<=port<=65535: raise ValueError('目标端口无效')
        with self.lock:
            if ident in self.forward_servers: return local_port
            if any(runtime['server'].server_address[1]==local_port for runtime in self.forward_servers.values()): raise ValueError('本机端口已被其他映射使用')
        owner=self
        gate=threading.BoundedSemaphore(16)
        tracker=set()
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                if not gate.acquire(blocking=False):
                    self.request.close();return
                try: owner.relay(self.request,str(target),port,scope='lan',tracker=tracker)
                finally: gate.release()
        try: server=SocksServer(('127.0.0.1',local_port),Handler)
        except OSError as exc: raise ValueError(f'本机端口 {local_port} 无法监听，请更换端口') from exc
        with self.lock:
            if self.state['phase']!='connected':
                server.server_close();raise ValueError('飞牛连接已断开')
            self.forward_servers[ident]={'server':server,'sockets':tracker}
        threading.Thread(target=server.serve_forever,daemon=True).start()
        return server.server_address[1]

    def stop_forward(self, ident):
        with self.lock: runtime=self.forward_servers.pop(ident,None)
        if runtime:
            runtime['server'].shutdown();runtime['server'].server_close()
            with self.lock: sockets=list(runtime['sockets'])
            for value in sockets:
                with contextlib.suppress(Exception):
                    if isinstance(value,socket.socket): value.shutdown(socket.SHUT_RDWR);value.close()
                    else:value.close(timeout=1)

    def forward_ids(self):
        with self.lock: return set(self.forward_servers)

    @staticmethod
    def exact(sock,n):
        result=b''
        while len(result)<n:
            b=sock.recv(n-len(result))
            if not b: raise EOFError()
            result+=b
        return result

    def relay(self,sock,host,port,socks=False,scope=None,tracker=None):
        ws=None
        established=False
        with self.lock:
            self.sockets.add(sock)
            if tracker is not None:tracker.add(sock)
        sock.settimeout(300)
        try:
            ws=self.open(host,port,scope=scope)
            with self.lock:
                if tracker is not None:tracker.add(ws)
            if socks: sock.sendall(bytes([5,0,0,1,0,0,0,0,0,0]))
            established=True
            with self.lock: self.state['connections']+=1
            upload_done=threading.Event()
            def upload():
                clean=False
                try:
                    while not self.stop_event.is_set():
                        b=sock.recv(65536)
                        if not b: break
                        ws.send_binary(b)
                        with self.lock: self.state['tx']+=len(b)
                    if ws.half_close and not self.stop_event.is_set():
                        ws.send(json.dumps({'eof':True}));clean=True
                except Exception: pass
                finally:
                    if not clean:
                        with contextlib.suppress(Exception): ws.close(timeout=1)
                    upload_done.set()
            threading.Thread(target=upload,daemon=True).start()
            while not self.stop_event.is_set():
                op,b=ws.recv_data()
                if op==websocket.ABNF.OPCODE_CLOSE or not b: break
                if op==websocket.ABNF.OPCODE_TEXT and ws.half_close and json.loads(b).get('eof'):
                    sock.shutdown(socket.SHUT_WR)
                    upload_done.wait(300)
                    break
                if op==websocket.ABNF.OPCODE_BINARY:
                    sock.sendall(b)
                    with self.lock: self.state['rx']+=len(b)
        except Exception:
            if socks and not established:
                with contextlib.suppress(Exception): sock.sendall(bytes([5,5,0,1,0,0,0,0,0,0]))
        finally:
            if ws:
                with contextlib.suppress(Exception): ws.close(timeout=1)
            with contextlib.suppress(Exception): sock.shutdown(socket.SHUT_RDWR)
            sock.close()
            with self.lock:
                self.sockets.discard(sock)
                self.sockets.discard(ws)
                if tracker is not None:
                    tracker.discard(sock);tracker.discard(ws)
                if ws: self.state['connections']=max(0,self.state['connections']-1)

    def handle(self,sock):
        try:
            sock.settimeout(300)
            version,n=self.exact(sock,2)
            methods=self.exact(sock,n)
            if version!=5 or 0 not in methods: sock.sendall(bytes([5,255]));return
            sock.sendall(bytes([5,0]))
            version,cmd,_,atype=self.exact(sock,4)
            if version!=5 or cmd!=1: sock.sendall(bytes([5,7,0,1,0,0,0,0,0,0]));return
            if atype==1: host=socket.inet_ntoa(self.exact(sock,4))
            elif atype==3: host=self.exact(sock,self.exact(sock,1)[0]).decode('ascii')
            else: sock.sendall(bytes([5,8,0,1,0,0,0,0,0,0]));return
            port=int.from_bytes(self.exact(sock,2),'big')
        except Exception:
            with contextlib.suppress(Exception): sock.sendall(bytes([5,5,0,1,0,0,0,0,0,0]));sock.close()
            return
        self.relay(sock,host,port,socks=True)

    def probe(self):
        start=time.monotonic()
        ws=self.open('192.168.100.1',80)
        try:
            ws.send_binary(b'GET / HTTP/1.1\r\nHost: 192.168.100.1\r\nConnection: close\r\n\r\n')
            data=b''
            while b'\r\n' not in data:
                _,part=ws.recv_data()
                if not part: raise ValueError('目标未返回 HTTP 响应')
                data+=part
                if len(data)>8192: raise ValueError('响应头过长')
            return {'target':'192.168.100.1:80','status_line':data.split(b'\r\n')[0].decode('ascii','replace'),'latency_ms':round((time.monotonic()-start)*1000),'transport':self.state.get('transport')}
        finally:
            ws.close(timeout=1)
            with self.lock: self.sockets.discard(ws)

    def stop(self):
        self.stop_event.set()
        with self.lock: forward_ids=list(self.forward_servers)
        for ident in forward_ids:self.stop_forward(ident)
        if self.server:
            self.server.shutdown();self.server.server_close();self.server=None
        with self.lock: sockets=list(self.sockets);self.sockets.clear()
        for s in sockets:
            with contextlib.suppress(Exception):
                if isinstance(s,socket.socket): s.shutdown(socket.SHUT_RDWR);s.close()
                else: s.close(timeout=1)
        if self.session:
            with contextlib.suppress(Exception): self.session.close()
        self.session=None
        self.state={'phase':'disconnected','connections':0,'tx':0,'rx':0}
        return self.status()
