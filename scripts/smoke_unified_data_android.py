"""Bidirectional ledger/attachment sync against a synthetic Android WebDAV server."""
import argparse,importlib.util,json,subprocess,shlex,tempfile,threading,time,uuid
from pathlib import Path
from toolbox.app import App
p=argparse.ArgumentParser();p.add_argument('--android-root',type=Path,required=True);p.add_argument('--serial',required=True);a=p.parse_args()
assert a.serial.startswith('emulator-')
adb=a.android_root/'.build/sdk/platform-tools/adb.exe'
def shell(*args):
 r=subprocess.run([str(adb),'-s',a.serial,'shell',shlex.join(args)],capture_output=True,timeout=150);assert r.returncode==0,r.stderr;return r.stdout.decode('utf-8','replace')
assert shell('getprop','ro.kernel.qemu').strip()=='1'
# The explicitly selected emulator's debug application contains synthetic fixtures only.
assert 'Success' in shell('pm','clear','net.lanbridge.android.debug')
spec=importlib.util.spec_from_file_location('sync_dav',a.android_root/'scripts/relay-test-server.py');dav=importlib.util.module_from_spec(spec);spec.loader.exec_module(dav)
server=dav.ThreadingHTTPServer(('127.0.0.1',0),dav.Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
def finish(app,job):
 for _ in range(2000):
  row=app.jobs.get(job['id'])
  if row['status'] not in ('queued','running','cancelling') and job['id'] not in app.jobs.active:
   assert row['status']=='completed',row;return row['result']
  time.sleep(.02)
 raise AssertionError('job timeout')
try:
 with tempfile.TemporaryDirectory(prefix='sync-acceptance-') as directory:
  app=App(Path(directory),register_live=False);app.data_sync.stop_worker()
  try:
   app.call('webdav.save',{'url':f'http://127.0.0.1:{server.server_port}/dav/','username':'user','password':'pass','remote_path':'UnifiedDataFixture'})
   receipt=Path(directory)/'receipt.pdf';receipt.write_bytes(b'%PDF-1.7\nsynthetic receipt')
   entry=finish(app,app.call('expenses.commit',{'request_id':str(uuid.uuid4()),'title':'PC unified sync fixture','date':'2026-09-27','amount':'8.88','attachments_add':[{'path':str(receipt),'kind':'invoice'}]}))['entry']
   finish(app,app.call('expenses.sync'))
   output=shell('am','instrument','-w','-e','action','seed','-e','title','Android unified sync fixture','-e','endpoint',f'http://10.0.2.2:{server.server_port}/dav/','-e','remote_path','UnifiedDataFixture','-e','username','user','-e','password','pass','net.lanbridge.android.debug.test/net.lanbridge.android.NavigationInstrumentation')
   assert 'PASS:' in output,output
   mobile=json.loads(shell('run-as','net.lanbridge.android.debug','cat','files/ledger-test-result.json'))
   received=next(e for e in mobile['entries'] if e['id']==entry['id']);assert any(k.startswith('attachment:') for k in received)
   finish(app,app.call('expenses.sync'))
   rows=app.call('expenses.list',{'start_date':'2026-09-01','end_date':'2026-09-30','project_id':'all'})['items']
   received=next(e for e in rows if e['title']=='Android unified sync fixture');assert received['attachments']
   path=app.call('expenses.attachment',{'id':received['id'],'attachment_id':received['attachments'][0]['id']})['path'];assert Path(path).is_file()
   print(json.dumps({'ok':True,'android':output.strip(),'pc_to_android_attachment':True,'android_to_pc_attachment':True,'requests':len(dav.requests)},ensure_ascii=False))
  finally:app.close();app.jobs.pool.shutdown(wait=True)
finally:server.shutdown();server.server_close();thread.join(3)
