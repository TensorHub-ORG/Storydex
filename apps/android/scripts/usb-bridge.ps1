<#
.SYNOPSIS
    打通 PC → Android 应用私有目录的 USB 通道。

.DESCRIPTION
    Storydex 的故事项目存在应用私有目录（/data/user/0/com.storydex.android/files/stories），
    release 包对 adb 完全不可达：非 debuggable 所以 run-as 失败，allowBackup="false" 堵掉
    adb backup，四个 provider 或未导出或需要签名级权限。

    但引擎自身已经提供了完备的文件读写 API（/api/fs/*）并绑定在 127.0.0.1，正是 adb forward
    需要的形状。本脚本读取手机上由「USB 调试桥」开关发布的 bridge.json 取得端口与访问令牌，
    建立端口转发，并自检通路。

    前置条件：
      1. 手机已通过 USB 连接并授权 adb 调试；
      2. Storydex 引擎正在运行；
      3. 已在「Storydex 控制台 → 工具 → USB 调试桥」中开启该开关。

.PARAMETER LocalPort
    PC 侧本地端口，默认 18765。

.PARAMETER Adb
    adb 可执行文件路径。默认按 ANDROID_HOME / 常见 SDK 位置 / PATH 依次查找。

.PARAMETER Off
    撤销端口转发并退出（不改动手机上的开关）。

.PARAMETER PassThru
    把 base URL 与令牌作为对象返回给调用方。默认不返回，避免令牌被打印到终端历史里。

.EXAMPLE
    .\usb-bridge.ps1
    建立转发并自检；只输出诊断信息，不输出令牌。

.EXAMPLE
    $bridge = .\usb-bridge.ps1 -PassThru
    建立转发，并把令牌捕获到变量里供后续调用复用。

.EXAMPLE
    .\usb-bridge.ps1 -Off
    撤销本脚本建立的端口转发。
#>
[CmdletBinding()]
param(
    [int]$LocalPort = 18765,
    [string]$Adb,
    [switch]$Off,
    [switch]$PassThru
)

$ErrorActionPreference = 'Stop'

function Resolve-Adb {
    param([string]$Explicit)
    if ($Explicit) {
        if (-not (Test-Path $Explicit)) { throw "指定的 adb 不存在：$Explicit" }
        return (Resolve-Path $Explicit).Path
    }
    $candidates = @()
    foreach ($root in @($env:ANDROID_HOME, $env:ANDROID_SDK_ROOT, "$env:LOCALAPPDATA\Android\Sdk")) {
        if ($root) { $candidates += (Join-Path $root 'platform-tools\adb.exe') }
    }
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { return (Resolve-Path $candidate).Path }
    }
    $onPath = Get-Command adb -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    throw '找不到 adb。请用 -Adb 显式指定，或设置 ANDROID_HOME。'
}

<#
    撤销端口转发。
    不能直接 `forward --remove` 后丢弃 stderr：Windows PowerShell 5.1 会把原生命令的
    stderr 包装成 ErrorRecord，配合 $ErrorActionPreference='Stop' 就变成终止性错误——
    而「监听器本来就不存在」是正常情况。所以先 --list 判断存在再删。
#>
function Remove-Forward {
    param([string]$AdbPath, [int]$Port)
    $existing = @(& $AdbPath forward --list | Where-Object { $_ -match "\stcp:$Port\s" })
    if ($existing.Count -eq 0) { return $false }
    & $AdbPath forward --remove "tcp:$Port" | Out-Null
    return $true
}

$adbPath = Resolve-Adb -Explicit $Adb

if ($Off) {
    if (Remove-Forward -AdbPath $adbPath -Port $LocalPort) {
        Write-Host "已撤销 tcp:$LocalPort 的端口转发。" -ForegroundColor Yellow
    } else {
        Write-Host "tcp:$LocalPort 上没有转发，无需撤销。" -ForegroundColor DarkGray
    }
    exit 0
}

# ── 1. 设备就位检查 ──
$devices = @(& $adbPath devices | Select-Object -Skip 1 | Where-Object { $_ -match '\sdevice$' })
if ($devices.Count -eq 0) {
    throw '没有已授权的设备。请确认 USB 已连接、已在手机上允许调试。'
}
if ($devices.Count -gt 1) {
    Write-Warning "检测到 $($devices.Count) 台设备，将使用 adb 默认目标。必要时设置 `$env:ANDROID_SERIAL。"
}

# ── 2. 读取桥文件 ──
$bridgePath = '/sdcard/Storydex/bridge.json'
$raw = (& $adbPath shell "cat $bridgePath 2>/dev/null") -join "`n"
if (-not $raw.Trim()) {
    throw @"
读不到 $bridgePath。请依次确认：
  1. Storydex 引擎正在运行（控制台状态显示「运行中」）；
  2. 已在「Storydex 控制台 → 工具 → USB 调试桥」开启开关。
开关默认关闭，因为它会把引擎访问令牌写入共享存储。
"@
}

try { $bridge = $raw | ConvertFrom-Json } catch { throw "bridge.json 解析失败：$raw" }
if (-not $bridge.port -or -not $bridge.token) { throw "bridge.json 缺少 port 或 token：$raw" }

$devicePort = [int]$bridge.port
Write-Host "桥文件已读取：设备端口 $devicePort，更新于 $($bridge.updatedAt)" -ForegroundColor DarkGray

# ── 3. 建立端口转发 ──
Remove-Forward -AdbPath $adbPath -Port $LocalPort | Out-Null
& $adbPath forward "tcp:$LocalPort" "tcp:$devicePort" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "adb forward 失败（tcp:$LocalPort → tcp:$devicePort）。" }

$baseUrl = "http://127.0.0.1:$LocalPort"
$headers = @{ Authorization = "Bearer $($bridge.token)" }

# ── 4. 自检：健康端点（免鉴权）──
try {
    $health = Invoke-RestMethod -Uri "$baseUrl/api/runtime/health" -TimeoutSec 5
    Write-Host "✓ 引擎健康检查通过" -ForegroundColor Green
} catch {
    throw @"
端口已转发但健康检查失败：$($_.Exception.Message)
bridge.json 可能是上一次运行残留的（应用被强杀时不会清理）。请重启引擎后重试。
"@
}

# ── 5. 自检：带令牌列出 stories 目录 ──
$storiesDir = if ($bridge.storiesDir) { $bridge.storiesDir } else { "$($bridge.cwd)/stories" }
try {
    # 不用 Invoke-RestMethod：引擎的 JSON 响应头没带 charset，PS 5.1 会按 Latin-1 解码，
    # 中文故事名会显示成乱码。这里自己按 UTF-8 解字节。
    $listResponse = Invoke-WebRequest -Uri "$baseUrl/api/fs/list?path=$([uri]::EscapeDataString($storiesDir))" `
        -Headers $headers -TimeoutSec 10 -UseBasicParsing
    $list = [System.Text.Encoding]::UTF8.GetString($listResponse.RawContentStream.ToArray()) | ConvertFrom-Json
} catch {
    throw "令牌鉴权或目录列举失败：$($_.Exception.Message)"
}

$entries = @($list.entries)
Write-Host "✓ 令牌有效，stories 下有 $($entries.Count) 个条目" -ForegroundColor Green
foreach ($entry in $entries) {
    $mark = if ($entry.is_dir) { '📁' } else { '  ' }
    Write-Host "    $mark $($entry.name)" -ForegroundColor DarkGray
}

# ── 6. 输出给调用方（Agent / 手工 curl）复用 ──
Write-Host ''
Write-Host 'USB 通道已就绪：' -ForegroundColor Cyan
Write-Host "  Base URL     $baseUrl"
Write-Host "  Stories 目录  $storiesDir"
Write-Host "  沙箱写入根    $($bridge.cwd)   （/api/fs/write 等写接口限制在此目录内）"
Write-Host ''
Write-Host '示例（读取一个文件，先取令牌）：' -ForegroundColor Cyan
Write-Host '  $b = .\usb-bridge.ps1 -PassThru' -ForegroundColor Cyan
Write-Host "  curl -H `"Authorization: Bearer `$(`$b.Token)`" `"`$(`$b.BaseUrl)/api/fs/raw?path=`$(`$b.StoriesDir)/...`""
Write-Host ''
Write-Host '用完请在手机上关闭「USB 调试桥」开关；关闭并重启引擎后旧令牌立即失效。' -ForegroundColor Yellow

# 令牌只在显式请求时通过返回值传出，避免直接运行脚本时打印到终端历史里。
if ($PassThru) {
    [pscustomobject]@{
        BaseUrl    = $baseUrl
        Token      = $bridge.token
        Cwd        = $bridge.cwd
        StoriesDir = $storiesDir
        LocalPort  = $LocalPort
        DevicePort = $devicePort
    }
}
