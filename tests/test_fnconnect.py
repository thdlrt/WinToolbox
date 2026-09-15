import json
import socket
import threading
import time
import pytest
from toolbox.app import App
from toolbox.features import fnconnect
from toolbox.features.fnconnect_transport import FnClient, nas_origin
from toolbox.features.fnconnect_tun import tun_config, TunController


def test_origins_and_tun_rules(monkeypatch):
    assert nas_origin('https://test.fnos.net')=='https://test.fnos.net'
    for value in ('https://example.com','http://test.fnos.net','https://u:p@test.fnos.net','https://test.fnos.net/login','http://127.0.0.1'):
        with pytest.raises(ValueError): nas_origin(value)
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('192.168.100.246',443))])
    c=tun_config('http://192.168.100.246:5666','all')
    assert c['rules'][0]=='IP-CIDR,192.168.100.246/32,DIRECT,no-resolve'
    assert 'NETWORK,udp,REJECT' in c['rules']
    assert c['rules'][-1]=='MATCH,HOME'
    assert c['proxies'][0]['udp'] is False
    assert c['ipv6'] is True  # Capture IPv6 to reject it, never silently bypass.
    assert c['tun']['inet6-address']
    assert '192.168.100.0/25' in c['tun']['route-address']


def test_credential_job_never_persists_password(tmp_path,monkeypatch):
    class Client:
        def __init__(self):self.state={'phase':'disconnected'}
        def status(self):return self.state
        def start(self,origin,username,password,scope):
            assert password=='fixture-secret-123'
            self.state={'phase':'connected'}
        def stop(self):self.state={'phase':'disconnected'}
    monkeypatch.setattr(fnconnect,'FnClient',Client)
    app=App(tmp_path,register_features=False,register_live=False)
    fnconnect.register(app)
    try:
        with pytest.raises(ValueError): app.jobs.submit('fnconnect.connect',{'password':'fixture-secret-123'})
        job=app.handlers['fnconnect.connect']({'origin':'https://test.fnos.net','username':'fixture','password':'fixture-secret-123'})
        for _ in range(100):
            current=app.jobs.get(job['id'])
            if current['status'] in ('completed','failed'):break
            time.sleep(.02)
        assert current['status']=='completed',current.get('error')
        assert 'fixture-secret-123' not in json.dumps(app.jobs.list())
        assert 'password' not in current['params']
    finally:app.close()


def test_socks_tcp_framing_and_cleanup():
    from websockets.sync.server import serve
    addresses=[]
    def handler(ws):
        addresses.append(json.loads(ws.recv()))
        ws.send(json.dumps({'ok':True}))
        try:
            for message in ws:ws.send(message)
        except Exception:pass
    server=serve(handler,'127.0.0.1',0)
    port=server.socket.getsockname()[1]
    threading.Thread(target=server.serve_forever,daemon=True).start()
    class Session:
        origin=f'http://127.0.0.1:{port}'
        def login(self,*a):pass
        def cookie(self):return ''
        def close(self):pass
        def maintain(self):pass
    client=FnClient(session_factory=lambda origin:Session(),port=0)
    try:
        client.start('http://192.168.100.246:5666','fixture','unused')
        sock=socket.create_connection(('127.0.0.1',client.status()['socks_port']),timeout=5)
        sock.sendall(b'\x05');sock.sendall(b'\x01\x00')
        assert FnClient.exact(sock,2)==b'\x05\x00'
        sock.sendall(bytes([5,1,0,1,192,168,100,1,0,80]))
        assert FnClient.exact(sock,10)[1]==0
        payload=b'z'*100000;sock.sendall(payload)
        assert FnClient.exact(sock,len(payload))==payload
        assert addresses[0]['host']=='192.168.100.1'
        sock.close()
    finally:client.stop();server.shutdown()
    assert client.status()['phase']=='disconnected'

def test_transport_dial_uses_pinned_address(monkeypatch):
    import httpcore
    from toolbox.features.fnconnect_transport import PinnedBackend
    seen=[]
    def dial(self,host,port,timeout,local_address,socket_options):
        seen.append((host,port));return 'socket'
    monkeypatch.setattr(httpcore.SyncBackend,'connect_tcp',dial)
    backend=PinnedBackend('test.fnos.net',['203.0.113.8'])
    assert backend.connect_tcp('test.fnos.net',443)=='socket'
    assert seen==[('203.0.113.8',443)]


def test_renewal_authentication_includes_current_handshake_id():
    from toolbox.features.fnconnect_transport import NasSession
    from types import SimpleNamespace
    session=NasSession.__new__(NasSession)
    session.lock=threading.RLock();session.closed=False;session.origin='https://test.fnos.net'
    session.ws=SimpleNamespace(connected=True);session.si='old';session.http=SimpleNamespace(get=lambda url:SimpleNamespace(status_code=401))
    seen=[]
    def bootstrap():session.si='fresh';seen.append('bootstrap')
    session.bootstrap=bootstrap
    def call(body):seen.append(body);return {'ticket':'ephemeral'}
    session.call=call;session.ticket=lambda ticket:seen.append('ticket')
    session.maintain()
    auth=[v for v in seen if isinstance(v,dict) and v['req']=='user.authToken'][0]
    assert auth['si']=='fresh'
    assert seen.count('bootstrap')==2
    assert seen[-1]['req']=='user.active'


def test_remember_login_encrypted_and_bound_to_account(tmp_path,monkeypatch):
    import os
    if os.name != 'nt': pytest.skip('Windows DPAPI')
    seen=[]
    class Client:
        def __init__(self): self.state={'phase':'disconnected'}
        def status(self): return self.state
        def start(self,origin,username,password,scope):
            seen.append(password);self.state={'phase':'connected'}
        def stop(self): self.state={'phase':'disconnected'}
    monkeypatch.setattr(fnconnect,'FnClient',Client)
    def wait(app,job):
        for _ in range(100):
            current=app.jobs.get(job['id'])
            if current['status'] in ('completed','failed'): break
            time.sleep(.02)
        assert current['status']=='completed',current.get('error')
    params={'origin':'https://test.fnos.net','username':'fixture','password':'unique-fixture-password','remember':True}
    app=App(tmp_path,register_features=False,register_live=False);fnconnect.register(app)
    try:
        wait(app,app.handlers['fnconnect.connect'](params))
        assert 'unique-fixture-password' not in (tmp_path/'fnconnect-login.json').read_text()
        assert 'secret' not in app.handlers['fnconnect.profile']({})
        assert 'unique-fixture-password' not in json.dumps(app.jobs.list())
    finally: app.close()
    app=App(tmp_path,register_features=False,register_live=False);fnconnect.register(app)
    try:
        assert app.handlers['fnconnect.profile']({})['remember']
        with pytest.raises(ValueError): app.handlers['fnconnect.connect']({**params,'origin':'https://other.fnos.net','password':''})
        wait(app,app.handlers['fnconnect.connect']({**params,'password':''}))
        assert seen==['unique-fixture-password']*2
        app.handlers['fnconnect.forget']({})
        assert not (tmp_path/'fnconnect-login.json').exists()
        assert not app.handlers['fnconnect.profile']({})['remember']
    finally: app.close()


def test_tun_preference_survives_disconnect_but_not_manual_stop(tmp_path,monkeypatch):
    from toolbox.features import fnconnect_service
    def unavailable(*args,**kwargs): raise OSError('fixture service absent')
    monkeypatch.setattr(fnconnect_service,'request',unavailable)
    tun=TunController(tmp_path)
    assert not tun.enabled
    tun.remember(True)
    tun.stop()
    assert TunController(tmp_path).enabled
    tun.stop(disable=True)
    assert not TunController(tmp_path).enabled


def test_service_generates_only_fixed_config():
    import subprocess
    from pathlib import Path
    executable=Path(__file__).resolve().parents[1]/'.build/fnconnect/WinToolboxTun.exe'
    if not executable.exists(): pytest.skip('Run prepare_fnconnect.py first')
    params={'host':'test.fnos.net','scope':'all','networks':['192.168.100.0/24'],'transport_ips':['203.0.113.8'],
            'executable':'injected.exe','config':{'external-controller':'0.0.0.0:9999','external-ui':'C:/untrusted'}}
    def validate(value):
        return subprocess.run([str(executable),'--validate'],input=json.dumps(value)+'\n',capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=10)
    result=validate(params)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)==tun_config('https://test.fnos.net','all',transport_ips=params['transport_ips'])
    for patch in ({'host':'test.fnos.net,REJECT'},{'networks':['0.0.0.0/0']},{'transport_ips':['example.com']},{'scope':'shell'}):
        assert validate({**params,**patch}).returncode!=0


def test_connect_restores_remembered_tun(tmp_path,monkeypatch):
    starts=[]
    class Client:
        def status(self): return {'phase':'disconnected','origin':'https://test.fnos.net','scope':'all','networks':['192.168.100.0/24'],'transport_ips':['203.0.113.8']}
        def start(self,*args): pass
        def stop(self): pass
    class Tun:
        enabled=True
        def __init__(self,*args): pass
        def status(self): return {'phase':'stopped','enabled':True}
        def start(self,*args): starts.append(args)
        def stop(self,*args,**kwargs): pass
    monkeypatch.setattr(fnconnect,'FnClient',Client);monkeypatch.setattr(fnconnect,'TunController',Tun)
    app=App(tmp_path,register_features=False,register_live=False);fnconnect.register(app)
    try:
        job=app.handlers['fnconnect.connect']({'origin':'https://test.fnos.net','scope':'all','username':'fixture','password':'fixture'})
        for _ in range(100):
            current=app.jobs.get(job['id'])
            if current['status'] in ('completed','failed'): break
            time.sleep(.02)
        assert current['status']=='completed'
        assert starts==[('https://test.fnos.net','all',['192.168.100.0/24'],['203.0.113.8'])]
    finally: app.close()


def test_auth_reader_ignores_push_notifications_and_keeps_receiving():
    import queue,websocket
    from toolbox.features.fnconnect_transport import NasSession
    incoming=queue.Queue()
    class Socket:
        def recv(self):
            try: return incoming.get(timeout=.05)
            except queue.Empty: raise websocket.WebSocketTimeoutException()
        def send(self,wire):
            req=json.loads(wire)
            for _ in range(100): incoming.put(json.dumps({'event':'notification'}))
            incoming.put(json.dumps({'reqid':req['reqid'],'ok':True}))
    session=NasSession.__new__(NasSession)
    session.ws=Socket();session.lock=threading.RLock();session.secret=None;session.pending_id=None;session.auth_dead=False
    session.replies=queue.Queue(maxsize=8);done=threading.Event()
    reader=threading.Thread(target=session._read_auth,args=(session.ws,session.replies,done),daemon=True);reader.start()
    try:
        assert session.call({'req':'fixture'})['ok']
        assert session.call({'req':'fixture2'})['ok']
        assert session.replies.empty()
        assert not session.auth_dead
    finally:done.set();reader.join(1)


def test_socks_half_close_receives_response_after_client_eof():
    from websockets.sync.server import serve
    def handle(ws):
        assert json.loads(ws.recv())['halfClose']
        ws.send(json.dumps({'ok':True,'halfClose':True}))
        data=bytearray()
        while True:
            message=ws.recv()
            if isinstance(message,str):
                assert json.loads(message)['eof'];break
            data.extend(message)
        ws.send(bytes(data));ws.send(json.dumps({'eof':True}))
    server=serve(handle,'127.0.0.1',0);port=server.socket.getsockname()[1]
    threading.Thread(target=server.serve_forever,daemon=True).start()
    class Session:
        origin=f'http://127.0.0.1:{port}'
        half_close=True
        def login(self,*args): pass
        def cookie(self): return ''
        def close(self): pass
        def maintain(self): pass
    client=FnClient(session_factory=lambda origin:Session(),port=0)
    try:
        client.start('http://192.168.100.246:5666','fixture','fixture')
        with socket.create_connection(('127.0.0.1',client.status()['socks_port']),timeout=5) as sock:
            sock.sendall(bytes([5,1,0]));assert client.exact(sock,2)==bytes([5,0])
            sock.sendall(bytes([5,1,0,1,192,168,100,1,0,80]));assert client.exact(sock,10)[1]==0
            body=b'z'*200000;sock.sendall(body);sock.shutdown(socket.SHUT_WR)
            assert client.exact(sock,len(body))==body
            assert sock.recv(1)==b''
    finally:client.stop();server.shutdown()


def test_selective_forward_relays_raw_tcp_on_loopback_with_lan_scope():
    from websockets.sync.server import serve
    handshakes=[]
    def handle(ws):
        hello=json.loads(ws.recv());handshakes.append(hello)
        ws.send(json.dumps({'ok':True,'halfClose':True}))
        while True:
            message=ws.recv()
            if isinstance(message,str):
                assert json.loads(message)['eof'];ws.send(json.dumps({'eof':True}));break
            ws.send(message)
    server=serve(handle,'127.0.0.1',0);remote_port=server.socket.getsockname()[1]
    threading.Thread(target=server.serve_forever,daemon=True).start()
    class Session:
        origin=f'http://127.0.0.1:{remote_port}'
        half_close=True
        policy={'allowPublic':True,'networks':['192.168.100.0/24']}
        def login(self,*args): pass
        def cookie(self): return ''
        def close(self): pass
        def maintain(self): pass
    probe=socket.socket();probe.bind(('127.0.0.1',0));local_port=probe.getsockname()[1];probe.close()
    client=FnClient(session_factory=lambda origin:Session(),port=0)
    try:
        client.start('http://192.168.100.246:5666','fixture','fixture','all')
        client.start_forward('fixture',local_port,'192.168.100.20',8123)
        with socket.create_connection(('127.0.0.1',local_port),timeout=5) as connection:
            body=b'GET /api/status HTTP/1.1\r\nHost: device\r\n\r\n'
            connection.sendall(body)
            assert client.exact(connection,len(body))==body
        client.stop_forward('fixture')
        with pytest.raises(OSError): socket.create_connection(('127.0.0.1',local_port),timeout=.2)
        assert handshakes and handshakes[0]['scope']=='lan'
        assert handshakes[0]['host']=='192.168.100.20' and handshakes[0]['port']==8123
    finally:client.stop();server.shutdown()


def test_selective_forward_requires_valid_nas_policy_and_allowed_target():
    class Session:
        origin='http://127.0.0.1:9';half_close=False
        def login(self,*args): pass
        def close(self): pass
        def maintain(self): pass
    client=FnClient(session_factory=lambda origin:Session(),port=0)
    try:
        client.start('http://192.168.100.246:5666','fixture','fixture')
        with pytest.raises(ValueError,match='访问范围'):client.start_forward('one',18880,'192.168.100.20',80)
    finally:client.stop()
    Session.policy={'allowPublic':False,'networks':['192.168.100.0/24']}
    client=FnClient(session_factory=lambda origin:Session(),port=0)
    try:
        client.start('http://192.168.100.246:5666','fixture','fixture')
        for host in ('127.0.0.1','169.254.1.2','192.168.101.2','8.8.8.8'):
            with pytest.raises(ValueError):client.start_forward('one',18880,host,80)
        with pytest.raises(ValueError):client.start_forward('one',18792,'192.168.100.20',80)
    finally:client.stop()


def test_forward_rules_persist_toggle_and_reject_unsafe_targets(tmp_path):
    class Client:
        def __init__(self):self.active=set();self.started=[]
        def status(self):return {'phase':'connected'}
        def forward_ids(self):return set(self.active)
        def start_forward(self,ident,local_port,host,port):self.active.add(ident);self.started.append((ident,local_port,host,port))
        def stop_forward(self,ident):self.active.discard(ident)
    client=Client();path=tmp_path/'fnconnect/forwards.json';rules=fnconnect.ForwardRules(path,client)
    item=rules.save({'name':'Home API','target_host':'192.168.100.20','target_port':8123,'local_port':0,'kind':'http','enabled':True})
    assert item['state']=='listening' and item['local_endpoint'].startswith('http://127.0.0.1:')
    assert client.started[0][2:]==('192.168.100.20',8123)
    assert json.loads(path.read_text('utf-8'))['rules'][0]['id']==item['id']
    rules.enable(item['id'],False);assert rules.list()['rules'][0]['state']=='disabled'
    loaded=fnconnect.ForwardRules(path,client);assert loaded.list()['rules'][0]['name']=='Home API'
    loaded.delete(item['id']);assert loaded.list()['rules']==[]
    base={'name':'bad','target_port':80,'local_port':18890,'kind':'http','enabled':True}
    for host in ('device.local','8.8.8.8','127.0.0.1','169.254.1.2'):
        with pytest.raises(ValueError):rules.save({**base,'target_host':host})
    with pytest.raises(ValueError):rules.save({**base,'target_host':'192.168.100.20','local_port':18792})
