"""Real PC/Android configuration exchange against an isolated WebDAV fixture."""
import argparse, importlib.util, json, subprocess, shlex, tempfile, threading, time
from pathlib import Path
from toolbox.app import App
from toolbox import config_snapshots

parser=argparse.ArgumentParser();parser.add_argument('--android-root',type=Path,required=True);parser.add_argument('--serial',required=True);args=parser.parse_args()
if not args.serial.startswith('emulator-'):raise ValueError('Explicit SDK emulator required')
adb=args.android_root/'.build/sdk/platform-tools/adb.exe'
def command(*parts):
    if parts[0]=='shell':parts=('shell',shlex.join(parts[1:]))
    result=subprocess.run([str(adb),'-s',args.serial,*parts],capture_output=True,timeout=120)
    if result.returncode:raise RuntimeError(result.stderr.decode('utf-8','replace'))
    return result.stdout.decode('utf-8','replace')
assert command('shell','getprop','ro.kernel.qemu').strip()=='1'
spec=importlib.util.spec_from_file_location('config_fixture',args.android_root/'scripts/relay-test-server.py');dav=importlib.util.module_from_spec(spec);spec.loader.exec_module(dav)
server=dav.ThreadingHTTPServer(('127.0.0.1',0),dav.Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
def finish(app,job):
    deadline=time.monotonic()+60
    while time.monotonic()<deadline:
        row=app.jobs.get(job['id'])
        if row['status'] not in ('running','queued','cancelling') and job['id'] not in app.jobs.active:
            assert row['status']=='completed',row
            return row['result']
        time.sleep(.03)
    raise AssertionError('PC job timeout')
def android(mode,filename=None):
    parts=['shell','am','instrument','-w','-e','action','config_backup_exchange','-e','mode',mode,'-e','url',f'http://10.0.2.2:{server.server_port}/dav/','-e','root','ConfigFixture','-e','user','user','-e','password','pass']
    if filename:parts+=['-e','filename',filename]
    output=command(*parts,'net.lanbridge.android.debug.test/net.lanbridge.android.NavigationInstrumentation')
    assert 'PASS' in output,output
    return output.strip()
try:
    with tempfile.TemporaryDirectory(prefix='unified-config-') as directory:
        app=App(Path(directory),register_live=False);app.data_sync.stop_worker()
        try:
            app.call('webdav.save',{'url':f'http://127.0.0.1:{server.server_port}/dav/','username':'user','password':'pass','remote_path':'ConfigFixture'})
            providers=app.settings.get()['providers']+[{'id':'fixture','name':'PC fixture','kind':'openai','base_url':'https://pc.invalid/v1','api_key':'pc-fixture-key'}]
            app.call('settings.update',{'providers':providers,'roles':{'chat':{'provider_id':'fixture','model':'pc-fixture-model'}},'preferences':{'theme':'dark'}})
            uploaded=finish(app,app.call('webdav.upload'))
            assert b'pc-fixture-key' not in dav.files['/dav/ConfigFixture/config-backups/shared/'+uploaded['name']]
            restored=android('restore',uploaded['name'])
            sent=android('upload')
            rows=app.call('webdav.list')['snapshots'];mobile=next(row for row in rows if row.get('source_platform')=='android')
            preview=app.call('webdav.preview',{'name':mobile['name'],'location':'unified'})
            assert not preview['same_platform'] and 'android-fixture-key' not in json.dumps(preview)
            finish(app,app.call('webdav.restore',{'name':mobile['name'],'location':'unified','preview_token':preview['token']}))
            assert app.settings.secret('android-fixture')=='android-fixture-key'
            assert app.settings.value['roles']['chat']['model']=='android-fixture-model'
            assert app.settings.value['preferences']['theme']=='dark'
            assert 'transcribe' in app.settings.value['roles']
            print(json.dumps({'ok':True,'android_restore':restored,'android_upload':sent,'pc_key_and_role_restore':True,'platform_preferences_preserved':True,'requests':len(dav.requests)},ensure_ascii=False))
        finally:app.close();app.jobs.pool.shutdown(wait=True)
finally:server.shutdown();server.server_close();thread.join(3)
