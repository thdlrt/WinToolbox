$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskExe = Join-Path $taskRoot 'dist\WinToolbox-portable\WinToolbox.exe'
if (-not (Test-Path -LiteralPath $taskExe)) { throw '请先打包便携版。' }
# Launch outside a packaged developer host: inherited registry virtualization
# otherwise makes HKCU shell verbs invisible to the normal Explorer process.
$taskStarted = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = ('"' + $taskExe + '"')
    CurrentDirectory = (Split-Path -Parent $taskExe)
}
if ($taskStarted.ReturnValue -ne 0) { throw "启动失败：$($taskStarted.ReturnValue)" }
Write-Output "WinToolbox 已启动，PID $($taskStarted.ProcessId)"
