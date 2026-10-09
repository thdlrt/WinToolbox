"""Stage the unmodified, pinned upstream NCM converter."""
import hashlib
import json
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = '1.5.1'
URL = f'https://github.com/taurusxin/ncmdump/releases/download/{VERSION}/ncmdump-{VERSION}-windows-amd64.zip'
SHA256 = 'bb849221c06b8fdbff42aefb86baea9c07256568658d80f4be72a39a2a1632dc'


def stage():
    archive = ROOT / '.build/ncmdump-1.5.1.zip'
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.is_file() or hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        with urllib.request.urlopen(URL, timeout=45) as response:
            data = response.read(4 * 1024**2 + 1)
        if hashlib.sha256(data).hexdigest() != SHA256:
            raise ValueError('ncmdump 下载 SHA-256 校验失败')
        archive.write_bytes(data)
    target = ROOT / 'desktop/src-tauri/resources/tools/ncmdump'
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as package:
        items = [item for item in package.infolist() if Path(item.filename).name == 'ncmdump.exe']
        if len(items) != 1:
            raise ValueError('ncmdump 发布包结构不符')
        data = package.read(items[0])
        (target / 'ncmdump.exe').write_bytes(data)
    license_url = f'https://raw.githubusercontent.com/taurusxin/ncmdump/{VERSION}/LICENSE.txt'
    if not (target / 'LICENSE.txt').is_file():
        with urllib.request.urlopen(license_url, timeout=30) as response:
            (target / 'LICENSE.txt').write_bytes(response.read(65536))
    (target / 'SOURCE.json').write_text(json.dumps({'version': VERSION, 'url': URL,
        'archive_sha256': SHA256, 'executable_sha256': hashlib.sha256(data).hexdigest(),
        'source': 'https://github.com/taurusxin/ncmdump/tree/1.5.1', 'license': 'MIT'}, indent=2), encoding='utf-8')
    print(f'ncmdump {VERSION} 已准备：{target}')


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    stage()
