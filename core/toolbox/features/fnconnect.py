"""Windows desktop integration; persistent job records never contain credentials."""
import atexit
import json
from pathlib import Path
from ..settings import protect, atomic_json
import secrets
import threading
import time
from .fnconnect_transport import FnClient, nas_origin
from .fnconnect_tun import TunController


def register(app):
    client=FnClient();tun=TunController(app.data_dir)
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
    def status(_=None): return {'state':client.status(),'tun':tun.status()}
    def run_connect(job):
        with lock: secret=credentials.pop(job.params.get('credential_token'),None)
        if not secret or time.monotonic()-secret[2]>300: raise ValueError('请重新输入飞牛密码后连接')
        username,password,_,remember=secret
        job.progress(10,'正在连接飞牛')
        try:
            job.check_cancelled()
            client.start(job.params['origin'],username,password,job.params['scope'])
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
    app.register('fnconnect.connect',connect)
    for method in ('disconnect','probe','tun-start','tun-stop'):
        app.register('fnconnect.'+method,lambda p,m=method:app.jobs.submit('fnconnect.'+m,{}))
