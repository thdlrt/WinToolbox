"""Exercise installed/portable Python and FFmpeg without developer PATH/packages."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

bundle=Path(sys.argv[1]).resolve()
assert (bundle/'python/python.exe').is_file()
with tempfile.TemporaryDirectory(prefix='wintoolbox-release-') as data:
    env=dict(os.environ,PATH=str(Path(os.environ['WINDIR'])/'System32'),PYTHONPATH=str(bundle/'core'),PYTHONUTF8='1',WINTOOLBOX_FFMPEG=str(bundle/'tools/ffmpeg.exe'),WINTOOLBOX_FFPROBE=str(bundle/'tools/ffprobe.exe'),WINTOOLBOX_TOOLS=str(bundle/'tools'))
    requests=[{'jsonrpc':'2.0','id':i+1,'method':method,'params':{}} for i,method in enumerate(('app.info','models.list','jobs.list','plugins.list','knowledge.list','live.history','live.devices','live.devices'))]
    result=subprocess.run([str(bundle/'python/python.exe'),'-u','-m','toolbox','--data-dir',data],input='\n'.join(json.dumps(r) for r in requests)+'\n',env=env,cwd=data,capture_output=True,encoding='utf-8',timeout=30)
    assert result.returncode==0,result.stderr
    assert 'com_loaded' not in result.stderr,result.stderr
    replies={r['id']:r for r in map(json.loads,result.stdout.splitlines()) if 'id' in r}
    for request in requests:
        assert 'result' in replies[request['id']],replies[request['id']]
    assert Path(replies[1]['result']['ffmpeg']).is_relative_to(bundle)
    generated=Path(data)/'tone.wav'
    generate=subprocess.run([str(bundle/'tools/ffmpeg.exe'),'-v','error','-f','lavfi','-i','sine=frequency=440:duration=0.1',str(generated)],env=env,cwd=data,capture_output=True,timeout=20)
    assert generate.returncode==0,generate.stderr
    check=subprocess.run([str(bundle/'tools/ffprobe.exe'),'-v','error','-show_format','-of','json',str(generated)],env=env,capture_output=True,text=True,timeout=20)
    assert check.returncode==0,check.stderr
    assert float(json.loads(check.stdout)['format']['duration'])>0
    print(json.dumps({'bundle':str(bundle),'isolated_rpc_methods':len(requests),'bundled_ffmpeg':True,'synthetic_media':True},ensure_ascii=False))
