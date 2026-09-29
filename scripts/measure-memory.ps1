param([string]$Label = 'snapshot', [string]$OutputPath)
$ErrorActionPreference = 'Stop'
# Read-only process-tree sampling. Working sets can share pages; private bytes
# measure committed private memory and must not be described as physical RAM.
$taskProcesses = @(Get-CimInstance Win32_Process)
$taskRoots = @($taskProcesses | Where-Object Name -eq 'WinToolbox.exe')
if (-not $taskRoots) { throw 'WinToolbox is not running' }
$taskIds = @($taskRoots.ProcessId)
do {
    $taskChildren = @($taskProcesses | Where-Object { $_.ParentProcessId -in $taskIds -and $_.ProcessId -notin $taskIds })
    $taskIds += $taskChildren.ProcessId
} while ($taskChildren.Count)
$taskRows = @($taskProcesses | Where-Object ProcessId -in $taskIds | ForEach-Object {
    $taskProcess = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
    if ($taskProcess) {
        [pscustomobject]@{
            pid = $_.ProcessId; parent = $_.ParentProcessId; name = $_.Name
            working_set_bytes = $taskProcess.WorkingSet64
            private_bytes = $taskProcess.PrivateMemorySize64
            cpu_seconds = $taskProcess.CPU
            threads = $taskProcess.Threads.Count
            role = $(if ($_.CommandLine -match '--type=([^ ]+)') { $Matches[1] } else { '' })
        }
    }
})
$taskResult = [pscustomobject]@{
    label = $Label; time = (Get-Date).ToUniversalTime().ToString('o')
    working_set_mb = [math]::Round(($taskRows | Measure-Object working_set_bytes -Sum).Sum / 1MB, 2)
    private_mb = [math]::Round(($taskRows | Measure-Object private_bytes -Sum).Sum / 1MB, 2)
    processes = $taskRows
}
$taskJson = $taskResult | ConvertTo-Json -Depth 4
if ($OutputPath) { Set-Content -LiteralPath $OutputPath -Value $taskJson -Encoding utf8 }
$taskJson
