<#
.SYNOPSIS
    把手机上的一个故事项目完整拉到 PC，并留一份原样备份。

.DESCRIPTION
    依赖 usb-bridge.ps1 建立的 USB 通道（本脚本内部自动调用它）。令牌只在本进程内传递，
    既不打印也不落盘，所以整个过程不会把引擎访问令牌留在终端历史里。

    产物布局：
        <Dest>\<时间戳>\original\      逐字节原样备份，文件会被置为只读，作业中不要改动
        <Dest>\<时间戳>\work\          工作副本，标准化改动都在这里做
        <Dest>\<时间戳>\manifest.json  每个文件的设备路径、大小、修改时间和 SHA256

    manifest 是为回写准备的：比对 work 与 original 得出改动清单，比对 manifest 与设备现状
    确认期间手机上没有别的改动，最后只写真正变化的那几个文件。

    本脚本只读，不向设备写入任何内容。

.PARAMETER Project
    stories 下的故事项目目录名，默认「测试故事001」。

.PARAMETER Dest
    PC 侧产物根目录，默认 %LOCALAPPDATA%\Storydex\story-pull（在仓库之外，不会被误提交）。

.PARAMETER MaxFileBytes
    单文件大小上限，超过则跳过并记入 manifest 的 skipped，默认 8 MiB。
    故事项目里正常只有 markdown 和 json；触发这个上限说明有意料之外的大文件，值得先看一眼。

.EXAMPLE
    .\pull-story-project.ps1
    拉取「测试故事001」。

.EXAMPLE
    .\pull-story-project.ps1 -Project 测试故事002
#>
[CmdletBinding()]
param(
    [string]$Project = '测试故事001',
    [string]$Dest,
    [int]$LocalPort = 18765,
    [string]$Adb,
    [long]$MaxFileBytes = 8MB
)

$ErrorActionPreference = 'Stop'
# PS 5.1 的进度条会让每次 Invoke-WebRequest 慢一个数量级，逐文件拉取时差别很明显。
$ProgressPreference = 'SilentlyContinue'

if (-not $Dest) { $Dest = Join-Path $env:LOCALAPPDATA 'Storydex\story-pull' }

$bridgeScript = Join-Path $PSScriptRoot 'usb-bridge.ps1'
if (-not (Test-Path $bridgeScript)) { throw "找不到 usb-bridge.ps1：$bridgeScript" }

# 6>$null 吞掉桥脚本的诊断输出（Write-Host 走信息流），只留 -PassThru 的返回对象。
$bridgeArgs = @{ PassThru = $true; LocalPort = $LocalPort }
if ($Adb) { $bridgeArgs['Adb'] = $Adb }
$bridge = & $bridgeScript @bridgeArgs 6>$null
if (-not $bridge -or -not $bridge.Token) {
    throw 'USB 桥未就绪。请先单独运行 usb-bridge.ps1 看具体是哪一步失败。'
}

$baseUrl = $bridge.BaseUrl
$headers = @{ Authorization = "Bearer $($bridge.Token)" }
$deviceRoot = "$($bridge.StoriesDir)/$Project"

function Invoke-FsList {
    param([string]$DevicePath)
    $uri = "$baseUrl/api/fs/list?path=$([uri]::EscapeDataString($DevicePath))"
    # 不用 Invoke-RestMethod：引擎的 JSON 响应头没带 charset，PS 5.1 就按 Latin-1 解码，
    # 中文文件名会变成乱码并导致后续 fs_raw 404。这里自己按 UTF-8 解字节。
    $response = Invoke-WebRequest -Uri $uri -Headers $headers -TimeoutSec 30 -UseBasicParsing
    [System.Text.Encoding]::UTF8.GetString($response.RawContentStream.ToArray()) | ConvertFrom-Json
}

function Save-FsFile {
    param([string]$DevicePath, [string]$OutFile)
    $uri = "$baseUrl/api/fs/raw?path=$([uri]::EscapeDataString($DevicePath))"
    # -OutFile 直接落原始字节，不经文本解码，中文和换行都不会被改写。
    Invoke-WebRequest -Uri $uri -Headers $headers -TimeoutSec 60 -UseBasicParsing -OutFile $OutFile | Out-Null
    # 空文件时 PS 可能不创建目标，补一个 0 字节文件让清单和磁盘一致。
    if (-not (Test-Path $OutFile)) { New-Item -ItemType File -Path $OutFile | Out-Null }
}

try { $null = Invoke-FsList -DevicePath $deviceRoot } catch {
    throw "读不到故事项目 $deviceRoot：$($_.Exception.Message)"
}

$stamp = (Get-Date).ToString('yyyyMMdd-HHmmss')
$runDir = Join-Path $Dest $stamp
$originalDir = Join-Path $runDir 'original'
$workDir = Join-Path $runDir 'work'
New-Item -ItemType Directory -Force -Path $originalDir | Out-Null

Write-Host "拉取 $deviceRoot" -ForegroundColor Cyan
Write-Host "  → $originalDir" -ForegroundColor DarkGray

$queue = New-Object System.Collections.Queue
$queue.Enqueue([pscustomobject]@{ Device = $deviceRoot; Relative = '' })
$records = New-Object System.Collections.ArrayList
$skipped = New-Object System.Collections.ArrayList
$dirs = New-Object System.Collections.ArrayList
$totalBytes = 0L

while ($queue.Count -gt 0) {
    $node = $queue.Dequeue()
    foreach ($entry in @((Invoke-FsList -DevicePath $node.Device).entries)) {
        $childDevice = "$($node.Device)/$($entry.name)"
        $childRelative = if ($node.Relative) { "$($node.Relative)/$($entry.name)" } else { $entry.name }
        $localPath = Join-Path $originalDir ($childRelative -replace '/', '\')

        if ($entry.is_dir) {
            New-Item -ItemType Directory -Force -Path $localPath | Out-Null
            [void]$dirs.Add($childRelative)
            $queue.Enqueue([pscustomobject]@{ Device = $childDevice; Relative = $childRelative })
            continue
        }

        if ([long]$entry.size -gt $MaxFileBytes) {
            [void]$skipped.Add([pscustomobject]@{ path = $childRelative; size = $entry.size; reason = 'exceeds MaxFileBytes' })
            Write-Host "  跳过（过大 $([long]$entry.size) 字节）$childRelative" -ForegroundColor Yellow
            continue
        }

        $parent = Split-Path -Parent $localPath
        if ($parent -and -not (Test-Path $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
        Save-FsFile -DevicePath $childDevice -OutFile $localPath

        $local = Get-Item -LiteralPath $localPath
        if ($local.Length -ne [long]$entry.size) {
            throw "传输不完整：$childRelative 设备 $($entry.size) 字节，本地 $($local.Length) 字节。"
        }
        $totalBytes += $local.Length
        [void]$records.Add([pscustomobject]@{
            path       = $childRelative
            devicePath = $childDevice
            size       = $local.Length
            modified   = $entry.modified
            sha256     = (Get-FileHash -LiteralPath $localPath -Algorithm SHA256).Hash
        })
    }
}

# 工作副本先拷出来，再把 original 整体置为只读——顺序反了会把只读位一起复制过去。
Copy-Item -LiteralPath $originalDir -Destination $workDir -Recurse -Force
Get-ChildItem -LiteralPath $originalDir -Recurse -File | ForEach-Object { $_.IsReadOnly = $true }

$manifest = [ordered]@{
    project     = $Project
    deviceRoot  = $deviceRoot
    pulledAt    = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    devicePort  = $bridge.DevicePort
    sandboxRoot = $bridge.Cwd
    dirCount    = $dirs.Count
    fileCount   = $records.Count
    totalBytes  = $totalBytes
    directories = @($dirs)
    files       = @($records)
    skipped     = @($skipped)
}
# 不用 Set-Content -Encoding utf8：PS 5.1 会写 BOM，严格 JSON 解析器会被它噎住。
[System.IO.File]::WriteAllText(
    (Join-Path $runDir 'manifest.json'),
    ($manifest | ConvertTo-Json -Depth 6),
    (New-Object System.Text.UTF8Encoding($false)))

Write-Host ''
Write-Host "✓ 已拉取 $($records.Count) 个文件、$($dirs.Count) 个目录，共 $([math]::Round($totalBytes / 1KB, 1)) KiB" -ForegroundColor Green
if ($skipped.Count -gt 0) { Write-Host "  有 $($skipped.Count) 个文件因超过大小上限被跳过，见 manifest.skipped" -ForegroundColor Yellow }
Write-Host "  原样备份（只读）  $originalDir"
Write-Host "  工作副本          $workDir"
Write-Host "  清单              $(Join-Path $runDir 'manifest.json')"
Write-Host ''
Write-Host '标准化请只改 work 下的文件；original 保留为对照，回写前用它算 diff。' -ForegroundColor Cyan
