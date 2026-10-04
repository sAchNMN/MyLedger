<#
.SYNOPSIS
  给月度批处理装 / 卸 / 查自动触发。

.DESCRIPTION
  两种触发方式，默认用第一种：

  1. 登录时触发（默认，推荐）
     在「启动」文件夹里放一个快捷方式。不需要管理员权限，登录后自动跑一次。
     笔记本最怕「到点没开机」——登录触发不存在这个问题。

  2. 每天定时触发（-Daily）
     用任务计划程序建一个每日任务。注意笔记本到点关机的话这次就跳过了。

  为什么默认选「登录触发」：
    这条流水线是幂等的（同一份文件重复导入不会重复记账），
    所以「跑得频繁」没有任何副作用。登录触发比定时触发更可靠，
    也不会为了跑一个轻量脚本去唤醒机器。

  为什么这件事不该交给 AI agent：
    解析、去重、分类都有缓存，全程离线、几秒跑完。
    让 AI 每天启动一次来做这件事，只多耗电，不多出任何价值。

  用法：
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\schedule-monthly.ps1
    ... -Action register                       # 装（登录触发）
    ... -Action register -Daily -Time 09:00    # 改成每天 09:00
    ... -Action status                         # 查看当前状态
    ... -Action remove                         # 卸载
#>
[CmdletBinding()]
param(
    [ValidateSet('register', 'remove', 'status')]
    [string]$Action = 'status',

    [switch]$Daily,

    [string]$Time = '09:00',

    [string]$TaskName = 'MyLedger-Monthly'
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Runner = Join-Path $RepoRoot 'scripts\monthly-run.ps1'
$StartupDir = [Environment]::GetFolderPath('Startup')
$Shortcut = Join-Path $StartupDir "$TaskName.lnk"
$PowershellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

if (-not (Test-Path -LiteralPath $Runner)) {
    Write-Host "找不到 $Runner"
    exit 3
}

function Show-Status {
    Write-Host "仓库根目录 : $RepoRoot"
    Write-Host "执行脚本   : $Runner"
    Write-Host ""

    if (Test-Path -LiteralPath $Shortcut) {
        Write-Host "登录触发   : 已启用（$Shortcut）"
    }
    else {
        Write-Host "登录触发   : 未启用"
    }

    $query = & schtasks.exe /Query /TN $TaskName /FO LIST 2>$null
    if ($LASTEXITCODE -eq 0 -and $query) {
        Write-Host "每日定时   : 已启用"
        foreach ($line in $query) { Write-Host "             $line" }
    }
    else {
        Write-Host "每日定时   : 未启用"
    }
    Write-Host ""
    Write-Host "日志位置   : $(Join-Path $env:USERPROFILE 'MyLedgerVault\logs')"
}

function Register-Startup {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($Shortcut)
    $lnk.TargetPath = $PowershellExe
    $lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`""
    $lnk.WorkingDirectory = $RepoRoot
    $lnk.Description = 'MyLedger 月度账单批处理（登录时自动运行）'
    $lnk.Save()
    Write-Host "已创建登录触发快捷方式："
    Write-Host "  $Shortcut"
    Write-Host "下次登录后自动跑一次。想立刻试一次："
    Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File `"$Runner`""
}

function Register-Daily {
    $inner = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$Runner`""
    $argList = @('/Create', '/TN', $TaskName, '/TR', $inner,
                 '/SC', 'DAILY', '/ST', $Time, '/F')
    $output = & schtasks.exe @argList 2>&1
    $code = $LASTEXITCODE
    foreach ($line in $output) { Write-Host $line }
    if ($code -eq 0) {
        Write-Host "已创建每日任务「$TaskName」，时间 $Time。"
        Write-Host "注意：笔记本到点关机的话这次会跳过，下次登录不会自动补跑。"
        Write-Host "      追求可靠就用默认的登录触发。"
    }
    else {
        Write-Host ""
        Write-Host "创建任务失败（退出码 $code）。通常是权限不够。"
        Write-Host "改用不需要管理员权限的登录触发："
        Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\schedule-monthly.ps1 -Action register"
    }
}

function Unregister-All {
    $removed = $false
    if (Test-Path -LiteralPath $Shortcut) {
        Remove-Item -LiteralPath $Shortcut -Force
        Write-Host "已删除登录触发快捷方式。"
        $removed = $true
    }
    $null = & schtasks.exe /Query /TN $TaskName /FO LIST 2>$null
    if ($LASTEXITCODE -eq 0) {
        $output = & schtasks.exe /Delete /TN $TaskName /F 2>&1
        foreach ($line in $output) { Write-Host $line }
        Write-Host "已删除每日定时任务。"
        $removed = $true
    }
    if (-not $removed) { Write-Host "没有找到任何已注册的触发方式。" }
    Write-Host ""
    Write-Host "提示：这只取消了自动触发，账本数据和日志一点没动。"
}

switch ($Action) {
    'register' {
        Register-Startup
        if ($Daily) {
            Write-Host ""
            Register-Daily
        }
    }
    'remove' { Unregister-All }
    'status' { Show-Status }
}
