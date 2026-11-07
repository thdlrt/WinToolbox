"""Stage the pinned Windows mount engine (rclone) for the netmount feature.

Downloads the exact rclone release once, verifies its SHA-256, and copies
rclone.exe into both `.build/netmount` (development) and the Tauri resources
`tools/netmount` directory that package.ps1 ships inside the portable app.
"""
from pathlib import Path
import hashlib
import shutil
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / '.build' / 'netmount'
ZIP = CACHE / 'rclone-v1.75.2-windows-amd64.zip'
URL = 'https://github.com/rclone/rclone/releases/download/v1.75.2/rclone-v1.75.2-windows-amd64.zip'
SHA = '8a9db2bad1dc79a65b3348fd857069598e2aab7cf4d118b77b1df23055aff818'
EXE = 'rclone-v1.75.2-windows-amd64/rclone.exe'


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    if not ZIP.exists():
        urllib.request.urlretrieve(URL, ZIP)
    if hashlib.sha256(ZIP.read_bytes()).hexdigest() != SHA:
        raise RuntimeError('rclone SHA256 mismatch')
    with zipfile.ZipFile(ZIP) as archive:
        archive.extract(EXE, CACHE)
    extracted = CACHE / EXE
    target = ROOT / 'desktop/src-tauri/resources/tools/netmount'
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(extracted, target / 'rclone.exe')
    shutil.copy2(extracted, CACHE / 'rclone.exe')
    license_file = ROOT / 'docs' / 'NETMOUNT-RCLONE-LICENSE.txt'
    if license_file.exists():
        shutil.copy2(license_file, target / 'LICENSE.txt')
    with zipfile.ZipFile(ZIP) as archive:
        for name in ('rclone-v1.75.2-windows-amd64/README.txt', 'rclone-v1.75.2-windows-amd64/git-log.txt'):
            try:
                (target / Path(name).name).write_bytes(archive.read(name))
            except KeyError:
                pass
    print('netmount engine ready:', target / 'rclone.exe')


if __name__ == '__main__':
    main()
