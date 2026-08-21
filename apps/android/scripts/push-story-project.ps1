<#
.SYNOPSIS
    把标准化过的工作副本回写到手机上的故事项目。

.DESCRIPTION
    pull-story-project.ps1 的逆操作，也是唯一会向设备写入的脚本。默认只打印计划，
    必须显式加 -Apply 才真正写入。

    改动清单不是靠人列的，而是逐字节比对 work\ 与 original\ 算出来的：
        · original 有、work 没有                → 删除
        · work 有、original 没有                → 新建
        · 两边都有但 SHA256 不同                → 覆盖
        · 一删一增且 SHA256 相同                → 认成移动，走 fs/rename 一次完成
    只有真正变化的文件会被碰，其余一律不动。

    写入前有两道闸门，都是为了不覆盖手机上后来的改动：
        1. 逐个重新下载「即将被碰的文件」，SHA256 必须与 manifest 一致；
        2. 整树重新枚举，文件清单与大小必须与 manifest 一致。
    任何一条不符就中止——那说明拉取之后手机上又推进了剧情，此时回写会把新回合冲掉，
    正确做法是重新 pull、重新标准化、再 push。

.PARAMETER RunDir
    pull-story-project.ps1 产出的那一层目录（内含 original\、work\、manifest.json）。
    省略时取 %LOCALAPPDATA%\Storydex\story-pull 下时间戳最新的一次。

.PARAMETER Apply
    真正写入设备。不加则只打印计划。

.EXAMPLE
    .\push-story-project.ps1
    预览最近一次拉取的回写计划。

.EXAMPLE
    .\push-story-project.ps1 -Apply
#>
[CmdletBinding()]
param(
    [string]$RunDir,
    [switch]$Apply,
    [int]$LocalPort = 18765,
    [string]$Adb
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# ── 定位本次拉取目录 ──
if (-not $RunDir) {
    $root = Join-Path $env:LOCALAPPDATA 'Storydex\story-pull'
    if (-not (Test-Path $root)) { throw "没有任何拉取记录：$root" }
    $latest = Get-ChildItem -LiteralPath $root -Directory | Sort-Object Name -Descending | Select-Object -First 1
    if (-not $latest) { throw "没有任何拉取记录：$root" }
    $RunDir = $latest.FullName
}
$originalDir = Join-Path $RunDir 'original'
$workDir = Join-Path $RunDir 'work'
$manifestPath = Join-Path $RunDir 'manifest.json'
foreach ($required in @($originalDir, $workDir, $manifestPath)) {
    if (-not (Test-Path $required)) { throw "目录结构不完整，缺少 $required" }
}
$manifest = [System.IO.File]::ReadAllText($manifestPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
$deviceRoot = $manifest.deviceRoot

Write-Host "回写目标  $deviceRoot" -ForegroundColor Cyan
Write-Host "工作副本  $workDir" -ForegroundColor DarkGray
Write-Host "拉取时间  $($manifest.pulledAt)" -ForegroundColor DarkGray
Write-Host ''

# ── 算改动清单：work 对 original 逐字节比对 ──
function Get-Tree {
    param([string]$Base)
    $map = @{}
    foreach ($file in Get-ChildItem -LiteralPath $Base -Recurse -File) {
        $rel = $file.FullName.Substring($Base.Length).TrimStart('\') -replace '\\', '/'
        $map[$rel] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
    }
    $map
}
$before = Get-Tree $originalDir
$after = Get-Tree $workDir

$added = @($after.Keys | Where-Object { -not $before.ContainsKey($_) } | Sort-Object)
$removed = @($before.Keys | Where-Object { -not $after.ContainsKey($_) } | Sort-Object)
$modified = @($after.Keys | Where-Object { $before.ContainsKey($_) -and $before[$_] -ne $after[$_] } | Sort-Object)

# 一删一增而内容完全相同 = 移动。配成 rename 少一半请求，也不会出现「删掉了还没写上」的中间态。
$renames = New-Object System.Collections.ArrayList
foreach ($new in @($added)) {
    $match = @($removed | Where-Object { $before[$_] -eq $after[$new] })
    if ($match.Count -eq 1) {
        [void]$renames.Add([pscustomobject]@{ From = $match[0]; To = $new })
    }
}
$renamedFrom = @($renames | ForEach-Object { $_.From })
$renamedTo = @($renames | ForEach-Object { $_.To })
$added = @($added | Where-Object { $renamedTo -notcontains $_ })
$removed = @($removed | Where-Object { $renamedFrom -notcontains $_ })

$writes = @(@($modified) + @($added) | Sort-Object)
if (-not $renames.Count -and -not $writes.Count -and -not $removed.Count) {
    Write-Host 'work 与 original 完全一致，没有需要回写的改动。' -ForegroundColor Green
    return
}

# ── 打印计划 ──
Write-Host "改动清单（$($renames.Count) 移动、$($writes.Count) 写入、$($removed.Count) 删除）：" -ForegroundColor Cyan
foreach ($rename in $renames) { Write-Host "  移动  $($rename.From)`n        → $($rename.To)" }
foreach ($rel in $writes) {
    $verb = if ($modified -contains $rel) { '覆盖' } else { '新建' }
    $size = (Get-Item -LiteralPath (Join-Path $workDir ($rel -replace '/', '\'))).Length
    Write-Host "  $verb  $rel（$size 字节）"
}
foreach ($rel in $removed) { Write-Host "  删除  $rel" -ForegroundColor Yellow }
Write-Host ''

# ── 连桥 ──
$bridgeScript = Join-Path $PSScriptRoot 'usb-bridge.ps1'
if (-not (Test-Path $bridgeScript)) { throw "找不到 usb-bridge.ps1：$bridgeScript" }
$bridgeArgs = @{ PassThru = $true; LocalPort = $LocalPort }
if ($Adb) { $bridgeArgs['Adb'] = $Adb }
# 6>$null：桥脚本的诊断走信息流，吞掉它，令牌就不会出现在终端历史里。
$bridge = & $bridgeScript @bridgeArgs 6>$null
if (-not $bridge -or -not $bridge.Token) {
    throw 'USB 桥未就绪。请先单独运行 usb-bridge.ps1 看具体是哪一步失败。'
}
$baseUrl = $bridge.BaseUrl
$headers = @{ Authorization = "Bearer $($bridge.Token)" }

function Invoke-Fs {
    param([string]$Endpoint, [hashtable]$Body)
    # 自己编码成 UTF-8 字节：PS 5.1 默认按 Latin-1 发正文，中文路径和正文都会被写坏。
    $json = $Body | ConvertTo-Json -Depth 4 -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    Invoke-WebRequest -Uri "$baseUrl/api/fs/$Endpoint" -Method Post -Headers $headers `
        -Body $bytes -ContentType 'application/json; charset=utf-8' -TimeoutSec 60 -UseBasicParsing | Out-Null
}
function Get-DeviceHash {
    param([string]$Relative)
    $uri = "$baseUrl/api/fs/raw?path=$([uri]::EscapeDataString("$deviceRoot/$Relative"))"
    try {
        $response = Invoke-WebRequest -Uri $uri -Headers $headers -TimeoutSec 60 -UseBasicParsing
    } catch {
        return $null   # 设备上已经没有这个文件
    }
    $stream = New-Object System.IO.MemoryStream((, $response.RawContentStream.ToArray()))
    (Get-FileHash -InputStream $stream -Algorithm SHA256).Hash
}

# ── 闸门 1：即将被碰的文件，设备现状必须与 manifest 一致 ──
$manifestHash = @{}
foreach ($record in @($manifest.files)) { $manifestHash[$record.path] = $record.sha256 }

$touched = @(@($renames | ForEach-Object { $_.From }) + @($writes) + @($removed) | Sort-Object -Unique)
$drift = New-Object System.Collections.ArrayList
Write-Host "校验设备现状（$($touched.Count) 个将被改动的文件）…" -ForegroundColor DarkGray
foreach ($rel in $touched) {
    $expected = if ($manifestHash.ContainsKey($rel)) { $manifestHash[$rel] } else { $null }
    $actual = Get-DeviceHash -Relative $rel
    if ($expected -ne $actual) {
        [void]$drift.Add("$rel（拉取时 $(if ($expected) { $expected.Substring(0, 12) } else { '不存在' })，现在 $(if ($actual) { $actual.Substring(0, 12) } else { '不存在' })）")
    }
}

# ── 闸门 2：整树清单必须与 manifest 一致 ──
function Get-DeviceInventory {
    $inventory = @{}
    $queue = New-Object System.Collections.Queue
    $queue.Enqueue([pscustomobject]@{ Device = $deviceRoot; Relative = '' })
    while ($queue.Count -gt 0) {
        $node = $queue.Dequeue()
        $uri = "$baseUrl/api/fs/list?path=$([uri]::EscapeDataString($node.Device))"
        $response = Invoke-WebRequest -Uri $uri -Headers $headers -TimeoutSec 30 -UseBasicParsing
        $listing = [System.Text.Encoding]::UTF8.GetString($response.RawContentStream.ToArray()) | ConvertFrom-Json
        foreach ($entry in @($listing.entries)) {
            $childRelative = if ($node.Relative) { "$($node.Relative)/$($entry.name)" } else { $entry.name }
            if ($entry.is_dir) {
                $queue.Enqueue([pscustomobject]@{ Device = "$($node.Device)/$($entry.name)"; Relative = $childRelative })
            } else {
                $inventory[$childRelative] = [long]$entry.size
            }
        }
    }
    $inventory
}
Write-Host '校验整树清单…' -ForegroundColor DarkGray
$inventory = Get-DeviceInventory
$manifestSize = @{}
foreach ($record in @($manifest.files)) { $manifestSize[$record.path] = [long]$record.size }
foreach ($rel in @($inventory.Keys | Sort-Object)) {
    if (-not $manifestSize.ContainsKey($rel)) { [void]$drift.Add("$rel（拉取之后新出现的文件）") }
    elseif ($manifestSize[$rel] -ne $inventory[$rel]) { [void]$drift.Add("$rel（大小 $($manifestSize[$rel]) → $($inventory[$rel])）") }
}
foreach ($rel in @($manifestSize.Keys | Sort-Object)) {
    if (-not $inventory.ContainsKey($rel)) { [void]$drift.Add("$rel（拉取之后在设备上消失）") }
}

if ($drift.Count -gt 0) {
    Write-Host ''
    Write-Host "✗ 设备状态与拉取时不一致，共 $($drift.Count) 处，已中止：" -ForegroundColor Red
    foreach ($item in $drift) { Write-Host "    · $item" -ForegroundColor Red }
    Write-Host ''
    Write-Host '这通常意味着拉取之后手机上又推进了剧情。此时回写会把新回合冲掉。' -ForegroundColor Yellow
    Write-Host '正确做法：重新 pull-story-project.ps1，在新副本上重跑标准化，再 push。' -ForegroundColor Yellow
    throw '设备状态漂移，拒绝回写。'
}
Write-Host '✓ 设备状态与拉取时逐字节一致' -ForegroundColor Green
Write-Host ''

if (-not $Apply) {
    Write-Host '以上均未执行。确认无误后加 -Apply 重跑。' -ForegroundColor Cyan
    return
}

# ── 执行 ──
# 先移动再写入：写入清单里可能有刚被移走的那个路径（同一目录改名 + 另建同名文件），
# 顺序反了会先被 rename 的「目标已存在」拦住。
foreach ($rename in $renames) {
    # fs_rename 用的是裸 std::fs::rename，不建父目录；fs_mkdir 是 create_dir_all，重复调用无害。
    $parent = Split-Path -Parent ($rename.To -replace '/', '\')
    if ($parent) { Invoke-Fs -Endpoint 'mkdir' -Body @{ path = "$deviceRoot/$($parent -replace '\\', '/')" } }
    Invoke-Fs -Endpoint 'rename' -Body @{ from = "$deviceRoot/$($rename.From)"; to = "$deviceRoot/$($rename.To)" }
    Write-Host "  ✓ 移动  $($rename.To)" -ForegroundColor Green
}
foreach ($rel in $writes) {
    $localPath = Join-Path $workDir ($rel -replace '/', '\')
    # 严格 UTF-8 解码：非法字节直接抛，不静默换成替代字符；顺带挡掉 BOM。
    $bytes = [System.IO.File]::ReadAllBytes($localPath)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        throw "$rel 带 UTF-8 BOM，设备侧的严格 JSON / frontmatter 解析会被它噎住，请先去掉。"
    }
    $strict = New-Object System.Text.UTF8Encoding($false, $true)
    $content = $strict.GetString($bytes)
    if ($strict.GetByteCount($content) -ne $bytes.Length) { throw "$rel 不是纯 UTF-8 文本，不能走 fs/write 回写。" }
    $parent = Split-Path -Parent ($rel -replace '/', '\')
    if ($parent) { Invoke-Fs -Endpoint 'mkdir' -Body @{ path = "$deviceRoot/$($parent -replace '\\', '/')" } }
    Invoke-Fs -Endpoint 'write' -Body @{ path = "$deviceRoot/$rel"; content = $content }
    Write-Host "  ✓ 写入  $rel" -ForegroundColor Green
}
foreach ($rel in $removed) {
    Invoke-Fs -Endpoint 'delete' -Body @{ path = "$deviceRoot/$rel" }
    Write-Host "  ✓ 删除  $rel" -ForegroundColor Green
}

# ── 回写后逐字节复核 ──
Write-Host ''
Write-Host '复核设备内容…' -ForegroundColor DarkGray
$bad = New-Object System.Collections.ArrayList
foreach ($rel in @(@($renames | ForEach-Object { $_.To }) + @($writes) | Sort-Object -Unique)) {
    $actual = Get-DeviceHash -Relative $rel
    if ($actual -ne $after[$rel]) { [void]$bad.Add($rel) }
}
foreach ($rel in @($removed) + @($renames | ForEach-Object { $_.From })) {
    if ($null -ne (Get-DeviceHash -Relative $rel)) { [void]$bad.Add("$rel 本该已不存在") }
}
if ($bad.Count -gt 0) {
    Write-Host "✗ 有 $($bad.Count) 个文件回写后与工作副本不一致：" -ForegroundColor Red
    foreach ($item in $bad) { Write-Host "    · $item" -ForegroundColor Red }
    throw '回写结果校验失败。'
}

Write-Host ''
Write-Host "✓ 回写完成并逐字节复核通过（$($renames.Count) 移动、$($writes.Count) 写入、$($removed.Count) 删除）" -ForegroundColor Green
Write-Host "  原样备份仍在 $originalDir，需要回滚就从它推回去。" -ForegroundColor DarkGray
Write-Host '  应用若正开着这个项目，请重新进入一次项目让它重新加载索引。' -ForegroundColor Cyan
