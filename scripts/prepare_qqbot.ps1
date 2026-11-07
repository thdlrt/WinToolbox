param([string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskBuild = Join-Path $taskRoot '.build/qqbot-runtime'
if (-not $OutputDirectory) { $OutputDirectory = $taskBuild }
New-Item -ItemType Directory -Force -Path $taskBuild | Out-Null
Copy-Item -LiteralPath "$taskRoot/tools/qqbot-runtime/package.json" -Destination $taskBuild -Force
$taskLock = "$taskRoot/tools/qqbot-runtime/package-lock.json"
if (Test-Path -LiteralPath $taskLock) { Copy-Item -LiteralPath $taskLock -Destination $taskBuild -Force }
Push-Location $taskBuild
try {
    if (Test-Path -LiteralPath $taskLock) { npm.cmd ci --ignore-scripts --no-audit --no-fund --registry https://registry.npmmirror.com --cache E:/dev-caches/npm }
    else { npm.cmd install --ignore-scripts --no-audit --no-fund --registry https://registry.npmmirror.com --cache E:/dev-caches/npm }
    if ($LASTEXITCODE -ne 0) { throw 'QQ SDK 依赖准备失败' }
} finally { Pop-Location }
Copy-Item -LiteralPath "$taskBuild/package-lock.json" -Destination $taskLock -Force
$taskNode = (Get-Command node.exe).Source
Copy-Item -LiteralPath $taskNode -Destination "$taskBuild/node.exe" -Force
$taskNodeLicense = Join-Path (Split-Path -Parent $taskNode) 'LICENSE'
if (Test-Path -LiteralPath $taskNodeLicense) { Copy-Item -LiteralPath $taskNodeLicense -Destination "$taskBuild/node-LICENSE.txt" -Force }
if ([IO.Path]::GetFullPath($OutputDirectory) -ne [IO.Path]::GetFullPath($taskBuild)) {
    New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
    robocopy $taskBuild $OutputDirectory /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -gt 7) { throw 'QQ 运行环境复制失败' }
}
