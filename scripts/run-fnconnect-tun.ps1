param([Parameter(Mandatory=$true)][string]$RuntimeDir)
$ErrorActionPreference = 'Stop'
$runtimePath = (Resolve-Path -LiteralPath $RuntimeDir).Path
$leasePath = Join-Path $runtimePath 'lease'
$statePath = Join-Path $runtimePath 'state.json'
$corePath = Join-Path $runtimePath 'mihomo-windows-amd64.exe'
$configPath = Join-Path $runtimePath 'config.json'
$coreProcess = $null
try {
    if (-not (Test-Path -LiteralPath $leasePath)) { throw 'Connection cancelled' }
    $coreProcess = Start-Process -FilePath $corePath -ArgumentList @('-d', ('"' + $runtimePath + '"'), '-f', ('"' + $configPath + '"')) -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimePath 'tun.log') -RedirectStandardError (Join-Path $runtimePath 'tun-error.log')
    Start-Sleep -Seconds 2
    if ($coreProcess.HasExited) { throw 'TUN core exited; check tun log' }
    @{phase='running';pid=$coreProcess.Id} | ConvertTo-Json -Compress | Set-Content -Encoding utf8 -LiteralPath $statePath
    while (-not $coreProcess.HasExited) {
        if (-not (Test-Path -LiteralPath $leasePath)) { break }
        if (((Get-Date).ToUniversalTime() - (Get-Item -LiteralPath $leasePath).LastWriteTimeUtc).TotalSeconds -gt 15) { break }
        Start-Sleep -Seconds 1
        $coreProcess.Refresh()
    }
    if ($coreProcess.HasExited -and (Test-Path -LiteralPath $leasePath)) { throw 'TUN core exited unexpectedly; see tun log' }
} catch {
    @{phase='error';error=$_.Exception.Message} | ConvertTo-Json -Compress | Set-Content -Encoding utf8 -LiteralPath $statePath
} finally {
    if ($coreProcess -and -not $coreProcess.HasExited) { Stop-Process -Id $coreProcess.Id -Force }
    if ((Test-Path -LiteralPath $statePath) -and (Get-Content -Raw -LiteralPath $statePath) -notmatch '"phase":"error"') { '{"phase":"stopped"}' | Set-Content -Encoding utf8 -LiteralPath $statePath }
}
