<#
.SYNOPSIS
  银行 APP 截图 → 结构化文本（用 Windows 自带的 OCR 引擎，全程离线）。

.DESCRIPTION
  为什么用 Windows 自带 OCR：
    它随系统安装、支持中文、完全本地运行。对比方案里，云端 OCR 会把你的
    银行卡流水上传到别人服务器——这是本方案里唯一的红线，不能碰。

  为什么要把文字按行重组：
    银行 APP 的交易明细是横向一行一条。OCR 只会按视觉顺序吐出文本片段，
    直接拼起来会丢掉「哪个金额属于哪个商户」的对应关系。
    这里用每个词的坐标把同一水平线上的片段聚成一行，再按 x 排序，
    就能还原出「时间 | 商户 | 金额」的行结构。

  输出（每个图片一份）：
    <名字>.raw.txt     OCR 原始文本行，顺序即识别顺序
    <名字>.rows.tsv    按 y 聚行、按 x 排序后的 TSV，可直接喂给导入器
    <名字>.ocr.log     识别统计（行数、引擎语言、耗时）

  用法：
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\bank-ocr.ps1 `
        -OutDir "$env:USERPROFILE\MyLedgerVault\ocr" 截图1.png 截图2.png

  截图要求：一屏一条完整列表，别把一笔交易截断在两屏之间；
           用银行 APP 的「交易明细」页，不要用首页的缩略列表。
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$Images,

    [string]$OutDir = (Join-Path $env:USERPROFILE 'MyLedgerVault\ocr'),

    [string]$Language = 'zh-Hans-CN',

    [double]$RowTolerance = 0.6
)

$ErrorActionPreference = 'Stop'

if (-not $Images -or $Images.Count -eq 0) {
    Write-Host "用法：bank-ocr.ps1 [-OutDir 目录] [-Language zh-Hans-CN] 图片1 图片2 ..."
    exit 2
}

# ---------------------------------------------------------------------------
# 载入 WinRT 类型并准备 await 桥接
#
# PowerShell 5.1 不能直接 await WinRT 的 IAsyncOperation，
# 需要把它转成 .NET Task 再同步等待。
# ---------------------------------------------------------------------------
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
    [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
    [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
    [Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
    [Windows.Storage.Streams.IRandomAccessStream, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
    [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
}
catch {
    Write-Host "无法载入 Windows OCR 组件：$($_.Exception.Message)"
    Write-Host "确认系统是 Windows 10/11，且已安装中文语言包。"
    exit 3
}

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object {
        $_.Name -eq 'AsTask' -and
        $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]

function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}

function Get-OcrEngine {
    param([string]$Lang)
    # 优先指定语言，不成就回退到用户配置语言（通常是中文系统）
    try {
        $language = New-Object Windows.Globalization.Language($Lang)
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
        if ($engine) { return $engine }
    }
    catch { }
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    if ($engine) { return $engine }
    throw "系统里没有可用的 OCR 语言包。到「设置 → 时间和语言 → 语言」里安装中文语言包。"
}

$engine = Get-OcrEngine -Lang $Language
Write-Host "OCR 引擎语言：$($engine.RecognizerLanguage.LanguageTag)"

if (-not (Test-Path -LiteralPath $OutDir)) {
    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
}

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$totalRows = 0

foreach ($image in $Images) {
    if (-not (Test-Path -LiteralPath $image)) {
        Write-Host "跳过（找不到）：$image"
        continue
    }

    $full = (Resolve-Path -LiteralPath $image).Path
    $base = [System.IO.Path]::GetFileNameWithoutExtension($full)
    $sw = [System.Diagnostics.Stopwatch]::StartNew()

    try {
        $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($full)) ([Windows.Storage.StorageFile])
        $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    }
    catch {
        Write-Host "识别失败 $base：$($_.Exception.Message)"
        continue
    }

    # -----------------------------------------------------------------------
    # 1) 原始文本行
    # -----------------------------------------------------------------------
    $rawLines = @()
    foreach ($line in $result.Lines) { $rawLines += $line.Text }
    $rawPath = Join-Path $OutDir "$base.raw.txt"
    [System.IO.File]::WriteAllLines($rawPath, $rawLines, $utf8NoBom)

    # -----------------------------------------------------------------------
    # 2) 带坐标的词 → 按 y 聚行 → 按 x 排序
    # -----------------------------------------------------------------------
    $words = @()
    foreach ($line in $result.Lines) {
        foreach ($word in $line.Words) {
            $rect = $word.BoundingRect
            $words += [pscustomobject]@{
                Text = $word.Text
                Left = [double]$rect.X
                Top  = [double]$rect.Y
                Cy   = [double]$rect.Y + [double]$rect.Height / 2
                H    = [double]$rect.Height
            }
        }
    }

    if ($words.Count -eq 0) {
        Write-Host "$base：没有识别到任何文字（图片可能太小或没有文字）"
        continue
    }

    $medianHeight = ($words | Sort-Object H | Select-Object -Index ([int]($words.Count / 2))).H
    if ($medianHeight -le 0) { $medianHeight = 12 }
    $tol = [Math]::Max(3, $medianHeight * $RowTolerance)

    $sorted = $words | Sort-Object Cy
    $rows = New-Object System.Collections.Generic.List[object]
    foreach ($word in $sorted) {
        $placed = $false
        foreach ($row in $rows) {
            if ([Math]::Abs($row.Cy - $word.Cy) -le $tol) {
                $row.Words.Add($word)
                $row.Cy = ($row.Cy * ($row.Words.Count - 1) + $word.Cy) / $row.Words.Count
                $placed = $true
                break
            }
        }
        if (-not $placed) {
            $newRow = [pscustomobject]@{ Cy = $word.Cy; Words = (New-Object System.Collections.Generic.List[object]) }
            $newRow.Words.Add($word)
            $rows.Add($newRow)
        }
    }

    $tsvLines = $rows | Sort-Object Cy | ForEach-Object {
        ($_.Words | Sort-Object Left | ForEach-Object { $_.Text }) -join "`t"
    }
    $tsvPath = Join-Path $OutDir "$base.rows.tsv"
    [System.IO.File]::WriteAllLines($tsvPath, $tsvLines, $utf8NoBom)

    $sw.Stop()
    $totalRows += $rows.Count
    $log = @(
        "图片      : $full"
        "识别行数  : $($result.Lines.Count)"
        "重组行数  : $($rows.Count)"
        "词数      : $($words.Count)"
        "引擎语言  : $($engine.RecognizerLanguage.LanguageTag)"
        "耗时      : $([math]::Round($sw.Elapsed.TotalSeconds, 2)) 秒"
    )
    [System.IO.File]::WriteAllLines((Join-Path $OutDir "$base.ocr.log"), $log, $utf8NoBom)

    Write-Host "$base：识别 $($result.Lines.Count) 行 → 重组 $($rows.Count) 行"
    Write-Host "  $rawPath"
    Write-Host "  $tsvPath"
}

Write-Host ""
Write-Host "完成。共重组 $totalRows 行。"
Write-Host "下一步：检查 .rows.tsv 里每行的字段顺序是否合理，再执行"
Write-Host "  python -m importer import <输出目录>\*.rows.tsv --source bank"
Write-Host ""
Write-Host "注意：银行截图的字段顺序各家不同，第一次用请先人工核对前几行。"
Write-Host "      识别错一个数字不会报错，只会让你的月支出悄悄多出一笔。"
