"""Documentation-only renderer: real frontend + isolated read-only backend.

No desktop interaction or cloud requests; Codex paths are temporary fixtures.
Never used by the production app.
"""
import json
import os
import sys
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'core'))
from toolbox.app import App
DATA=Path(os.environ.get('WINTOOLBOX_MANUAL_DATA',str(ROOT/'.build/manual-data')))
DATA.mkdir(parents=True,exist_ok=True)
CODEX=DATA/'codex-fixture'
CODEX.mkdir(exist_ok=True)
(CODEX/'config.toml').write_text('model = "示例模型"\nmodel_context_window = 272000\n',encoding='utf-8')
(CODEX/'models_cache.json').write_text(json.dumps({'models':[{'slug':'示例模型','display_name':'示例模型（仅演示）','context_window':272000,'max_context_window':872000}]},ensure_ascii=False),encoding='utf-8')
APP=App(DATA,register_live=False)
STATIC=ROOT/'desktop/dist'
BRIDGE=r'''<script>
window.__manualCaptionWindow={visible:new URLSearchParams(location.search).has('captions'),click_through:false};
window.__TAURI_INTERNALS__={
 metadata:{currentWebview:{label:'main'},currentWindow:{label:'main'}},
 transformCallback:()=>1,
 invoke:async(command,args={})=>{
  if(command==='plugin:event|listen')return 1;
  if(command==='plugin:event|unlisten')return null;
  if(command==='get_captions_window')return window.__manualCaptionWindow;
  if(command==='set_captions_window'){window.__manualCaptionWindow={visible:args.visible,click_through:false};return null;}
  if(command==='set_captions_click_through'){window.__manualCaptionWindow.click_through=args.enabled;return null;}
  if(command==='rpc'){
   const r=await fetch('/__manual_rpc',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(args)});
   const value=await r.json();if(!r.ok)throw new Error(value.error);return value;
  }
  if(command==='pick_files')return [];
  if(command==='pick_directory'||command==='pick_save')return null;
  return null;
 }
};window.__TAURI_EVENT_PLUGIN_INTERNALS__={unregisterListener:()=>{}};
</script>'''


class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_GET(self):
  name=urlsplit(self.path).path.lstrip('/') or 'index.html'
  path=(STATIC/name).resolve()
  if not path.is_relative_to(STATIC.resolve()) or not path.is_file():self.send_error(404);return
  data=path.read_bytes()
  if name=='index.html':data=data.replace(b'<head>',b'<head>'+BRIDGE.encode())
  kind={'.html':'text/html; charset=utf-8','.js':'text/javascript','.css':'text/css','.png':'image/png','.svg':'image/svg+xml'}.get(path.suffix,'application/octet-stream')
  self.send_response(200);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
 def do_POST(self):
  if self.path!='/__manual_rpc':self.send_error(404);return
  try:
   args=json.loads(self.rfile.read(min(int(self.headers.get('Content-Length','0')),1048576)))
   method=args['method'];params=args.get('params') or {}
   allowed={'app.info','settings.get','setup.get','setup.apply','models.list','jobs.list','plugins.list','knowledge.list','knowledge.documents','live.history','live.devices','codex.scan','codex.preview','codex.backups','captions.state','captions.configure'}
   if os.environ.get('WINTOOLBOX_CAPTION_FIXTURE')=='1':allowed.update({'captions.start','captions.stop'})
   if method not in allowed:raise ValueError('说明截图环境：此操作不执行。')
   if method.startswith('live.'):
    result={'devices':[],'sessions':[]}
   else:
    if method.startswith('codex.'):params={'home':str(CODEX),**params}
    result=APP.call(method,params)
   data=json.dumps(result,ensure_ascii=False).encode();self.send_response(200)
  except Exception as exc:data=json.dumps({'error':str(exc)},ensure_ascii=False).encode();self.send_response(400)
  self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)

if __name__=='__main__':
 server=ThreadingHTTPServer(('127.0.0.1',18763),Handler)
 print('Manual screenshot renderer: http://127.0.0.1:18763',flush=True)
 try:server.serve_forever()
 finally:APP.close()
