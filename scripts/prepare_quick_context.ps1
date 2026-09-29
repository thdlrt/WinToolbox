$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskSource = Join-Path $taskRoot 'native/quick-context/QuickContext.cs'
$taskOutput = Join-Path $taskRoot 'desktop/src-tauri/resources/tools/quick-context'
$taskFramework = Join-Path ([Environment]::GetFolderPath('Windows')) 'Microsoft.NET/Framework64/v4.0.30319'
$taskCompiler = Join-Path $taskFramework 'csc.exe'
New-Item -ItemType Directory -Force -Path $taskOutput | Out-Null
& $taskCompiler /nologo /target:winexe /platform:x64 /optimize+ /reference:System.Web.Extensions.dll /reference:Microsoft.CSharp.dll /reference:System.Core.dll /reference:System.Windows.Forms.dll "/reference:$taskFramework/WPF/UIAutomationClient.dll" "/reference:$taskFramework/WPF/UIAutomationTypes.dll" "/reference:$taskFramework/WPF/WindowsBase.dll" "/out:$taskOutput/WinToolbox.QuickContext.exe" $taskSource
if ($LASTEXITCODE -ne 0) { throw '选区读取组件构建失败' }
Write-Output '选区读取组件已构建（仅触发时运行）'
