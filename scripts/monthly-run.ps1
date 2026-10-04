<#
.SYNOPSIS
  MyLedger 月度批处理的后台执行器（给计划任务用，不弹窗、不暂停）。

.DESCRIPTION
  和 scripts\monthly.cmd 的区别：
    monthly.cmd   给人双击的，跑完会 pause 等你看
    本脚本         给计划任务跑的，非交互，结果写进 vault\logs\monthly-YYYYMM.log

  日志为什么只有几行 JSON（ML-004）：
    这个脚本以前是这么写的：

        $output = & $python @pyArgs 2>&1
        [System.IO.File]::AppendAllLines($LogFile, $output)

    子进程的**全部**标准输出和错误输出都被抄进日志 —— 解析失败的行、
    dry-run 预览、异常堆栈、异常消息，有什么抄什么。而日志的处境最糟：
    不像账本那样加密，却往往是 vault 里唯一一份人人可读的明文副本，
    还会被云盘同步、被备份、半年后被人翻出来。

    现在改成两件事分开：
      * Python 自己写结构化日志（--log-file），只有白名单字段：
        计数、状态、错误码。文件名过脱敏，交易内容一个字都不写。
      * 本脚本**不再捕获**子进程输出。手动跑的时候报告直接打在屏幕上，
        计划任务里没有控制台，输出自然消失 —— 不进日志。

    本脚本自己只留三条 JSON 记录（runner_start / runner_error / runner_end），
    用来覆盖「Python 根本没起来」这种情况（比如解释器找不到）。

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

function Write-AuditLine {
    # 只写固定事件名和几个非敏感字段。刻意不接受自由文本参数：
    # 能自由填写的地方，迟早会有人把异常消息原样塞进去。
    param(
        [string]$Event,
        [hashtable]$Fields = @{}
    )
    $payload = [ordered]@{ ts = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'); event = $Event }
    foreach ($key in $Fields.Keys) { $payload[$key] = $Fields[$key] }
    $line = $payload | ConvertTo-Json -Compress
    [System.IO.File]::AppendAllLines($LogFile, [string[]]@($line), $Utf8)
}

function Get-PythonExe {
    # 优先用隔离 venv 的解释器：PDF 账单解析依赖装在里面的 PyMuPDF。
    # 退回裸 python 的话，每次 PDF 导入都会变成一句看不懂的「没有 PDF 库」。
    $venv = Join-Path $env:USERPROFILE '.workbuddy\binaries\python\envs\default\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv) { return $venv }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $managed = Join-Path $env:USERPROFILE '.workbuddy\binaries\python\versions\3.13.12\python.exe'
    if (Test-Path -LiteralPath $managed) { return $managed }
    throw "找不到 Python。装一个 Python 3.10+ 并确保 python 在 PATH 上。"
}

Write-AuditLine -Event 'runner_start' -Fields @{
    dry_run = [bool]$DryRun
    ai      = [bool]$Ai
}

$exitCode = 1
try {
    $python = Get-PythonExe
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'

    # --no-review：后台执行器永远不开确认界面。
    # 定时/登录触发时没人在场，弹出来的浏览器窗口没人点，只会干扰。
    # 需要人工确认时手动跑 monthly.cmd，那个入口会自动拉起界面。
    $pyArgs = @('-m', 'importer', 'monthly', '--vault', $Vault, '--no-review',
                '--log-file', $LogFile)
    if ($Ai) { $pyArgs += '--ai' }
    if ($DryRun) { $pyArgs += '--dry-run' }

    Push-Location $RepoRoot
    try {
        # 刻意不赋值给变量：让子进程直接继承控制台。
        # 捕获输出就意味着「把它的全部输出抄进日志」，那正是要修的东西。
        & $python @pyArgs
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
}
catch {
    # 只记异常类型名，不记消息（消息里可能带路径、带原始值）。
    Write-AuditLine -Event 'runner_error' -Fields @{
        error_type = $_.Exception.GetType().Name
    }
    $exitCode = 1
}

Write-AuditLine -Event 'runner_end' -Fields @{ exit_code = $exitCode }

exit $exitCode
