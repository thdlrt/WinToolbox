param([switch]$Update)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskExecutable = Join-Path $taskRoot 'dist/WinToolbox-portable/WinToolbox.exe'
try {
    if ($Update) {
        $taskLogDirectory = Join-Path $taskRoot '.build/logs'
        New-Item -ItemType Directory -Force -Path $taskLogDirectory | Out-Null
        $taskLog = Join-Path $taskLogDirectory 'portable-update.log'
        $taskErrorLog = Join-Path $taskLogDirectory 'portable-update-error.log'
        $taskPowerShell = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
        # OS-level redirection avoids PowerShell 5.1 treating normal compiler
        # stderr/progress as terminating NativeCommandError records.
        $taskUpdate = Start-Process -FilePath $taskPowerShell -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',('"' + (Join-Path $PSScriptRoot 'package.ps1') + '"'),'-PortableOnly','-SkipInstall') -WindowStyle Hidden -RedirectStandardOutput $taskLog -RedirectStandardError $taskErrorLog -Wait -PassThru
        if ($taskUpdate.ExitCode -ne 0) { throw "更新失败，请查看日志：$taskErrorLog" }
    }
    if (-not (Test-Path -LiteralPath $taskExecutable)) { throw '尚未生成免安装版，请先运行“更新免安装版”。' }
    # The app is the requested interactive window; helper PowerShell stays hidden.
    Start-Process -FilePath $taskExecutable -WorkingDirectory (Split-Path -Parent $taskExecutable)
} catch {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'WinToolbox', 'OK', 'Error') | Out-Null
    exit 1
}
