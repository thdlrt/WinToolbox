"""Real PC/Android DAV interoperability on an explicitly named emulator.

Uses synthetic credentials and temporary PC data. Clears ONLY the debug fixture
app on the requested emulator; requires compiled debug instrumentation APKs.
"""
import argparse
import base64
import contextlib
import importlib.util
import json
from pathlib import Path
import subprocess
import shlex
import tempfile
import threading
import time

from toolbox.app import App


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--android-root', type=Path, required=True)
    parser.add_argument('--serial', required=True)
    args = parser.parse_args()
    if not args.serial.startswith('emulator-'):
        raise ValueError('Explicit Android SDK emulator required')
    adb = args.android_root/'.build/sdk/platform-tools/adb.exe'
    def command(*values, timeout=90):
        if values and values[0] == 'shell':
            values = ('shell', shlex.join(values[1:]))
        process = subprocess.run([str(adb),'-s',args.serial,*values],capture_output=True,timeout=timeout)
        if process.returncode:
            raise RuntimeError(process.stderr.decode('utf-8','replace'))
        return process.stdout.decode('utf-8','replace')
    assert command('shell','getprop','ro.kernel.qemu').strip()=='1'
    spec = importlib.util.spec_from_file_location('ledger_dav_fixture',args.android_root/'scripts/relay-test-server.py')
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    server = fixture.ThreadingHTTPServer(('127.0.0.1',0),fixture.Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    checks=[]
    command('shell','am','force-stop','net.lanbridge.android.debug')
    assert 'Success' in command('shell','pm','clear','net.lanbridge.android.debug')
    def mobile(action='sync', **extra):
        params={'action':action,'endpoint':f'http://10.0.2.2:{server.server_port}/dav/',
                'remote_path':'WinToolbox','username':'user','password':'pass',**extra}
        values=['shell','am','instrument','-w']
        for k,v in params.items():
            values.extend(['-e',k,str(v)])
        values.append('net.lanbridge.android.debug.test/net.lanbridge.android.NavigationInstrumentation')
        output=command(*values,timeout=100)
        if 'PASS:' not in output or 'FAIL:' in output:
            raise AssertionError(output)
        result=json.loads(command('shell','run-as','net.lanbridge.android.debug','cat','files/ledger-test-result.json'))
        if '同步失败' in result.get('status',''):
            raise AssertionError(result['status'])
        return result
    def finish(app, job):
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            row=app.jobs.get(job['id'])
            if row['status'] not in ('queued','running','cancelling') and job['id'] not in app.jobs.active:
                assert row['status']=='completed',row
                return row['result']
            time.sleep(.05)
        raise AssertionError('PC fixture job timed out')
    try:
        with tempfile.TemporaryDirectory(prefix='ledger-real-interop-') as temp:
            app=App(Path(temp)/'pc',register_live=False)
            app.ledger_close()  # deterministic manual rounds; cold Android startup tested below
            try:
                app.call('webdav.save',{'url':f'http://127.0.0.1:{server.server_port}/dav/','remote_path':'WinToolbox','username':'user','password':'pass'})
                project=app.call('expenses.projects.save',{'name':'Interop project','settlement_mode':'general'})
                entry=app.call('expenses.save',{'title':'PC receipt','project_id':project['id'],'date':'2026-09-26','amount':'123.45','category':'travel','notes':'initial'})
                pdf=Path(temp)/'receipt.pdf';pdf.write_bytes(b'%PDF-1.7\nsynthetic ledger fixture')
                entry=finish(app,app.call('expenses.attach',{'id':entry['id'],'revision':entry['revision'],'paths':[str(pdf)],'kind':'receipt'}))['entry']
                app.call('network.profiles.save',{'name':'Interop diagnostic','target':'https://example.com','attempts':1})
                finish(app,app.call('expenses.sync'))
                device=mobile()
                item=next(v for v in device['entries'] if v['id']==entry['id'])
                assert item['amount']=='123.45' and item['project_id']==project['id']
                assert any(k.startswith('attachment:') for k in item)
                assert any(v['name']=='Interop diagnostic' for v in device['network_profiles'])
                checks+=['PC→Android projects/entries/attachments/diagnostic profiles']
                # Phone keeps its old heads while PC publishes an independent edit.
                entry=app.call('expenses.get',{'id':entry['id']})
                app.call('expenses.save',{'id':entry['id'],'revision':entry['revision'],'title':'PC concurrent title'})
                finish(app,app.call('expenses.sync'))
                device=mobile('seed',project_id=project['id'],edit_id=entry['id'],edit_title='Android concurrent title',note='Android disjoint note')
                finish(app,app.call('expenses.sync'))
                current=app.call('expenses.get',{'id':entry['id']})
                assert current['notes']=='Android disjoint note'
                conflict=next(v for v in app.call('expenses.conflicts')['items'] if v['id']==entry['id'])
                assert 'title' in conflict['fields']
                app.call('expenses.resolve',{'entity_type':'entry','id':entry['id'],'heads':conflict['heads'],'changes':{'title':'PC concurrent title'}})
                finish(app,app.call('expenses.sync'))
                device=mobile()
                resolved=next(v for v in device['entries'] if v['id']==entry['id'])
                assert resolved['title']=='PC concurrent title' and not resolved['_conflicts']
                checks+=['concurrent field conflict retained','disjoint fields merged','conflict resolution PC→Android']
                listing=app.call('expenses.list',{'project_id':project['id'],'start_date':'2026-09-01','end_date':'2026-09-30'})
                mobile_entry=next(v for v in listing['items'] if v['id']!=entry['id'])
                assert mobile_entry['attachments']
                attachment=app.call('expenses.attachment',{'id':mobile_entry['id'],'attachment_id':mobile_entry['attachments'][0]['id']})
                assert Path(attachment['path']).read_bytes().startswith(b'\x89PNG')
                checks+=['Android→PC entry and hash-verified image']
                current=app.call('expenses.get',{'id':entry['id']})
                app.call('expenses.delete',{'id':entry['id'],'revision':current['revision']})
                finish(app,app.call('expenses.sync'))
                device=mobile()
                assert entry['id'] not in [v['id'] for v in device['entries']]
                checks+=['delete tombstone converges']
                exported=finish(app,app.call('expenses.export',{'project_id':project['id'],'start_date':'2026-09-01','end_date':'2026-09-30','format':'xlsx'}))
                assert exported['rows']==1 and Path(exported['path']).is_file()
                checks+=['project/date export includes Android record']
                new=app.call('expenses.save',{'title':'Cold startup sync','project_id':project['id'],'date':'2026-09-26','amount':'1.00'})
                finish(app,app.call('expenses.sync'))
                operation=next(o for o in app.ledger.export_operations() if o['entity_id']==new['id'])
                command('shell','am','force-stop','net.lanbridge.android.debug')
                command('shell','am','start','-n','net.lanbridge.android.debug/net.lanbridge.android.MainActivity')
                deadline=time.monotonic()+35
                while time.monotonic()<deadline:
                    names=command('shell','run-as','net.lanbridge.android.debug','ls','files/ledger-v1/ops')
                    if operation['op_id']+'.json' in names:
                        break
                    time.sleep(.25)
                else:
                    raise AssertionError('Opening Android did not automatically pull new PC entry')
                checks+=['Android cold-open automatic sync']
                print(json.dumps({'ok':True,'serial':args.serial,'checks':checks,'http_requests':len(fixture.requests)},ensure_ascii=False))
            finally:
                app.close();app.jobs.pool.shutdown(wait=True)
    finally:
        command('shell','am','force-stop','net.lanbridge.android.debug')
        server.shutdown();server.server_close();thread.join(3)


if __name__=='__main__':
    main()
