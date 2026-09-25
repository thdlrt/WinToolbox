"""GitHub release downloads and per-user Windows startup settings."""
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
import zipfile
from pathlib import Path, PurePosixPath

import httpx
from toolbox import __version__

REPOSITORY = 'https://github.com/thdlrt/WinToolbox'
API = 'https://api.github.com/repos/thdlrt/WinToolbox/releases/latest'
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'


def version(value):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', value)
    if not match:
        raise ValueError('发布版本号无效，仅支持正式版本。')
    return tuple(map(int, match.groups()))


def executable():
    path = Path(os.environ.get('WINTOOLBOX_APP_EXE', ''))
    if not path.is_absolute() or not path.is_file():
        raise ValueError('请在已打包的 WinToolbox 桌面程序中操作。')
    return path.resolve()


def startup(enabled=None):
    import winreg
    exe = executable()
    command = '"' + str(exe) + '"'
    if enabled is not None:
        if not isinstance(enabled, bool):
            raise ValueError('自启设置必须为布尔值')
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, 'WinToolbox', 0, winreg.REG_SZ, command)
            else:
                try: winreg.DeleteValue(key, 'WinToolbox')
                except FileNotFoundError: pass
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            actual = winreg.QueryValueEx(key, 'WinToolbox')[0]
    except FileNotFoundError:
        actual = ''
    disabled = False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run') as key:
            value = winreg.QueryValueEx(key, 'WinToolbox')[0]
            disabled = bool(value and value[0] == 3)
    except FileNotFoundError: pass
    return {'enabled': actual == command, 'disabled_by_windows': disabled, 'path': str(exe)}


def release_info(data, portable, current=__version__):
    tag = data.get('tag_name', '')
    newer = version(tag) > version(current)
    if data.get('draft') or data.get('prerelease'):
        raise ValueError('未找到正式发布版本。')
    number = tag.lstrip('v')
    name = f'WinToolbox-{number}-portable.zip' if portable else f'WinToolbox_{number}_x64-setup.exe'
    asset = next((a for a in data.get('assets', []) if a.get('name') == name), None)
    if not asset:
        raise ValueError('该版本缺少适合当前安装方式的 Windows 更新包。')
    url = asset.get('browser_download_url', '')
    if url != f'{REPOSITORY}/releases/download/{tag}/{name}':
        raise ValueError('发布包地址不属于指定 GitHub 仓库。')
    digest = asset.get('digest') or ''
    if not re.fullmatch(r'sha256:[a-fA-F0-9]{64}', digest):
        raise ValueError('发布包缺少 SHA-256 校验值，无法安全下载更新。')
    return {'current': current, 'version': number, 'available': newer, 'url': url,
            'name': name, 'sha256': digest[7:].lower(), 'size': asset['size'],
            'portable': portable, 'notes': str(data.get('body') or '')[:20000],
            'release_url': f'{REPOSITORY}/releases/tag/{tag}'}


def check():
    portable = (executable().parent / 'portable.flag').is_file()
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        response = client.get(API, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'WinToolbox'})
        if response.status_code == 404:
            raise ValueError('GitHub 尚无可用的正式发布版本。')
        if response.status_code == 403:
            raise ValueError('GitHub 暂时限制请求，请稍后再试。')
        response.raise_for_status()
        return release_info(response.json(), portable)


def unpack(package, destination):
    """Only ship-owned parts; data, arbitrary paths and links never enter staging."""
    allowed = {'WinToolbox.exe', 'portable.flag', 'python', 'core', 'tools', 'docs', '使用说明.md'}
    with zipfile.ZipFile(package) as archive:
        total = 0
        for item in archive.infolist():
            path = PurePosixPath(item.filename)
            if '\\' in item.filename or ':' in item.filename or '..' in path.parts or path.is_absolute():
                raise ValueError('更新包包含非法路径。')
            parts = path.parts
            if not parts or parts[0] != 'WinToolbox-portable':
                raise ValueError('更新包目录结构不正确。')
            if len(parts) == 1: continue
            if parts[1] not in allowed or stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError('更新包包含非程序文件或链接。')
            total += item.file_size
            if total > 8 * 1024**3:
                raise ValueError('更新包解压大小超出限制。')
            target = destination.joinpath(*parts[1:])
            if item.is_dir(): target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as src, target.open('wb') as out:
                    shutil.copyfileobj(src, out)
    for required in ('WinToolbox.exe', 'portable.flag', 'python/python.exe', 'core/toolbox/__main__.py'):
        if not (destination / required).is_file():
            raise ValueError('更新包不完整：' + required)


def psquote(value):
    return "'" + str(value).replace("'", "''") + "'"


def install_script(folder, exe, portable, pid, package):
    # Static program-owned roots only. No data directory participates in replacement.
    root = exe.parent
    backup = folder / 'previous'
    stage = folder / 'stage'
    lines = ["$ErrorActionPreference = 'Stop'", f'$taskPid = {int(pid)}',
             'Wait-Process -Id $taskPid -ErrorAction SilentlyContinue',
             f'$taskRoot = {psquote(root)}', f'$taskStage = {psquote(stage)}',
             f'$taskBackup = {psquote(backup)}', f'$taskExe = {psquote(exe)}',
             f'$taskLog = {psquote(folder / "install-result.txt")}']
    if not portable:
        lines += [f'Start-Process -FilePath {psquote(package)}', "'安装程序已打开' | Set-Content -LiteralPath $taskLog -Encoding utf8"]
    else:
        lines += ["$taskParts = @('WinToolbox.exe','portable.flag','python','core','tools','docs','使用说明.md')",
                  'New-Item -ItemType Directory -Path $taskBackup -Force | Out-Null',
                  '$taskMoved = @(); $taskInstalled = @()', 'try {',
                  ' foreach ($taskPart in $taskParts) {',
                  '  $taskSource = Join-Path $taskStage $taskPart',
                  '  if (!(Test-Path -LiteralPath $taskSource)) { continue }',
                  '  $taskTarget = Join-Path $taskRoot $taskPart',
                  '  if (Test-Path -LiteralPath $taskTarget) {',
                  '   Move-Item -LiteralPath $taskTarget -Destination (Join-Path $taskBackup $taskPart)',
                  '   $taskMoved += $taskPart', '  }',
                  '  Move-Item -LiteralPath $taskSource -Destination $taskTarget',
                  '  $taskInstalled += $taskPart', ' }',
                  " '更新完成' | Set-Content -LiteralPath $taskLog -Encoding utf8",
                  '} catch {',
                  ' $taskFailure = $_.Exception.Message',
                  ' foreach ($taskPart in $taskInstalled) { Move-Item -LiteralPath (Join-Path $taskRoot $taskPart) -Destination (Join-Path $taskStage $taskPart) }',
                  ' foreach ($taskPart in $taskMoved) { Move-Item -LiteralPath (Join-Path $taskBackup $taskPart) -Destination (Join-Path $taskRoot $taskPart) }',
                  ' $taskFailure | Set-Content -LiteralPath $taskLog -Encoding utf8',
                  '}', 'Start-Process -FilePath $taskExe -WorkingDirectory $taskRoot']
    path = folder / 'install.ps1'
    path.write_text('\n'.join(lines), encoding='utf-8-sig')
    return str(path)


def register(app):
    app.register('general.startup.get', lambda p: startup())
    app.register('general.startup.set', lambda p: startup(p['enabled']))
    app.register('updates.check', lambda p: app.jobs.submit('updates.check', {}))
    app.jobs.register('updates.check', lambda job: check())

    def download(job):
        info = check()  # Never accept download URLs/checksums from the UI.
        if not info['available'] or info['version'] != job.params.get('version'):
            raise ValueError('发布版本已变化或没有新版本，请重新检查更新。')
        folder = app.data_dir / 'updates' / uuid.uuid4().hex
        folder.mkdir(parents=True)
        package = folder / info['name']
        partial = folder / 'download.part'
        digest = hashlib.sha256(); received = 0
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            with client.stream('GET', info['url']) as response:
                response.raise_for_status()
                with partial.open('wb') as out:
                    for block in response.iter_bytes(1024*1024):
                        job.check_cancelled()
                        received += len(block)
                        if received > info['size']: raise ValueError('更新包大小与发布信息不符。')
                        out.write(block); digest.update(block)
                        job.progress(min(90, received / max(1, info['size']) * 90), '正在下载更新包…')
        if received != info['size'] or digest.hexdigest() != info['sha256']:
            partial.unlink(missing_ok=True)
            raise ValueError('更新包校验失败，请重新下载。')
        partial.rename(package)
        job.check_cancelled(); job.progress(93, '正在准备更新…')
        if info['portable']: unpack(package, folder / 'stage')
        (folder / 'verified.json').write_text(json.dumps(info), encoding='utf-8')
        return {**info, 'folder': str(folder), 'package': str(package), 'ready': True}

    def prepare(params):
        job = app.jobs.get(params['job_id'])
        if job['tool'] != 'updates.download' or job['status'] != 'completed':
            raise ValueError('请先完整下载更新包。')
        if app.jobs.active:
            raise ValueError('仍有后台任务，请完成或停止后再更新。')
        result = job['result']; folder = Path(result['folder']).resolve()
        if folder.parent != (app.data_dir / 'updates').resolve():
            raise ValueError('更新目录无效。')
        if version(result['version']) <= version(__version__):
            raise ValueError('此更新包不是新版本，请重新检查。')
        script = install_script(folder, executable(), result['portable'], os.environ['WINTOOLBOX_APP_PID'], Path(result['package']))
        return {'script': script}

    app.jobs.register('updates.download', download)
    app.register('updates.download', lambda p: app.jobs.submit('updates.download', {'version': p['version']}))
    app.register('updates.prepare', prepare)
