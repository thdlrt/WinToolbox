"""Stage the unmodified, hash-pinned upstream Mem Reduct portable component."""
from pathlib import Path
import hashlib
import shutil
import subprocess
import httpx

root = Path(__file__).resolve().parents[1]
cache = root / '.build/memreduct'
cache.mkdir(parents=True, exist_ok=True)
archive = cache / 'memreduct-3.5.2-bin.7z'
digest = 'ec41ca15ff623f53850d9ee01986d3f8ff457ec93457a6ceeb993b7164023d15'
if not archive.exists():
    response = httpx.get('https://github.com/henrypp/memreduct/releases/download/v.3.5.2/memreduct-3.5.2-bin.7z', follow_redirects=True, timeout=60)
    response.raise_for_status()
    if len(response.content) > 2 * 1024 * 1024: raise RuntimeError('Mem Reduct archive too large')
    if hashlib.sha256(response.content).hexdigest() != digest: raise RuntimeError('Mem Reduct archive checksum mismatch')
    archive.write_bytes(response.content)
if hashlib.sha256(archive.read_bytes()).hexdigest() != digest: raise RuntimeError('Mem Reduct archive checksum mismatch')
subprocess.run(['7z.exe', 'x', str(archive), '-o' + str(cache / 'unpacked'), '-y'], check=True, capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
source = cache / 'unpacked/memreduct/64'
if hashlib.sha256((source / 'memreduct.exe').read_bytes()).hexdigest() != 'dd55a81d56e8c32918e5190b2ec2a1f2a7edbbca884627439b4b619fdfb5602d': raise RuntimeError('Mem Reduct executable checksum mismatch')
target = root / 'desktop/src-tauri/resources/tools/memreduct'
shutil.copytree(source, target, dirs_exist_ok=True)
(target / 'SOURCE.txt').write_text('Unmodified Mem Reduct 3.5.2, separate executable, GPL-3.0-or-later.\nUpstream release and hashes: https://github.com/henrypp/memreduct/releases/tag/v.3.5.2\nCorresponding upstream source: https://github.com/henrypp/memreduct/tree/v.3.5.2\nLicense: License.txt\n', encoding='utf-8')
print('Mem Reduct 3.5.2 ready (upstream SHA-256 verified)')
