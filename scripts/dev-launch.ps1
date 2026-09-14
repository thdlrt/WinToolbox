param([switch]$RefreshDependencies)

# Shortcut entry: powershell.exe -WindowStyle Hidden -File this-script.ps1
$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$taskLogDir = Join-Path $taskRoot '.build/logs'
$taskLogPath = Join-Path $taskLogDir 'dev.log'
$taskEncoding = New-Object Text.UTF8Encoding($true)
$taskLogReady = $false

try {
    New-Item -ItemType Directory -Path $taskLogDir -Force | Out-Null
    if (-not (Test-Path -LiteralPath $taskLogPath)) { [IO.File]::WriteAllText($taskLogPath, '', $taskEncoding) }
    [IO.File]::AppendAllText($taskLogPath, "`r`n[$([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss'))] 启动开发模式`r`n", $taskEncoding)
    $taskLogReady = $true
    $taskLaunchArgs = @{}
    if ($RefreshDependencies) { $taskLaunchArgs.RefreshDependencies = $true }
    & (Join-Path $PSScriptRoot 'dev.ps1') @taskLaunchArgs *>&1 | ForEach-Object {
        [IO.File]::AppendAllText($taskLogPath, ([string]$_ + [Environment]::NewLine), $taskEncoding)
    }
} catch {
    $taskMessage = $_.Exception.Message
    if ($taskLogReady) {
        try { [IO.File]::AppendAllText($taskLogPath, "`r`n失败：$taskMessage`r`n$($_.ScriptStackTrace)`r`n", $taskEncoding) } catch { }
    }
    Add-Type -AssemblyName System.Windows.Forms
    $taskSummary = ($taskMessage -split '\r?\n')[0]
    if ($taskSummary.Length -gt 200) { $taskSummary = $taskSummary.Substring(0, 200) + '…' }
    [Windows.Forms.MessageBox]::Show("$taskSummary`r`n`r`n日志：$taskLogPath", 'WinToolbox 开发模式', [Windows.Forms.MessageBoxButtons]::OK, [Windows.Forms.MessageBoxIcon]::Warning) | Out-Null
    exit 1
}
