import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
import pytest

from toolbox.settings import Settings
from toolbox.storage import Storage


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_removing_provider_removes_its_secret(tmp_path):
    settings=Settings(tmp_path)
    settings.update({'providers':[{'id':'fixture','name':'Fixture','kind':'openai','base_url':'http://127.0.0.1:1234/v1','api_key':'fixture-key'}]})
    assert settings.secret('fixture')=='fixture-key'
    settings.update({'providers':[]})
    assert settings.secret('fixture')==''
    assert settings.export_secrets()=={}


def test_future_schema_is_not_silently_downgraded(tmp_path):
    with sqlite3.connect(tmp_path/'engine.sqlite3') as db:
        db.execute('PRAGMA user_version=99')
    with pytest.raises(RuntimeError,match='较新版本'):
        Storage(tmp_path)
    with sqlite3.connect(tmp_path/'engine.sqlite3') as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==99


def test_rpc_subprocess_clean_stdout_and_errors(tmp_path):
    requests=[{'jsonrpc':'2.0','id':1,'method':'app.info','params':{}},
              {'jsonrpc':'2.0','id':2,'method':'models.list','params':{}},
              {'jsonrpc':'2.0','id':3,'method':'invalid.method','params':{}}]
    env=dict(os.environ,PYTHONUTF8='1')
    core=Path(__file__).resolve().parents[1]/'core'
    env['PYTHONPATH']=str(core)
    process=subprocess.run([sys.executable,'-u','-m','toolbox','--data-dir',str(tmp_path)],input='\n'.join(json.dumps(x) for x in requests)+'\n',capture_output=True,text=True,encoding='utf-8',env=env,timeout=30)
    assert process.returncode==0,process.stderr
    replies={v['id']:v for v in map(json.loads,process.stdout.splitlines()) if 'id' in v}
    assert replies[1]['result']['name']=='WinToolbox'
    assert any(m['id']=='libreoffice' for m in replies[2]['result']['models'])
    assert replies[3]['error']['code']==-32601
