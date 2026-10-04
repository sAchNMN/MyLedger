<#
.SYNOPSIS
  MyLedger 真实账单加解密工具（7-Zip AES-256 + 加密文件名）。

.DESCRIPTION
  设计原则：
    1. 密码永不落盘 —— 每次操作交互输入，不写入脚本、不写入环境变量、不写入任何文件。
    2. 密文与仓库分离 —— vault 目录必须在 Git 仓库之外。
    3. 加密文件名 —— 使用 -mhe=on，别人连「归档里有几个文件、叫什么名字」都看不到。

  为什么用 7-Zip：本机已装（C:\Program Files\7-Zip\7z.exe），零安装成本；
  7z 格式支持 AES-256 + 头部加密，对账单场景完全够用，且不会因为「忘了关服务」而失效。

  用法（推荐双击 scripts\vault.cmd，或命令行）：
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\vault.ps1 init     创建仓库外的 vault 目录
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\vault.ps1 open     解密 vault.7z 到工作区 plain\
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\vault.ps1 add      把 plain\ 内容打包加密进 vault.7z
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\vault.ps1 close    清空解密出的明文工作区
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\vault.ps1 status   查看状态与安全检查

  为什么必须带 -ExecutionPolicy Bypass：Windows 默认执行策略是 Restricted，
  会直接拒绝运行本地 .ps1，报「在此系统上禁止运行脚本」。Bypass 只对本次进程生效。

  可选参数：
    -VaultPath "D:\SomeWhere\MyVault"   自定义 vault 位置（默认 %USERPROFILE%\MyLedgerVault）
    -Force                              跳过交互确认（仅用于 open / add / close）

  说明：本脚本为毫秒级轻量操作，不涉及 CPU/GPU 密集计算或大流量网络传输，
        因此未内置电源闸门。后续做批量加解密时再加。
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('init', 'open', 'add', 'close', 'status')]
    [string]$Command = 'status',

    [string]$VaultPath,

    [switch]$Force
)

$ErrorActionPreference = 'Stop'

if (-not $VaultPath) { $VaultPath = Join-Path $env:USERPROFILE 'MyLedgerVault' }
$VaultPath = [System.IO.Path]::GetFullPath($VaultPath)
$Archive = Join-Path $VaultPath 'myledger-vault.7z'
$PlainDir = Join-Path $VaultPath 'plain'

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

function Get-SevenZip {
    $roots = @($env:ProgramFiles, ${env:ProgramFiles(x86)}) | Where-Object { $_ }
    foreach ($r in $roots) {
        $c = Join-Path $r '7-Zip\7z.exe'
        if (Test-Path -LiteralPath $c) { return $c }
    }
    $cmd = Get-Command 7z -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "找不到 7z.exe。安装方法：winget install 7zip.7zip"
}

function Read-VaultPassword {
    param([string]$Prompt)
    $sec = Read-Host -Prompt $Prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function Get-PlainFiles {
    @(Get-ChildItem -LiteralPath $PlainDir -Recurse -Force -File -ErrorAction SilentlyContinue)
}

function Test-InsideGitRepo {
    param([string]$Path)
    # 关键陷阱：native 命令的 stderr 在 $ErrorActionPreference='Stop' 下会被
    # 包装成 NativeCommandError 并终止脚本。路径不在任何 Git 仓库里时 git 必然写
    # stderr，所以这里必须临时把策略降级为 Continue，跑完再恢复。
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $r = @(git -C $Path rev-parse --show-toplevel 2>&1) | Where-Object { $_ }
        if ($LASTEXITCODE -eq 0 -and $r) {
            return ([string]($r | Select-Object -First 1)).Trim()
        }
        return $null
    }
    finally {
        $ErrorActionPreference = $prev
    }
}

function Invoke-SevenZip {
    param([string[]]$Arguments)
    # 同理：7z 在密码错误、归档损坏时会把信息写 stderr，
    # 不降级策略的话脚本会在能读取退出码之前就抛异常。
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& (Get-SevenZip) @Arguments 2>&1)
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $prev
    }
    if ($null -eq $code) { $code = -1 }
    return [pscustomobject]@{ ExitCode = [int]$code; Output = $output }
}

function Get-VaultSafetyIssues {
    $issues = @()

    if (Test-Path -LiteralPath $VaultPath) {
        $repo = Test-InsideGitRepo -Path $VaultPath
        if ($repo) {
            $issues += "vault 位于 Git 仓库内（$repo）—— 一旦 git add -f 就会把密文和明文一起提交，必须移到仓库外。"
        }
    }

    $cloud = 'OneDrive|Dropbox|坚果云|Nutstore|Google Drive|百度网盘|iCloud|Quark|夸克'
    if ($VaultPath -match $cloud) {
        $issues += "vault 路径命中云同步目录关键词（$($Matches[0])）—— 密文会被自动上传到云端，请移出同步目录。"
    }

    return $issues
}

function Confirm-Action {
    param([string]$Prompt)
    if ($Force) { return $true }
    $ans = Read-Host -Prompt "$Prompt (y/N)"
    return ($ans -match '^[Yy]')
}

# ---------------------------------------------------------------------------
# 命令实现
# ---------------------------------------------------------------------------

function Invoke-Init {
    New-Item -ItemType Directory -Force -Path $VaultPath | Out-Null
    New-Item -ItemType Directory -Force -Path $PlainDir | Out-Null

    Write-Host "已创建 vault 目录：$VaultPath"
    Write-Host ""
    Write-Host "接下来："
    Write-Host "  1. 把真实账单（支付宝/微信/银行导出 CSV）放进 $PlainDir"
    Write-Host "  2. 执行  add  打包加密"
    Write-Host "  3. 执行  close 清掉明文"
    Write-Host ""
    Write-Host "必须遵守的两条："
    Write-Host "  - 不要把本目录放进任何云盘同步目录（OneDrive / 坚果云 / 百度网盘）。"
    Write-Host "  - 不要把本目录放在 Git 仓库里。"
    Write-Host ""
    Write-Host "密码自己记牢。7z 的 AES-256 没有找回途径，密码丢了数据就没了。"
}

function Invoke-Open {
    if (-not (Test-Path -LiteralPath $Archive)) {
        throw "找不到密文归档：$Archive（先用 add 创建）"
    }
    $existing = Get-PlainFiles
    if ($existing.Count -gt 0) {
        if (-not (Confirm-Action "明文工作区已有 $($existing.Count) 个文件，继续解压会覆盖同名文件。继续？")) {
            Write-Host "已取消。"; return
        }
    }
    New-Item -ItemType Directory -Force -Path $PlainDir | Out-Null

    $pw = Read-VaultPassword "输入 vault 密码"
    $res = Invoke-SevenZip -Arguments @('x', $Archive, "-o$PlainDir", "-p$pw", '-aoa', '-y', '-bso0', '-bsp0')
    $pw = $null
    if ($res.ExitCode -ne 0) {
        throw "解密失败（7z 退出码 $($res.ExitCode)）。密码错误或归档损坏。"
    }

    $files = Get-PlainFiles
    Write-Host "已解密 $($files.Count) 个文件到：$PlainDir"
    Write-Host ""
    Write-Warning "明文已落到磁盘。测完立刻执行 close 清空，不要让明文长期留在硬盘上。"
}

function Invoke-Add {
    $files = Get-PlainFiles
    if ($files.Count -eq 0) {
        throw "明文工作区是空的：$PlainDir（先把真实账单放进去）"
    }
    if (Test-Path -LiteralPath $Archive) {
        if (-not (Confirm-Action "密文归档已存在，将整体覆盖：$Archive。继续？")) {
            Write-Host "已取消。"; return
        }
    }
    New-Item -ItemType Directory -Force -Path $VaultPath | Out-Null

    $pw1 = Read-VaultPassword "设置 vault 密码（务必记牢，无法找回）"
    $pw2 = Read-VaultPassword "再输一次确认"
    if ($pw1 -ne $pw2) { $pw1 = $null; $pw2 = $null; throw "两次密码不一致，已取消。" }
    if ($pw1.Length -lt 12) { Write-Warning "密码短于 12 位，强度不足，建议加长。" }

    Push-Location -LiteralPath $PlainDir
    try {
        $res = Invoke-SevenZip -Arguments @('a', '-t7z', $Archive, '*', '-mhe=on', '-mx=9', "-p$pw1", '-y', '-bso0', '-bsp0')
    }
    finally {
        Pop-Location
    }
    $pw1 = $null
    $pw2 = $null
    if ($res.ExitCode -ne 0) { throw "加密打包失败（7z 退出码 $($res.ExitCode)）。" }

    if (Test-Path -LiteralPath $Archive) {
        $size = (Get-Item -LiteralPath $Archive).Length
        Write-Host "已生成密文归档：$Archive （$([math]::Round($size / 1KB, 1)) KB，AES-256 + 加密文件名）"
        Write-Host "包含 $($files.Count) 个文件。"
        Write-Host ""
        Write-Host "下一步：执行 close 清掉明文工作区。"
    }
}

function Invoke-Close {
    $files = Get-PlainFiles
    if ($files.Count -eq 0) {
        Write-Host "明文工作区已经是空的：$PlainDir"
        return
    }
    if (-not (Confirm-Action "即将删除 $PlainDir 下 $($files.Count) 个明文文件。确认？")) {
        Write-Host "已取消。"; return
    }

    Get-ChildItem -LiteralPath $PlainDir -Force -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

    $left = Get-PlainFiles
    if ($left.Count -eq 0) {
        Write-Host "已清空明文工作区。"
    }
    else {
        Write-Warning "仍有 $($left.Count) 个文件未能删除，请手动检查：$PlainDir"
    }
    Write-Host "提示：SSD 上覆写无意义（闪存有磨损均衡，原数据副本可能仍在），"
    Write-Host "      真正防「文件被拿走」靠的是 BitLocker 整盘加密，不是文件级擦除。"
}

function Invoke-Status {
    Write-Host "vault 目录   : $VaultPath"
    if (Test-Path -LiteralPath $Archive) {
        $item = Get-Item -LiteralPath $Archive
        Write-Host "密文归档     : 存在，$([math]::Round($item.Length / 1KB, 1)) KB，最后修改 $($item.LastWriteTime)"
    }
    else {
        Write-Host "密文归档     : 不存在"
    }

    if (Test-Path -LiteralPath $PlainDir) {
        $files = Get-PlainFiles
        $count = $files.Count
        Write-Host "明文工作区   : $count 个文件"
        if ($count -gt 0) {
            Write-Warning "当前磁盘上存在明文账单。测完请执行 close。"
            $files | Select-Object -First 20 | ForEach-Object { Write-Host "               $($_.FullName)" }
            if ($count -gt 20) { Write-Host "               ...（其余 $($count - 20) 个省略）" }
        }
    }
    else {
        Write-Host "明文工作区   : 目录不存在"
    }

    $sevenZip = $null
    try { $sevenZip = Get-SevenZip; Write-Host "7-Zip        : $sevenZip" }
    catch { Write-Warning "7-Zip        : 未找到，加解密命令不可用。" }

    $issues = Get-VaultSafetyIssues
    Write-Host ""
    if ($issues.Count -eq 0) {
        Write-Host "安全检查     : 通过（vault 不在 Git 仓库内，也不在云同步目录下）"
    }
    else {
        foreach ($i in $issues) { Write-Warning "安全检查     : $i" }
    }
}

switch ($Command) {
    'init' { Invoke-Init }
    'open' { Invoke-Open }
    'add' { Invoke-Add }
    'close' { Invoke-Close }
    'status' { Invoke-Status }
}
