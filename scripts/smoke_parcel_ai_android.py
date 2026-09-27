"""Synthetic SMS/provider + model API + encrypted PC/Android profile acceptance."""
import argparse, importlib.util, json, subprocess, shlex, tempfile, threading, time
from pathlib import Path
from urllib.parse import urlsplit,unquote
from toolbox.app import App
from toolbox import ai_config

p=argparse.ArgumentParser();p.add_argument('--android-root',type=Path,required=True);p.add_argument('--serial',required=True);a=p.parse_args()
if not a.serial.startswith('emulator-'):raise ValueError('Explicit SDK emulator required')
adb=a.android_root/'.build/sdk/platform-tools/adb.exe'
def command(*parts,timeout=150):
    if parts[0]=='shell':parts=('shell',shlex.join(parts[1:]))
    result=subprocess.run([str(adb),'-s',a.serial,*parts],capture_output=True,timeout=timeout)
    if result.returncode:raise RuntimeError(result.stderr.decode('utf-8','replace'))
    return result.stdout.decode('utf-8','replace')
assert command('shell','getprop','ro.kernel.qemu').strip()=='1'
spec=importlib.util.spec_from_file_location('parcel_dav',a.android_root/'scripts/relay-test-server.py');dav=importlib.util.module_from_spec(spec);spec.loader.exec_module(dav)
model_calls=[]
class Handler(dav.Handler):
    def handle_request(self):
        body=self.rfile.read(int(self.headers.get('Content-Length','0')));path=unquote(urlsplit(self.path).path)
        if self.command=='POST' and path=='/v1/chat/completions':
            assert self.headers.get('Authorization')=='Bearer fixture-mobile-key'
            request=json.loads(body);model_calls.append(request['model']);sources=json.loads(request['messages'][1]['content'])['sources'];items=[]
            for source in sources:
                first='FIRST' in source['body'];items.append({'source_ids':[source['id']],'code':'1-1234' if first else '2-9876','tracking_number':'PKG0001' if first else 'PKG0002','carrier':'Fixture courier','location':'North' if first else 'South','locker':'Fixture locker','status':'待取','deadline':None})
            output=json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'items':items},ensure_ascii=False)}}]},ensure_ascii=False).encode();code=200
        elif self.command=='PUT' and self.headers.get('If-Match') and self.headers['If-Match']!=dav.etag(path):code,output=412,b''
        else:code,output=self.response(body)
        self.send_response(code);self.send_header('Content-Length',str(len(output)))
        if path in dav.files:self.send_header('ETag',dav.etag(path))
        self.end_headers();self.wfile.write(output)
    do_PROPFIND=do_MKCOL=do_PUT=do_MOVE=do_GET=do_DELETE=do_POST=handle_request
server=dav.ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
def finish(app,job):
    deadline=time.monotonic()+60
    while time.monotonic()<deadline:
        row=app.jobs.get(job['id'])
        if row['status'] not in ('running','queued','cancelling') and job['id'] not in app.jobs.active:
            assert row['status']=='completed',row
            return row['result']
        time.sleep(.03)
    raise AssertionError('PC job timeout')
try:
    with tempfile.TemporaryDirectory(prefix='parcel-ai-') as directory:
        app=App(Path(directory),register_live=False);app.ledger_close()
        try:
            endpoint=f'http://127.0.0.1:{server.server_port}'
            app.call('webdav.save',{'url':endpoint+'/dav/','username':'user','password':'pass','remote_path':'AiFixture'})
            existing=app.settings.get()['providers'];existing.append({'id':'pc-fixture','name':'PC fixture','kind':'openai','base_url':'https://example.invalid/v1','api_key':'fixture-pc-key'})
            app.call('settings.update',{'providers':existing,'roles':{'chat':{'provider_id':'pc-fixture','model':'pc-fixture-model'}},'preferences':{'theme':'dark'}})
            finish(app,app.call('ai.config.upload'))
            cipher=dav.files['/dav/AiFixture/ai-config/config-v1.json'];assert b'fixture-pc-key' not in cipher
            command('shell','pm','grant','net.lanbridge.android.debug','android.permission.READ_SMS')
            command('emu','sms','send','+861069001','PARCEL_FIXTURE_FIRST pickup code 1-1234 at North locker tracking PKG0001')
            command('emu','sms','send','+861069002','PARCEL_FIXTURE_SECOND pickup code 2-9876 at South locker tracking PKG0002')
            time.sleep(2)
            output=command('shell','am','instrument','-w','-e','endpoint',f'http://10.0.2.2:{server.server_port}','net.lanbridge.android.debug.test/net.lanbridge.android.AiParcelIntegration')
            assert 'PASS:' in output,output
            preview=finish(app,app.call('ai.config.download'))
            assert 'fixture-mobile-key' not in json.dumps(preview)
            applied=finish(app,app.call('ai.config.download',{'confirmation_token':preview['confirmation_token'],'confirm_replace':True}))
            assert applied['applied'] and app.settings.secret('mobile-fixture')=='fixture-mobile-key'
            assert app.settings.value['roles']['parcel_vision']=={'provider_id':'vision-fixture','model':'vision-fixture-model'}
            assert app.settings.value['preferences']['theme']=='dark'
            payload=ai_config.decrypt(json.loads(dav.files['/dav/AiFixture/ai-config/config-v1.json']),'pass')
            assert payload['secrets']['vision-fixture']=='fixture-vision-key'
            assert model_calls==['parcel-fixture-model']
            print(json.dumps({'ok':True,'android':output.strip(),'model_requests':len(model_calls),'bidirectional_encrypted_keys':True,'pc_preferences_preserved':True,'dav_requests':len(dav.requests)},ensure_ascii=False))
        finally:app.close();app.jobs.pool.shutdown(wait=True)
finally:server.shutdown();server.server_close();thread.join(3)
