param([Parameter(Mandatory=$true)][string]$OwnerSid)
$ErrorActionPreference = 'Stop'
if ($OwnerSid -notmatch '^S-1-5-21-(\d+-){3}\d+$') { throw 'Invalid owner SID' }
$taskRoot = Join-Path $env:ProgramFiles 'WinToolboxTun'
if ((Test-Path -LiteralPath $taskRoot) -and ((Get-Item -LiteralPath $taskRoot).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Invalid service directory' }
New-Item -ItemType Directory -Force -Path $taskRoot | Out-Null
$taskAcl = New-Object Security.AccessControl.DirectorySecurity
$taskAcl.SetAccessRuleProtection($true,$false)
foreach ($taskSid in @('S-1-5-18','S-1-5-32-544')) {
    $taskRule = New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier($taskSid)), 'FullControl','ContainerInherit,ObjectInherit','None','Allow')
    $taskAcl.AddAccessRule($taskRule)
}
$taskAcl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier('S-1-5-32-545')), 'ReadAndExecute','ContainerInherit,ObjectInherit','None','Allow')))
Set-Acl -LiteralPath $taskRoot -AclObject $taskAcl
$taskService = Get-Service -Name WinToolboxTun -ErrorAction SilentlyContinue
if ($taskService -and $taskService.Status -ne 'Stopped') { Stop-Service -Name WinToolboxTun; $taskService.WaitForStatus('Stopped',[TimeSpan]::FromSeconds(20)) }
foreach ($taskFile in @('WinToolboxTun.exe','mihomo-windows-amd64.exe','tun-template.json','LICENSE.txt')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $taskFile) -Destination (Join-Path $taskRoot $taskFile) -Force
}
[IO.File]::WriteAllText((Join-Path $taskRoot 'owner.sid'),$OwnerSid)
if (-not $taskService) {
    New-Service -Name WinToolboxTun -BinaryPathName ('"' + (Join-Path $taskRoot 'WinToolboxTun.exe') + '"') -DisplayName 'WinToolbox TUN Helper' -StartupType Automatic -Description 'WinToolbox local TUN helper; routes exist only while a VPN session is active.' | Out-Null
}
Start-Service -Name WinToolboxTun
