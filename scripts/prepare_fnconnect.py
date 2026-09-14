"""Stage the pinned, user-requested Windows TUN engine and watchdog."""
from pathlib import Path
import hashlib
import shutil
import subprocess
import sys
import json
import urllib.request
import zipfile
ROOT=Path(__file__).resolve().parents[1]
CACHE=ROOT/'.build'/'fnconnect'
ZIP=ROOT/'.build'/'mihomo-1.19.30.zip'
URL='https://github.com/MetaCubeX/mihomo/releases/download/v1.19.30/mihomo-windows-amd64-v1.19.30.zip'
SHA='22c09fd67673895ef7cd6b1820563918275c3d316f2462b306208675118db3c0'
def main():
    CACHE.mkdir(parents=True,exist_ok=True)
    if not ZIP.exists(): urllib.request.urlretrieve(URL,ZIP)
    if hashlib.sha256(ZIP.read_bytes()).hexdigest()!=SHA: raise RuntimeError('Mihomo SHA256 mismatch')
    with zipfile.ZipFile(ZIP) as z:
        data=z.read('mihomo-windows-amd64.exe')
    (CACHE/'mihomo-windows-amd64.exe').write_bytes(data)
    target=ROOT/'desktop/src-tauri/resources/tools/fnconnect'
    target.mkdir(parents=True,exist_ok=True)
    shutil.copy2(CACHE/'mihomo-windows-amd64.exe',target/'mihomo-windows-amd64.exe')
    shutil.copy2(ROOT/'scripts/run-fnconnect-tun.ps1',target/'run-tun.ps1')
    shutil.copy2(ROOT/'scripts/run-fnconnect-tun.ps1',CACHE/'run-tun.ps1')
    compiler=Path('C:/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe')
    subprocess.run([str(compiler),'/nologo','/target:exe','/platform:x64','/r:System.ServiceProcess.dll','/r:System.Web.Extensions.dll','/out:'+str(CACHE/'WinToolboxTun.exe'),str(ROOT/'scripts/fnconnect-service/Service.cs')],check=True)
    sys.path.insert(0,str(ROOT/'core'))
    from toolbox.features.fnconnect_tun import tun_config
    (CACHE/'tun-template.json').write_text(json.dumps(tun_config('https://example.fnos.net','lan',transport_ips=['203.0.113.8'])),encoding='utf-8')
    shutil.copy2(ROOT/'scripts/install-fnconnect-service.ps1',CACHE/'install-service.ps1')
    for filename in ('WinToolboxTun.exe','tun-template.json','install-service.ps1'):
        shutil.copy2(CACHE/filename,target/filename)
    license_file=ROOT/'docs'/'MIHOMO-LICENSE.txt'
    if license_file.exists():
        shutil.copy2(license_file,target/'LICENSE.txt')
        shutil.copy2(license_file,CACHE/'LICENSE.txt')
if __name__=='__main__': main()
