param([switch]$StageOnly,[switch]$SkipInstall,[switch]$PortableOnly)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
if ($PortableOnly -and -not $StageOnly) {
    $taskRunning = Get-Process -Name wintoolbox -ErrorAction SilentlyContinue
    if ($taskRunning) { throw '请先退出正在运行的 WinToolbox，再更新免安装版。' }
}
$taskVersion = (Get-Content -LiteralPath "$taskRoot/desktop/src-tauri/tauri.conf.json" -Raw | ConvertFrom-Json).version
function Move-ObsoletePackageMetadata([string]$taskSitePackages) {
    if (-not (Test-Path -LiteralPath $taskSitePackages)) { return }
    foreach ($taskMetadata in (Get-ChildItem -LiteralPath $taskSitePackages -Directory -Filter 'wintoolbox_core-*.dist-info')) {
        if ($taskMetadata.Name -eq "wintoolbox_core-$taskVersion.dist-info") { continue }
        $taskSource = [IO.Path]::GetFullPath($taskMetadata.FullName)
        $taskBackup = [IO.Path]::GetFullPath((Join-Path $taskRoot ('.build/obsolete-package-metadata/' + [Guid]::NewGuid().ToString('N'))))
        $taskWorkspace = [IO.Path]::GetFullPath($taskRoot).TrimEnd('\') + '\'
        if (-not $taskSource.StartsWith($taskWorkspace, [StringComparison]::OrdinalIgnoreCase) -or -not $taskBackup.StartsWith($taskWorkspace, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unexpected package metadata target' }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $taskBackup) | Out-Null
        Move-Item -LiteralPath $taskSource -Destination $taskBackup
    }
}
$taskPython = Join-Path $taskRoot 'core/.venv/Scripts/python.exe'
if (-not (Test-Path $taskPython)) { throw '请先运行 scripts/dev.ps1 -BackendOnly 安装环境' }
if (-not $SkipInstall) { uv pip install --python $taskPython -e "$taskRoot/core"; if ($LASTEXITCODE -ne 0) { throw '后端依赖安装失败' } }
$taskBase = (& $taskPython -c "import sys; print(sys.base_prefix)").Trim()
$taskStage = Join-Path $taskRoot 'desktop/src-tauri/resources'
foreach ($taskPart in @('python','core','tools')) { New-Item -ItemType Directory -Path (Join-Path $taskStage $taskPart) -Force | Out-Null }
# Build-owned staging only. Robocopy exit codes 0..7 are successful.
robocopy $taskBase "$taskStage/python" /E /NFL /NDL /NJH /NJS /NP /XD __pycache__ .git | Out-Null
if ($LASTEXITCODE -gt 7) { throw '复制 Python 失败' }
robocopy "$taskRoot/core/.venv/Lib/site-packages" "$taskStage/python/Lib/site-packages" /E /NFL /NDL /NJH /NJS /NP /XD __pycache__ /XF '*.pyc' | Out-Null
if ($LASTEXITCODE -gt 7) { throw '复制依赖失败' }
Move-ObsoletePackageMetadata "$taskStage/python/Lib/site-packages"
# Remove development-only path injection; the bundle must not resolve source from this machine.
foreach ($taskDevFile in @('_editable_impl_wintoolbox_core.pth','_editable_impl_wintoolbox_core.py','_virtualenv.pth','_virtualenv.py')) {
    $taskDevPath = Join-Path "$taskStage/python/Lib/site-packages" $taskDevFile
    if (Test-Path -LiteralPath $taskDevPath) { Remove-Item -LiteralPath $taskDevPath -Force }
}
robocopy "$taskRoot/core/toolbox" "$taskStage/core/toolbox" /E /NFL /NDL /NJH /NJS /NP /XD __pycache__ /XF '*.pyc' | Out-Null
if ($LASTEXITCODE -gt 7) { throw '复制程序失败' }
$taskStandaloneFFmpeg = Join-Path $env:USERPROFILE 'scoop/apps/ffmpeg/current/bin/ffmpeg.exe'
if (Test-Path -LiteralPath $taskStandaloneFFmpeg) { $taskFFmpeg = $taskStandaloneFFmpeg }
else { $taskFFmpeg = (Get-Command ffmpeg.exe).Source }
$taskFFprobe = Join-Path (Split-Path -Parent $taskFFmpeg) 'ffprobe.exe'
Copy-Item -LiteralPath $taskFFmpeg -Destination "$taskStage/tools/ffmpeg.exe" -Force
Copy-Item -LiteralPath $taskFFprobe -Destination "$taskStage/tools/ffprobe.exe" -Force
$taskUv = (Get-Command uv.exe).Source
Copy-Item -LiteralPath $taskUv -Destination "$taskStage/tools/uv.exe" -Force
& $taskPython "$PSScriptRoot/prepare_fnconnect.py"
if ($LASTEXITCODE -ne 0) { throw "准备 FN Connect 核心失败" }
& $taskPython "$PSScriptRoot/prepare_adb.py"
if ($LASTEXITCODE -ne 0) { throw '准备 ADB 运行环境失败' }
& $taskPython "$PSScriptRoot/prepare_memreduct.py"
if ($LASTEXITCODE -ne 0) { throw '准备 Mem Reduct 失败' }
Copy-Item -LiteralPath "$taskRoot/docs/THIRD_PARTY.md" -Destination "$taskStage/tools/THIRD_PARTY.md" -Force
& $taskPython -c 'import pathlib,subprocess,sys; r=subprocess.run([sys.argv[1],sys.argv[2]],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,check=True); pathlib.Path(sys.argv[3]).write_bytes(r.stdout)' $taskFFmpeg '-L' "$taskStage/tools/ffmpeg-license.txt"
if ($LASTEXITCODE -ne 0) { throw '保存 FFmpeg 许可信息失败' }
& $taskPython -c 'import pathlib,subprocess,sys; r=subprocess.run([sys.argv[1],sys.argv[2]],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,check=True); pathlib.Path(sys.argv[3]).write_bytes(r.stdout)' $taskFFmpeg '-buildconf' "$taskStage/tools/ffmpeg-buildconf.txt"
if ($LASTEXITCODE -ne 0) { throw '保存 FFmpeg 编译信息失败' }
& $taskPython "$PSScriptRoot/make_icons.py"
if ($LASTEXITCODE -ne 0) { throw '图标生成失败' }
Write-Output "已准备独立运行环境：$taskStage"
if ($StageOnly) { exit 0 }
Push-Location "$taskRoot/desktop"
try {
    if ($PortableOnly) { npm.cmd run tauri -- build --no-bundle }
    else { npm.cmd run tauri build }
    if ($LASTEXITCODE -ne 0) { throw '桌面打包失败' }
} finally { Pop-Location }
$taskDist = Join-Path $taskRoot 'dist'
$taskPortable = Join-Path $taskDist 'WinToolbox-portable'
New-Item -ItemType Directory -Path $taskPortable -Force | Out-Null
Copy-Item -LiteralPath "$taskRoot/desktop/src-tauri/target/release/wintoolbox.exe" -Destination "$taskPortable/WinToolbox.exe" -Force
foreach ($taskPart in @('python','core','tools')) { robocopy "$taskStage/$taskPart" "$taskPortable/$taskPart" /E /NFL /NDL /NJH /NJS /NP | Out-Null; if ($LASTEXITCODE -gt 7) { throw "便携文件复制失败：$taskPart" } }
Move-ObsoletePackageMetadata "$taskPortable/python/Lib/site-packages"
Set-Content -LiteralPath "$taskPortable/portable.flag" -Value 'WinToolbox portable data mode' -Encoding utf8
Copy-Item -LiteralPath "$taskRoot/README.md" -Destination "$taskPortable/使用说明.md" -Force
New-Item -ItemType Directory -Path "$taskPortable/docs" -Force | Out-Null
Copy-Item -LiteralPath "$taskRoot/docs/PROJECT-MEMORY.md" -Destination "$taskPortable/docs/PROJECT-MEMORY.md" -Force
Copy-Item -LiteralPath "$taskRoot/docs/FILESYNC.md" -Destination "$taskPortable/docs/FILESYNC.md" -Force
Copy-Item -LiteralPath "$taskRoot/docs/FILE-RELAY.md" -Destination "$taskPortable/docs/FILE-RELAY.md" -Force
Copy-Item -LiteralPath "$taskRoot/docs/FLOATING-ORB.md" -Destination "$taskPortable/docs/FLOATING-ORB.md" -Force
if ($PortableOnly) {
    Write-Output "免安装版已更新：$taskPortable/WinToolbox.exe"
    Write-Output '设置、模型和记录保留在相邻 data 目录。'
    exit 0
}
$taskInstallerSource = Get-ChildItem -LiteralPath "$taskRoot/desktop/src-tauri/target/release/bundle/nsis" -Filter "WinToolbox_$($taskVersion)_x64-setup.exe" | Select-Object -First 1
if (-not $taskInstallerSource) { throw '没有找到当前版本安装包' }
$taskInstaller = Join-Path $taskDist $taskInstallerSource.Name
Copy-Item -LiteralPath $taskInstallerSource.FullName -Destination $taskInstaller -Force

# GitHub Release must never include the user's adjacent portable data directory.
$taskReleaseRoot = [IO.Path]::GetFullPath((Join-Path $taskRoot '.build/release'))
$taskExpectedReleaseRoot = [IO.Path]::GetFullPath((Join-Path $taskRoot '.build')).TrimEnd('\') + '\'
if (-not $taskReleaseRoot.StartsWith($taskExpectedReleaseRoot, [StringComparison]::OrdinalIgnoreCase)) { throw '发布暂存目录越界' }
if (Test-Path -LiteralPath $taskReleaseRoot) { Remove-Item -LiteralPath $taskReleaseRoot -Recurse -Force }
$taskReleasePortable = Join-Path $taskReleaseRoot 'WinToolbox-portable'
New-Item -ItemType Directory -Path $taskReleasePortable -Force | Out-Null
Copy-Item -LiteralPath "$taskRoot/desktop/src-tauri/target/release/wintoolbox.exe" -Destination "$taskReleasePortable/WinToolbox.exe" -Force
foreach ($taskPart in @('python','core','tools')) { robocopy "$taskStage/$taskPart" "$taskReleasePortable/$taskPart" /E /NFL /NDL /NJH /NJS /NP | Out-Null; if ($LASTEXITCODE -gt 7) { throw "发布文件复制失败：$taskPart" } }
Set-Content -LiteralPath "$taskReleasePortable/portable.flag" -Value 'WinToolbox portable data mode' -Encoding utf8
Copy-Item -LiteralPath "$taskRoot/README.md" -Destination "$taskReleasePortable/使用说明.md" -Force
New-Item -ItemType Directory -Path "$taskReleasePortable/docs" -Force | Out-Null
foreach ($taskDoc in @('PROJECT-MEMORY.md','FILESYNC.md','FILE-RELAY.md','FLOATING-ORB.md',"RELEASE-$taskVersion.md")) {
    Copy-Item -LiteralPath "$taskRoot/docs/$taskDoc" -Destination "$taskReleasePortable/docs/$taskDoc" -Force
}
$taskPortableZip = Join-Path $taskDist "WinToolbox-$taskVersion-portable.zip"
Compress-Archive -LiteralPath $taskReleasePortable -DestinationPath $taskPortableZip -Force
@($taskInstaller,$taskPortableZip) | Get-FileHash -Algorithm SHA256 | ForEach-Object {
    [PSCustomObject]@{ File = [IO.Path]::GetFileName($_.Path); Hash = $_.Hash }
} | ConvertTo-Json | Set-Content -LiteralPath "$taskDist/SHA256SUMS.json" -Encoding utf8
Write-Output "打包完成：$taskDist"
