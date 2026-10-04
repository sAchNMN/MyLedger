<#
.SYNOPSIS
  安装 MyLedger 提交闸门（pre-commit）。

.DESCRIPTION
  作用：把 Git 的 hooks 目录指向仓库内的 .githooks/，让防泄露闸门随仓库一起版本化。

  为什么需要这一步：Git 默认从 .git/hooks 读取钩子，而 .git/ 不参与版本控制。
  这样即使钩子文件提交到了 GitHub，换一台机器克隆下来也不会生效。
  改成 core.hooksPath = .githooks 之后，钩子文件本身在仓库里，克隆后跑一次本脚本即可启用。

  用法（推荐双击 scripts\setup.cmd，或命令行）：
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-git-hooks.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-git-hooks.ps1 -RepoRoot "D:\code\MyLedger"

  为什么必须带 -ExecutionPolicy Bypass：Windows 默认执行策略是 Restricted，
  会直接拒绝运行本地 .ps1，报「在此系统上禁止运行脚本」。
  Bypass 只对本次进程生效，不修改系统设置，比 Set-ExecutionPolicy 干净。

  注意：core.hooksPath 是本地配置，不会随仓库推送。每次克隆/换机器后需要重新运行一次。

  关于 bash：本脚本用 `git hook run pre-commit` 触发自检，不调用 PATH 上的 bash。
  Windows 上 PATH 里的 bash 往往是 WSL 转发器，不是 Git 使用的那个 bash。
#>
[CmdletBinding()]
param(
    [string]$RepoRoot
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# 1. 定位仓库根目录
# ---------------------------------------------------------------------------
if (-not $RepoRoot) {
    $RepoRoot = (git rev-parse --show-toplevel 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $RepoRoot) {
        throw "当前目录不是 Git 仓库。先执行 git init，或用 -RepoRoot 指定仓库路径。"
    }
}
$RepoRoot = $RepoRoot.Trim()
Set-Location -LiteralPath $RepoRoot
Write-Host "仓库根目录：$RepoRoot"

# ---------------------------------------------------------------------------
# 2. 检查钩子文件
# ---------------------------------------------------------------------------
$hook = Join-Path $RepoRoot '.githooks\pre-commit'
if (-not (Test-Path -LiteralPath $hook)) {
    throw "找不到 .githooks\pre-commit，无法安装。"
}

# ---------------------------------------------------------------------------
# 3. 校验换行符：CRLF 会让 Git Bash 报 bad interpreter，钩子静默失效
# ---------------------------------------------------------------------------
$bytes = [System.IO.File]::ReadAllBytes($hook)
$hasCrlf = $false
for ($i = 1; $i -lt $bytes.Length; $i++) {
    if ($bytes[$i] -eq 10 -and $bytes[$i - 1] -eq 13) { $hasCrlf = $true; break }
}
if ($hasCrlf) {
    Write-Warning "pre-commit 是 CRLF 换行，Git Bash 可能拒绝执行，正在转换为 LF。"
    $text = [System.IO.File]::ReadAllText($hook).Replace("`r`n", "`n")
    [System.IO.File]::WriteAllText($hook, $text, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "已转换为 LF。"
}
else {
    Write-Host "换行符检查通过（LF）。"
}

# ---------------------------------------------------------------------------
# 4. 安装
# ---------------------------------------------------------------------------
git config core.hooksPath .githooks
if ($LASTEXITCODE -ne 0) { throw "写入 core.hooksPath 失败。" }
$hp = (git config --get core.hooksPath).Trim()
Write-Host "core.hooksPath = $hp（钩子已启用）"

# ---------------------------------------------------------------------------
# 5. 触发钩子的正确方式
#
# 不要直接调用 PATH 上的 bash。在 Windows 上它常指向 WSL 转发器
# (%LOCALAPPDATA%\Microsoft\WindowsApps\bash.exe)，那不是 Git 使用的 bash，
# 而且大概率被安全策略拦掉，导致自检假失败。
#
# 正确做法是用 `git hook run pre-commit`（Git >= 2.36），
# 让 Git 按它自己的规则挑选解释器，和真实提交时的执行路径完全一致。
# ---------------------------------------------------------------------------
function Test-HookRunSupported {
    $v = (git --version) -replace '[^0-9.]', ''
    $parts = @($v.Split('.') | Where-Object { $_ })
    if ($parts.Count -lt 2) { return $false }
    $major = 0; $minor = 0
    [void][int]::TryParse($parts[0], [ref]$major)
    [void][int]::TryParse($parts[1], [ref]$minor)
    return ($major -gt 2 -or ($major -eq 2 -and $minor -ge 36))
}

function Get-GitBashPath {
    $gitCmd = Get-Command git -ErrorAction SilentlyContinue
    if ($gitCmd) {
        $base = Split-Path (Split-Path $gitCmd.Source -Parent) -Parent
        foreach ($rel in @('bin\bash.exe', 'usr\bin\bash.exe')) {
            $c = Join-Path $base $rel
            if (Test-Path -LiteralPath $c) { return $c }
        }
    }
    foreach ($r in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not $r) { continue }
        $c = Join-Path $r 'Git\bin\bash.exe'
        if (Test-Path -LiteralPath $c) { return $c }
    }
    $cmd = Get-Command bash -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notmatch 'WindowsApps') { return $cmd.Source }
    return $null
}

$script:HookOutput = @()
function Invoke-PreCommitHook {
    $script:HookOutput = @()

    # 关键陷阱：native 命令配合 2>&1，在 $ErrorActionPreference='Stop' 下
    # 会把 stderr 包装成 NativeCommandError 并当作终止性错误抛出。
    # 而闸门被触发时本来就会输出拦截信息，于是自检必然崩。
    # 所以这里必须先临时降级为 Continue，跑完再恢复。
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $c = $null
    try {
        if (Test-HookRunSupported) {
            $script:HookOutput = @(git hook run pre-commit 2>&1)
            $c = $LASTEXITCODE
        }
        else {
            $bashPath = Get-GitBashPath
            if ($bashPath) {
                Write-Host "（Git 版本低于 2.36，改用 Git 自带 bash 触发自检：$bashPath）"
                $script:HookOutput = @(& $bashPath $hook 2>&1)
                $c = $LASTEXITCODE
            }
            else {
                $script:HookOutput = @("既没有 git hook run，也找不到 Git 自带 bash，无法自检。")
            }
        }
    }
    finally {
        $ErrorActionPreference = $prevEap
    }

    if ($null -eq $c) { return -1 }
    return [int]$c
}

if (-not (Test-HookRunSupported) -and -not (Get-GitBashPath)) {
    Write-Warning "无法定位可用的 bash 解释器，跳过自检。钩子本身仍然有效（Git 提交时用自己的 bash 执行）。"
}

# ---------------------------------------------------------------------------
# 6. 自检：只在暂存区为空时进行，避免动到用户已暂存的文件
# ---------------------------------------------------------------------------
$staged = @(git diff --cached --name-only)
$stagedEmpty = -not ($staged | Where-Object { $_ })

if (-not $stagedEmpty) {
    Write-Warning "暂存区非空，跳过自检。请自行确认钩子已生效。"
    return
}

# 6.1 反向自检：干净暂存区下应放行
$code = Invoke-PreCommitHook
if ($code -eq 0) {
    Write-Host "自检 1/2 通过：空暂存区下正常放行。"
}
else {
    Write-Warning "自检 1/2 失败：钩子返回 $code，请检查 .githooks/pre-commit 语法。"
    foreach ($l in $script:HookOutput) { Write-Host "      $l" }
}

# 6.2 正向自检：造一个假敏感文件，闸门必须拦住
$probe = Join-Path $RepoRoot 'imports\__selftest__.csv'
try {
    Set-Content -LiteralPath $probe -Value "交易时间,交易对方,金额`r`n2026-10-04,自检商户,1.00" -Encoding UTF8
    git add -f -- $probe 2>$null | Out-Null
    $gateCode = Invoke-PreCommitHook

    if ($gateCode -ne 0 -and $gateCode -ne -1) {
        Write-Host "自检 2/2 通过：闸门成功拦下 imports\ 下的测试文件。"
    }
    elseif ($gateCode -eq -1) {
        Write-Warning "自检 2/2 无法判定：钩子未能被触发，请手动跑一次 git commit 验证。"
    }
    else {
        Write-Warning "自检 2/2 失败：闸门没有拦住 imports\ 下的文件，不要依赖它。"
        foreach ($l in $script:HookOutput) { Write-Host "      $l" }
    }
}
finally {
    # 清理自检残留。两条纪律，都是踩过坑换来的：
    #   1. 不用 Remove-Item —— 它依赖回收站，部分安全策略/沙箱环境下会失败，
    #      一旦抛出异常，暂存区就留下幽灵条目。
    #   2. 不用无条件的 git rm —— 路径已不在暂存区时它会报 fatal，
    #      而 NativeCommandError 在 $ErrorActionPreference='Stop' 下会中断整个清理。
    # 所以先查暂存区，确认真的暂存过再 unstage；删除走 .NET，结果确定。
    $stillStaged = @(git diff --cached --name-only -- 'imports/__selftest__.csv') | Where-Object { $_ }
    if ($stillStaged) {
        $null = git rm --cached --quiet -- 'imports/__selftest__.csv' 2>&1
    }
    if ([System.IO.File]::Exists($probe)) { [System.IO.File]::Delete($probe) }
}

Write-Host ""
Write-Host "完成。以后每次 git commit 都会先过闸门。"
Write-Host "注意：core.hooksPath 是本地配置，换机器或重新克隆后需要再跑一次本脚本。"
