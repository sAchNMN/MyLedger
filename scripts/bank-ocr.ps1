<#
.SYNOPSIS
  银行 APP 截图 → 结构化文本（Windows 自带 OCR，全程离线）。

.DESCRIPTION
  为什么必须切片：
    Windows OCR 单张图有尺寸上限（OcrEngine.MaxImageDimension，通常是 2600px）。
    超限时它不报错，而是**静默把整张图缩到上限再识别**。
    实测一张 1216 x 9456 的手机长截图被压到 27% 后文字全糊，
    冒号、小数点、负号一个都认不出来 —— 时间和金额全废。
    这不是 OCR 引擎不行，是喂给它的图太大。

    所以按高度切片：每片不超过上限，识别完把坐标折算回原图合并。
    顺带每片放大 2 倍，小字识别率还能再上一截。

  为什么按行重组 + 按间距拼字段：
    1. OCR 只按视觉顺序吐文字片段，不告诉你「哪个金额属于哪个商户」。
       用每个词的中心 y 把同一水平线的片段聚成一行，还原行结构。
    2. Windows OCR 对中文经常**按单字返回 Word**，直接 join 会把
       「-38.50」拆成三格。行内按 x 排序后用字符间距判断词内 / 换列。

  输出（每张图一份）：
    <名字>.raw.txt     OCR 原始文本行
    <名字>.rows.tsv    按行重组、按列拼接后的 TSV，可直接喂给导入器
    <名字>.ocr.log     识别统计（切片数、尺寸、阈值、耗时）

  用法：
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\bank-ocr.ps1 `
        -OutDir "$env:USERPROFILE\MyLedgerVault\ocr" 截图1.png 截图2.png

  截图建议：一屏一条完整列表，别把一笔交易截断在两屏之间；
           长截图没问题，本脚本自己会切。
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$Images,

    [string]$OutDir = (Join-Path $env:USERPROFILE 'MyLedgerVault\ocr'),

    [string]$Language = 'zh-Hans-CN',

    # 每片在原图上的高度。配合 Scale 使用，保证「片高 x Scale」不超过 OCR 上限。
    # 默认值不需要切：本机 OcrEngine.MaxImageDimension = 10000px，
    # 一般手机长截图（约 9500px）本来就装得下。这个参数是给真正超长的图兜底的。
    [int]$StripHeight = 4000,

    # 相邻片的重叠高度，防止正好切在字上。
    [int]$Overlap = 100,

    # 放大倍数。**默认 1.0：实测放大反而更差。**
    # 引擎会自己决定缩放；人为放大后再喂给它，一旦「放大后尺寸」超过上限，
    # 它会把图压回上限，结果比不放还糊。实测 2x / 3x 的识别质量都明显低于 1x。
    [double]$Scale = 1.0,

    # 同一水平线聚成一行时，y 差的容忍度（相对中位行高）
    [double]$RowTolerance = 0.6
)

$ErrorActionPreference = 'Stop'

if (-not $Images -or $Images.Count -eq 0) {
    Write-Host "用法：bank-ocr.ps1 [-OutDir 目录] [-StripHeight 1200] [-Scale 2.0] 图片1 图片2 ..."
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
    [Windows.Graphics.Imaging.BitmapTransform, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.BitmapBounds, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.BitmapPixelFormat, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.BitmapAlphaMode, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.ExifOrientationMode, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.ColorManagementMode, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
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
$engineMax = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension
Write-Host "OCR 引擎语言：$($engine.RecognizerLanguage.LanguageTag)  单图上限：${engineMax}px"

# 片高和放大倍数必须服从 OCR 上限，否则又会被静默压缩
$effScale = $Scale
if ($StripHeight * $effScale -gt ($engineMax - 20)) {
    $effScale = [Math]::Max(1.0, ($engineMax - 20) / $StripHeight)
    Write-Host "  片高 ${StripHeight} x 放大 ${Scale} 超过上限，自动把放大倍数降到 $([math]::Round($effScale, 2))"
}

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
    }
    catch {
        Write-Host "识别失败 $base：$($_.Exception.Message)"
        continue
    }

    $imgW = [int]$decoder.PixelWidth
    $imgH = [int]$decoder.PixelHeight

    # -----------------------------------------------------------------------
    # 切片参数
    #
    # 关键：要比较的是**放大后**的高度，不是原图高度。
    # 原图 9456 < 上限 10000 看着没问题，但再放大 2 倍就是 18912，
    # 引擎会把它压回上限 —— 等于白放大，字反而比不放还小。
    # 这个判据写错过一次，代价是识别质量掉了一半。
    # -----------------------------------------------------------------------
    $maxScaled = $engineMax - 20
    if ([Math]::Round($imgH * $effScale) -le $maxScaled) {
        $stripH = $imgH                      # 整图放大后仍不超限，一片就够
    }
    else {
        $stripH = [Math]::Min($StripHeight, [Math]::Floor($maxScaled / $effScale))
        if ($stripH -lt 200) { $stripH = 200 }
    }

    $overlap = [Math]::Min($Overlap, [Math]::Max(0, $stripH - 50))
    $step = [Math]::Max(50, $stripH - $overlap)

    $strips = @()
    $y = 0
    while ($y -lt $imgH) {
        $h = [Math]::Min($stripH, $imgH - $y)
        $strips += [pscustomobject]@{ Y = $y; H = $h }
        if (($y + $h) -ge $imgH) { break }
        $y += $step
    }

    Write-Host "$base：$imgW x $imgH 像素，切 $($strips.Count) 片（片高 $stripH / 重叠 $overlap / 放大 $([math]::Round($effScale, 2))x）"

    # -----------------------------------------------------------------------
    # 逐片 OCR，坐标折算回原图
    # -----------------------------------------------------------------------
    $words = @()
    $rawLines = @()
    $lineCount = 0
    $failStrips = 0

    foreach ($strip in $strips) {
        try {
            $bounds = New-Object Windows.Graphics.Imaging.BitmapBounds
            $bounds.X = [uint32]0
            $bounds.Y = [uint32]$strip.Y
            $bounds.Width = [uint32]$imgW
            $bounds.Height = [uint32]$strip.H

            $transform = New-Object Windows.Graphics.Imaging.BitmapTransform
            $transform.Bounds = $bounds
            $transform.ScaledWidth = [uint32][Math]::Round($imgW * $effScale)
            $transform.ScaledHeight = [uint32][Math]::Round($strip.H * $effScale)

            $bitmap = Await ($decoder.GetSoftwareBitmapAsync(
                [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,
                [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied,
                $transform,
                [Windows.Graphics.Imaging.ExifOrientationMode]::IgnoreExifOrientation,
                [Windows.Graphics.Imaging.ColorManagementMode]::DoNotColorManage
            )) ([Windows.Graphics.Imaging.SoftwareBitmap])

            $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        }
        catch {
            Write-Host "  第 $($strip.Y)px 处的切片失败：$($_.Exception.Message)"
            $failStrips++
            continue
        }

        $linesHere = @($result.Lines)
        $lineCount += $linesHere.Count
        foreach ($line in $linesHere) { $rawLines += $line.Text }

        foreach ($line in $linesHere) {
            foreach ($word in $line.Words) {
                $rect = $word.BoundingRect
                # 缩放坐标 → 原图坐标
                $origCy = $strip.Y + ($rect.Y + $rect.Height / 2) / $effScale
                $origX = $rect.X / $effScale
                $origW = $rect.Width / $effScale
                $origH = $rect.Height / $effScale

                # 重叠区去重：第 2 片起，落在本条顶部重叠区里的词上一片已经处理过
                if ($strip.Y -gt 0 -and $origCy -lt ($strip.Y + $overlap * 0.5)) { continue }

                $words += [pscustomobject]@{
                    Text  = $word.Text
                    Left  = [double]$origX
                    Width = [double]$origW
                    Cy    = [double]$origCy
                    H     = [double]$origH
                }
            }
        }
    }

    $rawPath = Join-Path $OutDir "$base.raw.txt"
    [System.IO.File]::WriteAllLines($rawPath, $rawLines, $utf8NoBom)

    if ($words.Count -eq 0) {
        Write-Host "$base：没有识别到任何文字。"
        [System.IO.File]::WriteAllLines((Join-Path $OutDir "$base.ocr.log"), @(
            "图片      : $full"
            "尺寸      : $imgW x $imgH 像素"
            "切片      : $($strips.Count) 片，失败 $failStrips"
            "结果      : 没有识别到任何文字"
        ), $utf8NoBom)
        continue
    }

    # -----------------------------------------------------------------------
    # 按 y 聚行
    # -----------------------------------------------------------------------
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

    # -----------------------------------------------------------------------
    # 行内用字符间距把碎片拼成字段
    # -----------------------------------------------------------------------
    $sortedWidths = @($words | Where-Object { $_.Width -gt 0 } | ForEach-Object { $_.Width } | Sort-Object)
    $medianWidth = if ($sortedWidths.Count -gt 0) {
        $sortedWidths[[int]($sortedWidths.Count / 2)]
    } else { 12 }
    $gapThreshold = [Math]::Max(2.5, $medianWidth * 0.8)

    $tsvLines = New-Object System.Collections.Generic.List[string]
    $rejoined = 0
    foreach ($row in ($rows | Sort-Object Cy)) {
        $ws = @($row.Words | Sort-Object Left)
        $fields = New-Object System.Collections.Generic.List[string]
        $buf = ""
        $prevRight = $null
        foreach ($w in $ws) {
            if ($null -ne $prevRight) {
                $gap = $w.Left - $prevRight
                if (($gap -gt $gapThreshold) -or ($gap -lt ($gapThreshold * -1))) {
                    if ($buf) { $fields.Add($buf); $buf = "" }
                }
            }
            $buf += $w.Text
            $prevRight = $w.Left + $w.Width
        }
        if ($buf) { $fields.Add($buf) }
        if ($fields.Count -lt $row.Words.Count) { $rejoined++ }
        $tsvLines.Add(($fields -join "`t"))
    }

    $tsvPath = Join-Path $OutDir "$base.rows.tsv"
    [System.IO.File]::WriteAllLines($tsvPath, $tsvLines, $utf8NoBom)

    $sw.Stop()
    $totalRows += $rows.Count
    $log = @(
        "图片      : $full"
        "尺寸      : $imgW x $imgH 像素"
        "切片      : $($strips.Count) 片（片高 $stripH / 重叠 $overlap / 放大 $([math]::Round($effScale, 2))x）"
        "识别行数  : $lineCount"
        "重组行数  : $($rows.Count)"
        "词块数    : $($words.Count)"
        "中位字宽  : $([math]::Round($medianWidth, 1)) px"
        "列间距阈值: $([math]::Round($gapThreshold, 1)) px"
        "发生拼接  : $rejoined 行"
        "引擎语言  : $($engine.RecognizerLanguage.LanguageTag)"
        "耗时      : $([math]::Round($sw.Elapsed.TotalSeconds, 2)) 秒"
    )
    [System.IO.File]::WriteAllLines((Join-Path $OutDir "$base.ocr.log"), $log, $utf8NoBom)

    Write-Host "  → 识别 $lineCount 行 / 重组 $($rows.Count) 行，拼接 $rejoined 行，耗时 $([math]::Round($sw.Elapsed.TotalSeconds, 1))s"
    Write-Host "     $rawPath"
    Write-Host "     $tsvPath"
}

Write-Host ""
Write-Host "完成。共重组 $totalRows 行。"
Write-Host "下一步：核对 .rows.tsv 的字段顺序，再执行"
Write-Host "  python -m importer import <输出目录>\*.rows.tsv --source bank"
