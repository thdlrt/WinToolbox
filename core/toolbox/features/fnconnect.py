"""Windows desktop integration; persistent job records never contain credentials."""
import atexit
import ipaddress
import json
from pathlib import Path
from ..settings import protect, atomic_json
import secrets
import threading
import time
from .fnconnect_transport import FnClient, PRIVATE, nas_origin
from .fnconnect_tun import TunController


class ForwardRules:
    RESERVED_PORTS={18792,18795}
    def __init__(self,path,client):
        self.path=Path(path);self.client=client;self.lock=threading.RLock();self.errors={}
        self.rules=[]
        if self.path.exists():
            try:
                value=json.loads(self.path.read_text('utf-8'))
                if value.get('version')==1 and isinstance(value.get('rules'),list): self.rules=[self._rule(rule) for rule in value['rules']]
            except Exception as exc:self.errors['_load']='端口映射配置无法读取：'+str(exc)
    def _rule(self,value,creating=False):
        if not isinstance(value,dict): raise ValueError('端口映射无效')
        ident=str(value.get('id') or (secrets.token_hex(8) if creating else ''))
        if not ident or len(ident)>64: raise ValueError('端口映射 ID 无效')
        name=str(value.get('name','')).strip()
        if not name or len(name)>60 or any(ord(char)<32 for char in name): raise ValueError('名称需为 1-60 个字符')
        target=str(value.get('target_host','')).strip()
        try: address=ipaddress.ip_address(target)
        except ValueError as exc: raise ValueError('目标地址必须是局域网 IPv4') from exc
        if address.version!=4 or not any(address in network and address not in (network.network_address,network.broadcast_address) for network in PRIVATE):
            raise ValueError('目标地址必须是可用的局域网 IPv4')
        target_port=int(value.get('target_port',0));local_port=int(value.get('local_port',0))
        if not 1<=target_port<=65535: raise ValueError('目标端口无效')
        if not 1024<=local_port<=65535 or local_port in self.RESERVED_PORTS: raise ValueError('本机端口需为 1024-65535，且不能占用工具箱端口')
        kind=str(value.get('kind','http'))
        if kind not in ('http','https','tcp'): raise ValueError('映射类型无效')
        return {'id':ident,'name':name,'target_host':str(address),'target_port':target_port,'local_port':local_port,'kind':kind,'enabled':value.get('enabled') is True}
    def _write(self):
        self.path.parent.mkdir(parents=True,exist_ok=True);atomic_json(self.path,{'version':1,'rules':self.rules})
    def _next_port(self):
        used={rule['local_port'] for rule in self.rules}|self.RESERVED_PORTS
        for port in range(18880,19080):
            if port not in used:return port
        raise ValueError('没有可用的自动端口，请手动填写')
    def _activate(self,rule):
        if not rule['enabled'] or self.client.status().get('phase')!='connected': return
        try:self.client.start_forward(rule['id'],rule['local_port'],rule['target_host'],rule['target_port']);self.errors.pop(rule['id'],None)
        except Exception as exc:self.errors[rule['id']]=str(exc)
    def start_enabled(self):
        with self.lock:
            for rule in self.rules:self._activate(rule)
    def list(self):
        with self.lock:
            active=self.client.forward_ids() if hasattr(self.client,'forward_ids') else set();connected=self.client.status().get('phase')=='connected'
            result=[]
            for rule in self.rules:
                state='disabled' if not rule['enabled'] else ('listening' if rule['id'] in active else ('error' if rule['id'] in self.errors else ('waiting' if not connected else 'starting')))
                endpoint=('http://' if rule['kind']=='http' else 'https://' if rule['kind']=='https' else '')+f"127.0.0.1:{rule['local_port']}"
                result.append({**rule,'state':state,'local_endpoint':endpoint,**({'error':self.errors[rule['id']]} if rule['id'] in self.errors else {})})
            return {'rules':result,**({'error':self.errors['_load']} if '_load' in self.errors else {})}
    def save(self,value):
        with self.lock:
            if not isinstance(value,dict): raise ValueError('端口映射无效')
            creating=not value.get('id')
            if creating and len(self.rules)>=24: raise ValueError('最多保存 24 条端口映射')
            if not value.get('local_port'): value={**value,'local_port':self._next_port()}
            rule=self._rule(value,creating=creating)
            if any(other['local_port']==rule['local_port'] and other['id']!=rule['id'] for other in self.rules): raise ValueError('本机端口已被其他映射使用')
            index=next((i for i,other in enumerate(self.rules) if other['id']==rule['id']),None)
            if not creating and index is None: raise ValueError('端口映射不存在')
            self.client.stop_forward(rule['id'])
            if index is None:self.rules.append(rule)
            else:self.rules[index]=rule
            self.errors.pop(rule['id'],None);self._write();self._activate(rule)
            return next(item for item in self.list()['rules'] if item['id']==rule['id'])
    def enable(self,ident,enabled):
        with self.lock:
            rule=next((rule for rule in self.rules if rule['id']==ident),None)
            if not rule: raise ValueError('端口映射不存在')
            self.client.stop_forward(ident);rule['enabled']=enabled is True;self.errors.pop(ident,None);self._write();self._activate(rule)
            return next(item for item in self.list()['rules'] if item['id']==ident)
    def delete(self,ident):
        with self.lock:
            if not any(rule['id']==ident for rule in self.rules): raise ValueError('端口映射不存在')
            self.client.stop_forward(ident);self.rules=[rule for rule in self.rules if rule['id']!=ident];self.errors.pop(ident,None);self._write()
            return {'ok':True}


def register(app):
    client=FnClient();tun=TunController(app.data_dir);forwards=ForwardRules(Path(app.data_dir)/'fnconnect/forwards.json',client)
    client.on_failure=tun.stop
    credentials={};lock=threading.RLock()
    saved_path=Path(app.data_dir)/'fnconnect-login.json'
    def saved():
        with lock:
            if not saved_path.exists(): return {}
            return json.loads(saved_path.read_text('utf-8'))
    def profile(_=None):
        value=saved()
        return {k:value[k] for k in ('origin','username','scope') if k in value} | {'remember':bool(value.get('secret'))}
    def forget(_=None):
        with lock: saved_path.unlink(missing_ok=True)
        return {'ok':True}
    app.register('fnconnect.profile',profile)
    app.register('fnconnect.forget',forget)
    def close():
        with lock: credentials.clear()
        tun.stop();client.stop()
    app.fnconnect_close=close
    atexit.register(close)
    def status(_=None): return {'state':client.status(),'tun':tun.status(),'forwards':forwards.list()}
    def run_connect(job):
        with lock: secret=credentials.pop(job.params.get('credential_token'),None)
        if not secret or time.monotonic()-secret[2]>300: raise ValueError('请重新输入飞牛密码后连接')
        username,password,_,remember=secret
        job.progress(10,'正在连接飞牛')
        try:
            job.check_cancelled()
            client.start(job.params['origin'],username,password,job.params['scope'])
            forwards.start_enabled()
            if remember:
                with lock: atomic_json(saved_path,{'origin':job.params['origin'],'username':username,'scope':job.params['scope'],'secret':protect(password)})
            try: job.check_cancelled()
            except BaseException: client.stop();raise
            if tun.enabled:
                current=client.status()
                job.progress(80,'正在恢复上次开启的 TUN')
                tun.start(current['origin'],current['scope'],current.get('networks'),current.get('transport_ips'))
                try: job.check_cancelled()
                except BaseException: tun.stop();client.stop();raise
            return status()
        finally: password=None;secret=None
    def connect(p):
        origin=nas_origin(p.get('origin'))
        scope=p.get('scope','lan')
        if scope not in ('lan','all'): raise ValueError('代理范围无效')
        username,password=p.get('username'),p.get('password')
        remember=p.get('remember',False) is True
        value=saved()
        if not password and value.get('origin')==origin and value.get('username')==username and value.get('secret'):
            password=protect(value['secret'],decrypt=True)
        if not remember: forget()
        if not isinstance(username,str) or not username or not isinstance(password,str) or not password or len(password)>256: raise ValueError('请输入飞牛管理员账号和密码')
        if client.status()['phase'] not in ('disconnected','error'): raise ValueError('请先断开当前连接')
        token=secrets.token_hex(16)
        with lock:
            for old in list(credentials):
                if time.monotonic()-credentials[old][2]>300: credentials.pop(old,None)
            if credentials: raise ValueError('已有连接任务，请稍后重试')
            credentials[token]=(username,password,time.monotonic(),remember)
        try: return app.jobs.submit('fnconnect.connect',{'origin':origin,'scope':scope,'credential_token':token})
        except BaseException:
            with lock: credentials.pop(token,None)
            raise
    def disconnect(job):
        with lock: credentials.clear()
        tun.stop();client.stop();return status()
    def probe(job):
        job.check_cancelled();job.progress(20,'正在通过隧道连接家中路由器');return client.probe()
    def tun_start(job):
        current=client.status()
        if current['phase']!='connected': raise ValueError('请先连接飞牛')
        job.check_cancelled()
        return tun.start(current['origin'],current['scope'],current.get('networks'),current.get('transport_ips'))
    app.jobs.register('fnconnect.connect',run_connect)
    app.jobs.register('fnconnect.disconnect',disconnect)
    app.jobs.register('fnconnect.probe',probe)
    app.jobs.register('fnconnect.tun-start',tun_start)
    app.jobs.register('fnconnect.tun-stop',lambda job:tun.stop(disable=True))
    def reset(_):
        for job in app.jobs.list():
            if job.get('tool','').startswith('fnconnect.') and job.get('status') in ('queued','running','cancelling'):
                app.jobs.cancel(job['id'])
        threading.Thread(target=close,daemon=True).start()
        return {'ok':True}
    app.register('fnconnect.reset',reset)
    app.register('fnconnect.status',status)
    app.register('fnconnect.forward.list',lambda _:forwards.list())
    app.register('fnconnect.forward.save',forwards.save)
    app.register('fnconnect.forward.enable',lambda p:forwards.enable(str(p.get('id','')),p.get('enabled') is True))
    app.register('fnconnect.forward.delete',lambda p:forwards.delete(str(p.get('id',''))))
    app.register('fnconnect.connect',connect)
    for method in ('disconnect','probe','tun-start','tun-stop'):
        app.register('fnconnect.'+method,lambda p,m=method:app.jobs.submit('fnconnect.'+m,{}))
