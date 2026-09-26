"""Authenticated shared-root migration/config-restore acceptance fixture.

Only temporary PC data and the synthetic Android DAV server are used.
"""
import argparse
import importlib.util
import io
import json
import shlex
import subprocess
from pathlib import Path
import tempfile
import threading
import time
import zipfile

from toolbox.app import App
from toolbox.settings import atomic_json, protect


def finish(app, job):
    deadline = time.monotonic()+60
    while time.monotonic()<deadline:
        row=app.jobs.get(job['id'])
        if row['status'] not in ('queued','running','cancelling') and job['id'] not in app.jobs.active:
            assert row['status']=='completed', row
            return row['result']
        time.sleep(.02)
    raise AssertionError('Fixture timed out')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--android-root',type=Path,required=True)
    parser.add_argument('--serial');parser.add_argument('--android-only',action='store_true');parser.add_argument('--move-root')
    args=parser.parse_args()
    spec=importlib.util.spec_from_file_location('layout_dav',args.android_root/'scripts/relay-test-server.py')
    dav=importlib.util.module_from_spec(spec);spec.loader.exec_module(dav)
    dav.directories.update({'/dav/old-inbox/','/dav/old-inbox/nested/','/dav/Shared/','/dav/Shared/file-relay/'})
    dav.files.update({'/dav/old-inbox/note.txt':b'legacy-version','/dav/old-inbox/nested/receipt.txt':b'nested-legacy',
                      '/dav/Shared/file-relay/note.txt':b'existing-canonical'})
    for name in dav.files: dav.modified[name]=time.time()
    http=dav.ThreadingHTTPServer(('127.0.0.1',0),dav.Handler)
    thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
    try:
        if args.serial:
            if not args.serial.startswith('emulator-'): raise ValueError('Explicit emulator required')
            adb=args.android_root/'.build/sdk/platform-tools/adb.exe'
            def shell(*parts):
                process=subprocess.run([str(adb),'-s',args.serial,'shell',shlex.join(parts)],capture_output=True,timeout=150)
                assert process.returncode==0,process.stderr.decode('utf-8','replace')
                return process.stdout.decode('utf-8','replace')
            assert shell('getprop','ro.kernel.qemu').strip()=='1'
            shell('am','force-stop','net.lanbridge.android.debug')
            assert 'Success' in shell('pm','clear','net.lanbridge.android.debug')
            options=['am','instrument','-w','-e','action','webdav','-e','endpoint',f'http://10.0.2.2:{http.server_port}/dav/',
                     '-e','remote_path','Shared','-e','legacy_path','old-inbox','-e','username','user','-e','password','pass']
            if args.move_root: options.extend(['-e','move_root',args.move_root])
            output=shell(*options,'net.lanbridge.android.debug.test/net.lanbridge.android.NavigationInstrumentation')
            assert 'PASS:' in output,output
            result=json.loads(shell('run-as','net.lanbridge.android.debug','cat','files/webdav-test-result.json'))
            expected_root=args.move_root or 'Shared'
            assert result['root']==expected_root and result['relay_path']==expected_root+'/file-relay'
            assert result['ledger_unchanged'] and result['connection_unchanged']
            assert dav.files['/dav/old-inbox/note.txt']==b'legacy-version'
            assert dav.files['/dav/Shared/file-relay/note.txt']==b'existing-canonical'
            assert any(p.startswith('/dav/Shared/config-backups/android/') for p in dav.files)
            shell('am','force-stop','net.lanbridge.android.debug')
            print(json.dumps({'android_ok':True,'root':result['root'],'relay_path':result['relay_path'],'configuration_restore_keeps_ledger':True,'instrumentation':output.strip()},ensure_ascii=False),flush=True)
            if args.android_only: return
        with tempfile.TemporaryDirectory(prefix='webdav-layout-') as temp:
            root=Path(temp);data=root/'data';data.mkdir()
            atomic_json(data/'webdav.json',{'url':f'http://127.0.0.1:{http.server_port}/dav/','username':'user','password_dpapi':protect('pass'),'remote_path':'Shared','include_secrets':False})
            atomic_json(data/'relay-connection.json',{'use_shared':True,'remote_path':'old-inbox','download_dir':str(root/'downloads')})
            app=App(data,register_live=False);app.ledger_close()
            try:
                config=app.call('webdav.get');relay=app.call('relay.get')
                assert config['service_paths']['relay']=='Shared/file-relay'
                assert relay['remote_path']=='Shared/file-relay' and relay['use_shared']
                result=finish(app,app.call('relay.test'))
                assert not result['migration'].get('pending'),result
                assert dav.files['/dav/old-inbox/note.txt']==b'legacy-version'
                assert dav.files['/dav/Shared/file-relay/note.txt']==b'existing-canonical'
                assert dav.files['/dav/Shared/file-relay/nested/receipt.txt']==b'nested-legacy'
                assert any(p.startswith('/dav/Shared/file-relay/legacy-') and b==b'legacy-version' for p,b in dav.files.items())
                app.call('settings.update',{'preferences':{'theme':'dark'}})
                entry=app.call('expenses.save',{'title':'Keep business data','date':'2026-09-26','amount':'12.30'})
                backup=finish(app,app.call('webdav.upload',{'include_secrets':False}))
                remote='/dav/Shared/config-backups/windows/'+backup['name']
                assert remote in dav.files
                with zipfile.ZipFile(io.BytesIO(dav.files[remote])) as z:
                    assert 'settings.json' in z.namelist()
                    assert not any(n.startswith(('expenses/','project-memory/','engine.sqlite3','media/')) for n in z.namelist())
                app.call('settings.update',{'preferences':{'theme':'light'}})
                changed=app.call('expenses.save',{'id':entry['id'],'revision':entry['revision'],'amount':'99.99'})
                restored=finish(app,app.call('webdav.restore',{'name':backup['name'],'location':'config'}))
                assert app.call('settings.get')['preferences']['theme']=='dark'
                assert app.call('expenses.get',{'id':entry['id']})['amount']=='99.99'
                assert Path(restored['recovery_path']).exists()
                # New canonical files must also migrate on a later root change.
                source=root/'new.txt';source.write_text('after-first-migration','utf-8')
                finish(app,app.call('relay.upload',{'paths':[str(source)],'path':'','move':False}))
                assert dav.files['/dav/Shared/file-relay/new.txt']==b'after-first-migration'
                app.call('webdav.save',{'remote_path':'Second'})
                result=finish(app,app.call('relay.test'))
                assert not result['migration'].get('pending'),result
                assert dav.files['/dav/Second/file-relay/new.txt']==b'after-first-migration'
                assert dav.files['/dav/Shared/file-relay/new.txt']==b'after-first-migration'
                assert app.call('webdav.get')['service_paths']['config_backups']=='Second/config-backups/windows'
                print(json.dumps({'ok':True,'checks':['one root','automatic children','legacy copy','collision retention','config-only backup','config restore preserves edited ledger','recovery copy','root change copies new canonical files'],'requests':len(dav.requests)},ensure_ascii=False))
            finally:
                app.close();app.jobs.pool.shutdown(wait=True)
    finally:
        http.shutdown();http.server_close();thread.join(3)


if __name__=='__main__': main()
