"""Stage the pinned official ADB runtime for the portable Windows build."""
import hashlib
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = '37.0.1'
URL = 'https://dl.google.com/android/repository/platform-tools_r37.0.1-win.zip'
SHA256 = '45f4d63113e895ebde0c90f194099a4676b6ac653bd28d54314a9e022bbc1a99'
FILES = ('adb.exe', 'AdbWinApi.dll', 'AdbWinUsbApi.dll', 'libwinpthread-1.dll', 'NOTICE.txt', 'source.properties')


def stage():
    target = ROOT / 'desktop/src-tauri/resources/tools/adb'
    archive = ROOT / '.build/android-platform-tools.zip'
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        with urllib.request.urlopen(URL, timeout=60) as response:
            data = response.read(64 * 1024 * 1024 + 1)
        if len(data) > 64 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != SHA256:
            raise ValueError('ADB 下载校验失败，请重试或检查固定版本下载源')
        temporary = archive.with_suffix('.download')
        temporary.write_bytes(data)
        temporary.replace(archive)
    target.mkdir(parents=True, exist_ok=True)
    hashes = {}
    with zipfile.ZipFile(archive) as package:
        for name in FILES:
            info = package.getinfo('platform-tools/' + name)
            if info.file_size > 32 * 1024 * 1024:
                raise ValueError('ADB 压缩包文件大小无效')
            data = package.read(info)
            destination = target / name
            digest = hashlib.sha256(data).hexdigest()
            hashes[name] = digest
            if not destination.exists() or hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                destination.write_bytes(data)
    (target / 'SOURCE.json').write_text(json.dumps({
        'version': VERSION, 'url': URL, 'archive_sha256': SHA256, 'files': hashes,
        'documentation': 'https://developer.android.com/tools/releases/platform-tools',
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print('ADB ' + VERSION + ' 已准备：' + str(target))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    try:
        stage()
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
