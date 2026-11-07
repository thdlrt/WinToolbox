param([string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $taskRoot '.build\bambu-cloud' }
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$taskSource = Join-Path $taskRoot 'tools\bambu-cloud\main.cpp'
$taskBuildDirectory = Join-Path $taskRoot '.build\bambu-cloud'
New-Item -ItemType Directory -Force -Path $taskBuildDirectory | Out-Null
$taskExe = Join-Path $taskBuildDirectory 'bambu-cloud-monitor.exe'
if ((Test-Path -LiteralPath $taskExe) -and (Get-Item -LiteralPath $taskExe).LastWriteTimeUtc -ge (Get-Item -LiteralPath $taskSource).LastWriteTimeUtc) {
    if ([IO.Path]::GetFullPath($OutputDirectory) -ne [IO.Path]::GetFullPath($taskBuildDirectory)) { Copy-Item -LiteralPath $taskExe -Destination $OutputDirectory -Force }
    return
}
$taskVsWhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$taskVs = (& $taskVsWhere -latest -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath).Trim()
if (-not $taskVs) { throw '需要 Visual Studio C++ 工具构建云端状态适配器' }
$taskBuild = Join-Path $taskBuildDirectory 'build.cmd'
@"
@echo off
call "$taskVs\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 exit /b 1
cl /nologo /EHsc /MD /std:c++17 /O2 "$taskSource" /Fe:"$taskExe" /Fo:"$taskBuildDirectory\main.obj"
exit /b %ERRORLEVEL%
"@ | Set-Content -LiteralPath $taskBuild -Encoding utf8
& cmd.exe /d /c $taskBuild
if ($LASTEXITCODE -ne 0) { throw 'Bambu 云端状态适配器构建失败' }
if ([IO.Path]::GetFullPath($OutputDirectory) -ne [IO.Path]::GetFullPath($taskBuildDirectory)) { Copy-Item -LiteralPath $taskExe -Destination $OutputDirectory -Force }
