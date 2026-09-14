param([switch]$BackendOnly, [switch]$RefreshDependencies, [switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$taskPython = Join-Path $taskRoot 'core/.venv/Scripts/python.exe'
$taskCore = Join-Path $taskRoot 'core'
$taskDesktop = Join-Path $taskRoot 'desktop'
$taskMutex = New-Object System.Threading.Mutex($false, 'Local\WinToolbox.Dev')
$taskOwnsMutex = $false

function Invoke-DevCommand {
    param([string]$Executable, [string[]]$Arguments, [string]$FailureMessage)
    # PowerShell 5.1 represents redirected native stderr as ErrorRecords;
    # tool progress there is normal. Check the actual process exit code.
    $taskPreviousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $Executable @Arguments
        $taskCommandExitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $taskPreviousPreference }
    if ($taskCommandExitCode -ne 0) { throw "$FailureMessage (exit $taskCommandExitCode)" }
}

function Get-DevCommand {
    param([string]$Name, [string]$Hint)
    $taskCommand = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $taskCommand) { throw $Hint }
    return $taskCommand.Source
}

try {
    try { $taskOwnsMutex = $taskMutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $taskOwnsMutex = $true }
    if (-not $taskOwnsMutex) { throw 'WinToolbox 开发模式已在启动或运行，请勿重复启动。' }
    if (-not $CheckOnly -and (Get-Process -Name wintoolbox -ErrorAction SilentlyContinue)) {
        throw '请先退出正在运行的 WinToolbox，再启动开发模式。'
    }
    # An explicit path still supports isolated development/CLI fixtures.
    if ([string]::IsNullOrWhiteSpace($env:WINTOOLBOX_DATA_DIR)) {
        $env:WINTOOLBOX_DATA_DIR = Join-Path $taskRoot 'dist/WinToolbox-portable/data'
    } elseif (-not [IO.Path]::IsPathRooted($env:WINTOOLBOX_DATA_DIR)) {
        $env:WINTOOLBOX_DATA_DIR = Join-Path $taskRoot $env:WINTOOLBOX_DATA_DIR
    }
    $env:WINTOOLBOX_DATA_DIR = [IO.Path]::GetFullPath($env:WINTOOLBOX_DATA_DIR)
    if (-not $CheckOnly) { New-Item -ItemType Directory -Path $env:WINTOOLBOX_DATA_DIR -Force | Out-Null }
    $env:WINTOOLBOX_PYTHON = $taskPython
    $env:PYTHONUTF8 = '1'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PYTHONPATH = $taskCore

    if (-not (Test-Path -LiteralPath $taskPython -PathType Leaf)) {
        if ($CheckOnly) { throw '尚未创建项目 Python 环境，请运行 dev.ps1 安装依赖。' }
        $taskUv = Get-DevCommand 'uv.exe' '缺少 uv，请先安装 uv 后重试。'
        Invoke-DevCommand -Executable $taskUv -Arguments @('venv', '--python', '3.12', (Join-Path $taskCore '.venv')) -FailureMessage '创建项目 Python 环境失败'
    }
    $taskDependencyCheck = @'
import importlib.metadata as metadata
import pathlib, re, sys, tomllib
project = tomllib.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))["project"]
missing = False
for requirement in project.get("dependencies", []):
    name = re.split(r"[<>=!~;\[ ]", requirement, maxsplit=1)[0]
    try:
        metadata.version(name)
    except metadata.PackageNotFoundError:
        missing = True
sys.exit(1 if missing else 0)
'@
    $taskDependencyCheck | & $taskPython - (Join-Path $taskCore 'pyproject.toml')
    $taskMissingBackend = $LASTEXITCODE -ne 0
    if ($CheckOnly -and $taskMissingBackend) { throw '缺少后端依赖，请运行 dev.ps1 安装。' }
    if (-not $CheckOnly -and ($RefreshDependencies -or $taskMissingBackend)) {
        $taskUv = Get-DevCommand 'uv.exe' '缺少 uv，无法安装项目依赖。请安装 uv 后重试。'
        $taskInstallArgs = @('pip', 'install', '--python', $taskPython, '-e', $taskCore)
        if ($RefreshDependencies) { $taskInstallArgs += '--reinstall' }
        Invoke-DevCommand -Executable $taskUv -Arguments $taskInstallArgs -FailureMessage '安装后端依赖失败'
    }

    if (-not $CheckOnly -and [string]::IsNullOrWhiteSpace($env:WINTOOLBOX_ADB)) {
        Invoke-DevCommand -Executable $taskPython -Arguments @((Join-Path $PSScriptRoot 'prepare_adb.py')) -FailureMessage '准备 ADB 失败'
        $env:WINTOOLBOX_ADB = Join-Path $taskRoot 'desktop/src-tauri/resources/tools/adb/adb.exe'
    }
    if ($BackendOnly -and -not $CheckOnly) {
        & $taskPython -u -m toolbox --data-dir $env:WINTOOLBOX_DATA_DIR
        exit $LASTEXITCODE
    }
    $taskNpm = Get-DevCommand 'npm.cmd' '缺少 Node.js / npm，请安装 Node.js 后重试。'
    $taskNode = Get-DevCommand 'node.exe' '缺少 Node.js，请安装 Node.js 后重试。'
    $null = Get-DevCommand 'cargo.exe' '缺少 Rust / Cargo，请先安装 Rust 与 Visual Studio C++ 构建工具。'
    Push-Location $taskDesktop
    try {
        $taskFrontendCheck = @'
const fs = require("fs");
const path = require("path");
const pkg = JSON.parse(fs.readFileSync("package.json", "utf8"));
const names = Object.keys({...pkg.dependencies, ...pkg.devDependencies});
const missing = names.some(name => !fs.existsSync(path.join("node_modules", name, "package.json"))) || ["vite", "tsc", "tauri"].some(name => !fs.existsSync(path.join("node_modules", ".bin", name + ".cmd")));
process.exit(missing ? 1 : 0);
'@
        $taskFrontendCheck | & $taskNode -
        $taskMissingFrontend = $LASTEXITCODE -ne 0
        if ($CheckOnly -and $taskMissingFrontend) { throw '缺少前端依赖，请运行 dev.ps1 安装。' }
        if (-not $CheckOnly -and ($RefreshDependencies -or $taskMissingFrontend)) {
            $taskInstallCommand = 'install'
            if (Test-Path -LiteralPath (Join-Path $taskDesktop 'package-lock.json')) { $taskInstallCommand = 'ci' }
            Invoke-DevCommand -Executable $taskNpm -Arguments @($taskInstallCommand) -FailureMessage '安装前端依赖失败'
        }
        # Confirm the option against this machine's installed CLI schema.
        $taskSchema = Get-Content -LiteralPath (Join-Path $taskDesktop 'node_modules/@tauri-apps/cli/config.schema.json') -Raw | ConvertFrom-Json
        $taskTauriArgs = @('run', 'tauri', '--', 'dev')
        if ($null -ne $taskSchema.definitions.BuildConfig.properties.additionalWatchFolders) {
            $taskBuildDir = Join-Path $taskRoot '.build'
            New-Item -ItemType Directory -Path $taskBuildDir -Force | Out-Null
            $taskDevConfigPath = Join-Path $taskBuildDir 'tauri.dev.generated.json'
            $taskDevConfig = @{ build = @{ additionalWatchFolders = @([IO.Path]::GetFullPath((Join-Path $taskCore 'toolbox'))) } } | ConvertTo-Json -Depth 5
            [IO.File]::WriteAllText($taskDevConfigPath, $taskDevConfig, (New-Object Text.UTF8Encoding($false)))
            $taskTauriArgs += @('--config', $taskDevConfigPath)
        } else { Write-Warning '当前 Tauri CLI 不支持 Python 自动重启；修改 Python 后请重新启动开发模式。' }
        if ($CheckOnly) {
            [ordered]@{
                root = $taskRoot
                data_dir = $env:WINTOOLBOX_DATA_DIR
                backend_dependencies_ready = -not $taskMissingBackend
                frontend_dependencies_ready = -not $taskMissingFrontend
                python_watch_supported = $null -ne $taskSchema.definitions.BuildConfig.properties.additionalWatchFolders
                dev_config = $taskDevConfigPath
                log = Join-Path $taskRoot '.build/logs/dev.log'
            } | ConvertTo-Json
            return
        }
        Write-Host "开发数据：$env:WINTOOLBOX_DATA_DIR"
        Write-Host '界面修改自动刷新；Python / Rust 修改自动重启桌面窗口。'
        Invoke-DevCommand -Executable $taskNpm -Arguments $taskTauriArgs -FailureMessage '开发模式启动失败，请查看日志'
    } finally { Pop-Location }
} finally {
    if ($taskOwnsMutex) { $taskMutex.ReleaseMutex() }
    $taskMutex.Dispose()
}
