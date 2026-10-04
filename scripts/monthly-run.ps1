<#
.SYNOPSIS
  MyLedger 月度批处理的后台执行器（给计划任务用，不弹窗、不暂停）。

.DESCRIPTION
  和 scripts\monthly.cmd 的区别：
    monthly.cmd   给人双击的，跑完会 pause 等你看
    本脚本         给计划任务跑的，非交互，输出追加到 vault\logs\monthly-YYYYMM.log

  日志落盘是自动化的必需品。没人盯着屏幕的时候，
  出了问题你唯一能依靠的就是这份日志。

  用法（一般不用手动调，由 schedule-monthly.ps1 注册）：
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\monthly-run.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\monthly-run.ps1 -Ai
#>
[CmdletBinding()]
param(
    [switch]$Ai,
    [switch]$DryRun,
    [string]$Vault = (Join-Path $env:USERPROFILE 'MyLedgerVault')
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$LogDir = Join-Path $Vault 'logs'
if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
}
$LogFile = Join-Path $LogDir ("monthly-{0:yyyyMM}.log" -f (Get-Date))
$Utf8 = New-Object System.Text.UTF8Encoding($false)

function Get-PythonExe {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $managed = Join-Path $env:USERPROFILE '.workbuddy\binaries\python\versions\3.13.12\python.exe'
    if (Test-Path -LiteralPath $managed) { return $managed }
    throw "找不到 Python。装一个 Python 3.10+ 并确保 python 在 PATH 上。"
}

$lines = New-Object System.Collections.Generic.List[string]
$lines.Add(("[{0:yyyy-MM-dd HH:mm:ss}] ---- 开始月度处理 ----" -f (Get-Date)))

$exitCode = 1
try {
    $python = Get-PythonExe
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'

    # --no-review：后台执行器永远不开确认界面。
    # 定时/登录触发时没人在场，弹出来的浏览器窗口没人点，只会干扰。
    # 需要人工确认时手动跑 monthly.cmd，那个入口会自动拉起界面。
    $pyArgs = @('-m', 'importer', 'monthly', '--vault', $Vault, '--no-review')
    if ($Ai) { $pyArgs += '--ai' }
    if ($DryRun) { $pyArgs += '--dry-run' }

    Push-Location $RepoRoot
    try {
        $output = & $python @pyArgs 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    foreach ($item in $output) {
        foreach ($line in ([string]$item -split "`r?`n")) {
            if ($line -ne '') { $lines.Add($line) }
        }
    }
}
catch {
    $lines.Add("执行异常：$($_.Exception.Message)")
    $exitCode = 1
}

$lines.Add(("[{0:yyyy-MM-dd HH:mm:ss}] ---- 结束，退出码 {1} ----" -f (Get-Date), $exitCode))
$lines.Add('')

[System.IO.File]::AppendAllLines($LogFile, [string[]]$lines, $Utf8)

# 后台运行时没有控制台，这里输出只是给「手动跑一次看看」用的
foreach ($line in $lines) { Write-Output $line }

exit $exitCode
