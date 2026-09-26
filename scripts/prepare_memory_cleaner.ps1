$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskSource = Join-Path $taskRoot 'native/memory-cleaner/MemoryCleaner.cs'
$taskOutput = Join-Path $taskRoot 'desktop/src-tauri/resources/tools/memory-cleaner'
$taskCompiler = Join-Path ([Environment]::GetFolderPath('Windows')) 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
if (-not (Test-Path -LiteralPath $taskCompiler)) { throw '缺少 .NET Framework C# 编译器' }
New-Item -ItemType Directory -Force -Path $taskOutput | Out-Null
$taskExe = Join-Path $taskOutput 'WinToolbox.MemoryCleaner.exe'
$taskManifest = Join-Path $taskOutput 'manifest.json'
$taskSourceHash = (Get-FileHash -LiteralPath $taskSource -Algorithm SHA256).Hash.ToLowerInvariant()
if ((Test-Path -LiteralPath $taskExe) -and (Test-Path -LiteralPath $taskManifest)) {
    $taskPrevious = Get-Content -LiteralPath $taskManifest -Raw | ConvertFrom-Json
    if ($taskPrevious.source_sha256 -eq $taskSourceHash -and $taskPrevious.sha256 -eq (Get-FileHash -LiteralPath $taskExe -Algorithm SHA256).Hash.ToLowerInvariant()) {
        Write-Output '固定用途内存清理组件已准备（源码未变）'
        return
    }
}
& $taskCompiler /nologo /target:winexe /platform:x64 /optimize+ /reference:System.Web.Extensions.dll /reference:Microsoft.CSharp.dll /reference:System.Core.dll "/out:$taskExe" $taskSource
if ($LASTEXITCODE -ne 0) { throw '清理组件构建失败' }
@{ sha256=(Get-FileHash -LiteralPath $taskExe -Algorithm SHA256).Hash.ToLowerInvariant(); source_sha256=$taskSourceHash; protocol=1 } | ConvertTo-Json | Set-Content -LiteralPath $taskManifest -Encoding utf8
Write-Output '固定用途内存清理组件已构建'
