# 把本仓库托管的技能镜像进 CC Switch 技能库（真实目录）。
# 背景：CC Switch >= 4.0.22 的 WebDAV 上传打包会把技能库里的 junction/symlink 当普通文件读取，
# 在 Windows 上稳定报「拒绝访问 (os error 5)」并中断整次同步；因此仓库托管的技能在技能库中
# 必须是真实目录，由本脚本负责 仓库 -> 技能库 的单向同步（/MIR，会删除库中多余文件）。
# 用法：scripts\sync-ccswitch-skills.ps1 [-Library 路径] [-Skills 名称列表]
param(
    [string]$Library = "$env:USERPROFILE\.cc-switch\skills",
    [string[]]$Skills = @('agent-knowledge', 'netease-music')
)
$ErrorActionPreference = 'Stop'
$repoSkills = Join-Path (Resolve-Path (Join-Path $PSScriptRoot '..')) 'skills'
foreach ($name in $Skills) {
    $src = Join-Path $repoSkills $name
    $dst = Join-Path $Library $name
    if (-not (Test-Path -LiteralPath $src)) { throw "仓库技能源目录不存在：$src" }
    $item = Get-Item -LiteralPath $dst -Force -ErrorAction SilentlyContinue
    if ($item -and $item.LinkType) {
        throw "$dst 仍是链接（$($item.LinkType)）。请先确认目标内容已备份，再手工执行 rmdir `"$dst`" 后重跑本脚本。"
    }
    robocopy $src $dst /MIR /COPY:DAT /DCOPY:DAT /R:1 /W:1 /NJH /NJS /NDL /NC /NS | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy 同步 $name 失败（exit $LASTEXITCODE）" }
    Write-Host ("synced {0} -> {1} (robocopy exit {2})" -f $src, $dst, $LASTEXITCODE)
}
