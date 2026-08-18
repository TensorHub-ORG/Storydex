param(
    [string]$CandidateRoot = "",
    [int]$StartupTimeoutSeconds = 30,
    [int]$ShutdownTimeoutSeconds = 15
)

$ErrorActionPreference = "Stop"

function Test-IsPathInside {
    param(
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Root
    )

    $candidatePath = [System.IO.Path]::GetFullPath($Candidate).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
    $rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
    if ($candidatePath.Equals($rootPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    return $candidatePath.StartsWith(
        $rootPath + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Wait-ForProcessExit {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        if ($null -eq (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
            return $true
        }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

$previewRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$desktopRoot = (Resolve-Path (Join-Path $previewRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($CandidateRoot)) {
    $CandidateRoot = Join-Path $desktopRoot "candidate\staging"
}
$candidatePath = [System.IO.Path]::GetFullPath($CandidateRoot)
$previewExecutable = Join-Path $candidatePath "storydex-tauri-preview.exe"
$sidecarExecutable = Join-Path $candidatePath "storydex-agentd.exe"
foreach ($requiredFile in @($previewExecutable, $sidecarExecutable)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Tauri preview smoke requires packaged candidate file: $requiredFile"
    }
}

$systemTemporaryRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$smokeRoot = Join-Path $systemTemporaryRoot ("storydex-tauri-smoke-" + [Guid]::NewGuid().ToString("N"))
if (-not (Test-IsPathInside -Candidate $smokeRoot -Root $systemTemporaryRoot)) {
    throw "Smoke root escaped the operating-system temporary directory: $smokeRoot"
}

$roamingRoot = Join-Path $smokeRoot "app-data\roaming"
$localRoot = Join-Path $smokeRoot "app-data\local"
$fixtureRoot = Join-Path $smokeRoot "workspace-fixture"
New-Item -ItemType Directory -Path $roamingRoot, $localRoot, $fixtureRoot -Force | Out-Null

foreach ($variable in @(
    "CONDA_PREFIX",
    "PYTHONHOME",
    "PYTHONPATH",
    "STORYDEX_ALLOW_SYSTEM_PYTHON_FALLBACK",
    "STORYDEX_AGENT_PROVIDER_REPLAY_FIXTURE",
    "STORYDEX_COOMI_BRIDGE",
    "STORYDEX_EMBED_PYTHON",
    "STORYDEX_GLOBAL_ROOT",
    "STORYDEX_PYTHON",
    "STORYDEX_TAURI_SIDECAR_PATH",
    "STORYDEX_WORKSPACE_ROOT",
    "VIRTUAL_ENV"
)) {
    Remove-Item -LiteralPath "Env:$variable" -ErrorAction SilentlyContinue
}
$env:APPDATA = $roamingRoot
$env:LOCALAPPDATA = $localRoot
$env:STORYDEX_AGENTD_REFACTOR_ROOT = $fixtureRoot
$env:STORYDEX_DISABLE_NETWORK = "1"
$env:STORYDEX_TAURI_TEST_ROOT = $smokeRoot
$env:STORYDEX_TESTING = "1"

$previewProcess = $null
$agentdProcessId = 0
$logPath = ""
$succeeded = $false
try {
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $previewExecutable
    $startInfo.WorkingDirectory = $candidatePath
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $previewProcess = [System.Diagnostics.Process]::Start($startInfo)
    if ($null -eq $previewProcess) {
        throw "Tauri preview process did not start."
    }

    $startupDeadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    $readyPort = 0
    do {
        $previewProcess.Refresh()
        if ($previewProcess.HasExited) {
            $previewStdout = $previewProcess.StandardOutput.ReadToEnd()
            $previewStderr = $previewProcess.StandardError.ReadToEnd()
            $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
            [System.IO.File]::WriteAllText((Join-Path $smokeRoot "preview.stdout.log"), $previewStdout, $utf8WithoutBom)
            [System.IO.File]::WriteAllText((Join-Path $smokeRoot "preview.stderr.log"), $previewStderr, $utf8WithoutBom)
            $stderrDetail = if ([string]::IsNullOrWhiteSpace($previewStderr)) { "no stderr" } else { $previewStderr.Trim() }
            throw "Tauri preview exited before sidecar readiness with code $($previewProcess.ExitCode): $stderrDetail"
        }
        $latestLog = Get-ChildItem -LiteralPath $smokeRoot -Recurse -File -Filter "agentd-*.log" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTimeUtc -Descending |
            Select-Object -First 1
        if ($null -ne $latestLog) {
            $logPath = $latestLog.FullName
            $logContent = Get-Content -Raw -LiteralPath $logPath
            $readyMatch = [regex]::Match($logContent, "\[lifecycle\] ready pid=(\d+) port=(\d+)")
            if ($readyMatch.Success) {
                $agentdProcessId = [int]$readyMatch.Groups[1].Value
                $readyPort = [int]$readyMatch.Groups[2].Value
                break
            }
        }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $startupDeadline)

    if ($readyPort -lt 1) {
        throw "Timed out waiting for the packaged Tauri preview sidecar ready log."
    }

    $health = $null
    $lastHealthError = "health endpoint was not reached"
    for ($attempt = 0; $attempt -lt 20; $attempt += 1) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$readyPort/api/v1/sys/health" -TimeoutSec 2
            if ($health.ok -eq $true -and $health.data.runtime -eq "storydex-agentd") {
                break
            }
            $lastHealthError = "health response did not identify storydex-agentd"
        }
        catch {
            $lastHealthError = $_.Exception.Message
        }
        $health = $null
        Start-Sleep -Milliseconds 150
    }
    if ($null -eq $health) {
        throw "Packaged Tauri preview health probe failed: $lastHealthError"
    }

    $logContent = Get-Content -Raw -LiteralPath $logPath
    if ($logContent -match '(?i)"token"\s*:' -or $logContent -match '(?i)(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])') {
        throw "Packaged Tauri preview log exposed a runtime-token-shaped secret: $logPath"
    }

    $closeDeadline = [DateTime]::UtcNow.AddSeconds(10)
    $closeRequested = $false
    do {
        $previewProcess.Refresh()
        if ($previewProcess.HasExited) {
            $closeRequested = $true
            break
        }
        if ($previewProcess.MainWindowHandle -ne [IntPtr]::Zero) {
            $closeRequested = $previewProcess.CloseMainWindow()
            if ($closeRequested) {
                break
            }
        }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $closeDeadline)
    if (-not $closeRequested) {
        throw "Packaged Tauri preview main window could not be closed gracefully."
    }

    if (-not $previewProcess.WaitForExit($ShutdownTimeoutSeconds * 1000)) {
        throw "Packaged Tauri preview did not exit within $ShutdownTimeoutSeconds seconds."
    }
    if ($agentdProcessId -gt 0 -and -not (Wait-ForProcessExit -ProcessId $agentdProcessId -TimeoutSeconds $ShutdownTimeoutSeconds)) {
        throw "Packaged storydex-agentd process $agentdProcessId survived the Tauri preview exit."
    }

    $finalLogContent = Get-Content -Raw -LiteralPath $logPath
    if ($finalLogContent -notmatch "\[lifecycle\] graceful shutdown requested" -or
        $finalLogContent -notmatch "\[lifecycle\] sidecar stopped cleanly") {
        throw "Packaged Tauri preview did not record a clean authenticated sidecar shutdown: $logPath"
    }

    $succeeded = $true
    [pscustomobject]@{
        ok = $true
        previewPid = $previewProcess.Id
        agentdPid = $agentdProcessId
        port = $readyPort
        healthRuntime = $health.data.runtime
        logTokenRedacted = $true
        gracefulExit = $true
    } | ConvertTo-Json -Depth 3
}
finally {
    if ($null -ne $previewProcess) {
        $previewProcess.Refresh()
        if (-not $previewProcess.HasExited) {
            Stop-Process -Id $previewProcess.Id -Force -ErrorAction SilentlyContinue
            $null = $previewProcess.WaitForExit(3000)
        }
    }
    if ($agentdProcessId -gt 0) {
        Stop-Process -Id $agentdProcessId -Force -ErrorAction SilentlyContinue
    }

    if ($succeeded) {
        $resolvedSmokeRoot = [System.IO.Path]::GetFullPath($smokeRoot)
        if (-not (Test-IsPathInside -Candidate $resolvedSmokeRoot -Root $systemTemporaryRoot)) {
            throw "Refusing to clean an unexpected smoke directory: $resolvedSmokeRoot"
        }
        Remove-Item -LiteralPath $resolvedSmokeRoot -Recurse -Force
    }
    else {
        Write-Warning "Tauri preview smoke artifacts were preserved for diagnosis: $smokeRoot"
        if (-not [string]::IsNullOrWhiteSpace($logPath)) {
            Write-Warning "Tauri preview sidecar log: $logPath"
        }
    }
}
