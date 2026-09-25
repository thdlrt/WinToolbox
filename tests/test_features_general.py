import json
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from toolbox.features import general


def release(tag='v0.2.0'):
    name=f'WinToolbox-{tag[1:]}-portable.zip'
    return {'tag_name':tag,'assets':[{'name':name,'size':10,'digest':'sha256:'+'a'*64,'browser_download_url':f'{general.REPOSITORY}/releases/download/{tag}/{name}'}]}


def test_numeric_versions_and_no_downgrade():
    assert general.version('v0.1.10') > general.version('0.1.9')
    assert general.release_info(release(),True,'0.1.21')['available']
    assert not general.release_info(release(),True,'0.2.0')['available']
    assert not general.release_info(release(),True,'0.3.0')['available']
    with pytest.raises(ValueError): general.version('v0.2.0-rc1')


def test_reject_wrong_assets_sources_and_unverified_packages():
    data=release()
    with pytest.raises(ValueError): general.release_info(data,False)
    data['assets'][0]['browser_download_url']='https://example.com/evil.zip'
    with pytest.raises(ValueError): general.release_info(data,True)
    data=release();data['assets'][0]['digest']=None
    with pytest.raises(ValueError): general.release_info(data,True)


@pytest.mark.parametrize('name',['WinToolbox-portable/data/secret','WinToolbox-portable/../escape','WinToolbox-portable/core/C:/escape','/absolute'])
def test_archive_rejects_data_and_traversal(tmp_path,name):
    package=tmp_path/'bad.zip'
    with zipfile.ZipFile(package,'w') as z:z.writestr(name,'bad')
    with pytest.raises(ValueError):general.unpack(package,tmp_path/'stage')
    assert not (tmp_path/'escape').exists()


def test_archive_requires_complete_runtime(tmp_path):
    package=tmp_path/'good.zip'
    with zipfile.ZipFile(package,'w') as z:
        for name in ['WinToolbox.exe','portable.flag','python/python.exe','core/toolbox/__main__.py']:
            z.writestr('WinToolbox-portable/'+name,'new')
    general.unpack(package,tmp_path/'stage')
    assert (tmp_path/'stage/core/toolbox/__main__.py').read_text()=='new'


@pytest.mark.parametrize('fail',[False,True])
def test_windows_update_and_rollback_preserve_data(tmp_path,fail):
    root=tmp_path/"用户's app";root.mkdir()
    folder=root/'data/updates/id';folder.mkdir(parents=True)
    (root/'data/records.txt').write_text('user data')
    for name in ['WinToolbox.exe','portable.flag','core']:
        (root/name).write_text('old '+name)
        (folder/'stage').mkdir(exist_ok=True)
        (folder/'stage'/name).write_text('new '+name)
    script=Path(general.install_script(folder,root/'WinToolbox.exe',True,999999999,folder/'p.zip'))
    text=script.read_text('utf-8-sig')
    # Run the real file-move code with no app launch. Inject one move failure to test rollback.
    text="function Start-Process { param($FilePath,$WorkingDirectory) }\n"+text
    if fail:text=text.replace('  $taskTarget = Join-Path $taskRoot $taskPart',"  if ($taskPart -eq 'core') { throw 'injected lock' }\n  $taskTarget = Join-Path $taskRoot $taskPart")
    script.write_text(text,'utf-8-sig')
    result=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(script)],capture_output=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert (root/'data/records.txt').read_text()=='user data'
    assert (root/'WinToolbox.exe').read_text()==('old WinToolbox.exe' if fail else 'new WinToolbox.exe')
    assert (root/'core').read_text()==('old core' if fail else 'new core')


def test_startup_uses_only_own_user_value(tmp_path,monkeypatch):
    import sys
    exe=tmp_path/'工具箱.exe';exe.touch()
    monkeypatch.setenv('WINTOOLBOX_APP_EXE',str(exe))
    values={'OtherApp':'keep'}
    class Key:
        def __enter__(self):return self
        def __exit__(self,*args):pass
    def read(key,name):
        if name not in values:raise FileNotFoundError()
        return values[name],1
    def open_key(hive,key,*args):
        if key!=general.RUN_KEY:raise FileNotFoundError()
        return Key()
    fake=SimpleNamespace(HKEY_CURRENT_USER=1,KEY_SET_VALUE=2,REG_SZ=1,OpenKey=open_key,CreateKeyEx=open_key,QueryValueEx=read,
      SetValueEx=lambda k,n,r,t,v:values.__setitem__(n,v),DeleteValue=lambda k,n:values.pop(n))
    monkeypatch.setitem(sys.modules,'winreg',fake)
    assert not general.startup()['enabled']
    assert general.startup(True)['enabled']
    assert values['WinToolbox']=='"'+str(exe.resolve())+'"'
    assert not general.startup(False)['enabled']
    assert values=={'OtherApp':'keep'}

@pytest.mark.parametrize('corrupt',[False,True])
def test_download_verifies_before_offering_install(tmp_path,monkeypatch,corrupt):
    import hashlib
    package=tmp_path/'fixture.zip'
    with zipfile.ZipFile(package,'w') as z:
        for name in ['WinToolbox.exe','portable.flag','python/python.exe','core/toolbox/__main__.py']:
            z.writestr('WinToolbox-portable/'+name,'new')
    content=package.read_bytes()
    info={'available':True,'version':'0.2.0','portable':True,'name':'update.zip','url':'https://fixture',
          'sha256':('0'*64 if corrupt else hashlib.sha256(content).hexdigest()),'size':len(content)}
    monkeypatch.setattr(general,'check',lambda:info)
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def raise_for_status(self):pass
        def iter_bytes(self,size):yield content
    class Client(Response):
        def __init__(self,*args,**kwargs):pass
        def stream(self,*args):return Response()
    monkeypatch.setattr(general.httpx,'Client',Client)
    runners={}
    app=SimpleNamespace(data_dir=tmp_path/'data',register=lambda *a:None,jobs=SimpleNamespace(register=lambda n,f:runners.__setitem__(n,f)))
    general.register(app)
    job=SimpleNamespace(params={'version':'0.2.0'},check_cancelled=lambda:None,progress=lambda *a:None)
    if corrupt:
        with pytest.raises(ValueError,match='校验失败'):runners['updates.download'](job)
        assert not list(app.data_dir.rglob('verified.json'))
    else:
        result=runners['updates.download'](job)
        assert result['ready']
        assert (Path(result['folder'])/'stage/core/toolbox/__main__.py').is_file()
