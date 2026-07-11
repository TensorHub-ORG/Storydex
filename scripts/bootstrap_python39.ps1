[CmdletBinding()]
param(
    [switch]$InstallRequirements,
    [switch]$Recreate
)

$ErrorActionPreference = "Stop"
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = (Resolve-Path (Join-Path $scriptRoot "..")).Path
$pythonRoot = Join-Path $projectRoot ".python39"
$pythonExe = Join-Path $pythonRoot "Scripts\python.exe"
$requirementsFile = Join-Path $projectRoot "requirements.txt"

function Write-Storydex([string]$Message) {
    Write-Host "[Storydex] $Message"
}

function Test-InternalPython {
    if (-not (Test-Path $pythonExe)) {
        return $false
    }

    try {
        & $pythonExe -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 9) else 1)" | Out-Null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Test-Python39Executable([string]$Executable, [string]$Label) {
    if (-not $Executable -or -not (Test-Path $Executable)) {
        return $null
    }

    try {
        $output = & $Executable -c "import sys; print(sys.executable); print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}'); raise SystemExit(0 if sys.version_info[:2] == (3, 9) else 1)" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $output -or $output.Count -lt 2) {
            return $null
        }

        return @{
            Label = $Label
            Executable = [string]$output[0]
            Version = [string]$output[1]
        }
    } catch {
        return $null
    }
}

function Get-CondaPython39Candidate {
    $preferredNames = @("pytorch2.2.2", "pytorch2", "pytorch")
    $envRoots = @()

    try {
        $condaJson = & conda env list --json 2>$null
        if ($LASTEXITCODE -eq 0 -and $condaJson) {
            $parsed = ($condaJson | ConvertFrom-Json)
            if ($parsed.envs) {
                $envRoots += @($parsed.envs)
            }
        }
    } catch {}

    $envRoots += @(
        "C:\Users\lenovo\anaconda3\envs\pytorch2.2.2",
        "C:\Users\lenovo\anaconda3\envs\pytorch2",
        "C:\Users\lenovo\anaconda3\envs\pytorch",
        "D:\anaconda\envs\pytorch2.2.2",
        "D:\anaconda\envs\pytorch2",
        "D:\anaconda\envs\pytorch"
    )

    $uniqueRoots = @()
    foreach ($root in $envRoots) {
        $text = [string]$root
        if ($text -and $uniqueRoots -notcontains $text) {
            $uniqueRoots += $text
        }
    }

    foreach ($name in $preferredNames) {
        foreach ($root in $uniqueRoots) {
            if ((Split-Path -Leaf $root) -ne $name) {
                continue
            }
            $candidate = Test-Python39Executable (Join-Path $root "python.exe") "conda env $name"
            if ($null -ne $candidate) {
                return $candidate
            }
        }
    }

    foreach ($root in $uniqueRoots) {
        $candidate = Test-Python39Executable (Join-Path $root "python.exe") "conda env $(Split-Path -Leaf $root)"
        if ($null -ne $candidate) {
            return $candidate
        }
    }

    return $null
}

function Get-PythonCandidate {
    $configured = Test-Python39Executable $env:STORYDEX_PYTHON_SOURCE "STORYDEX_PYTHON_SOURCE"
    if ($null -ne $configured) {
        return $configured
    }

    $condaCandidate = Get-CondaPython39Candidate
    if ($null -ne $condaCandidate) {
        return $condaCandidate
    }

    $candidates = @(
        @{ Label = "py -3.9"; Command = "py"; Args = @("-3.9") },
        @{ Label = "python"; Command = "python"; Args = @() }
    )

    foreach ($candidate in $candidates) {
        $commandInfo = Get-Command $candidate.Command -ErrorAction SilentlyContinue
        if (-not $commandInfo) {
            continue
        }

        $args = @($candidate.Args) + @(
            "-c",
            "import sys; print(sys.executable); print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}'); raise SystemExit(0 if sys.version_info[:2] == (3, 9) else 1)"
        )

        try {
            $output = & $candidate.Command @args 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $output -or $output.Count -lt 2) {
                continue
            }

            $executable = [string]$output[0]
            $version = [string]$output[1]
            if (-not (Test-Path $executable)) {
                continue
            }

            return @{
                Label = $candidate.Label
                Executable = $executable
                Version = $version
            }
        } catch {
            continue
        }
    }

    return $null
}

function New-InternalPython {
    $candidate = Get-PythonCandidate
    if ($null -eq $candidate) {
        throw "No Python 3.9 runtime found. Install Python 3.9, activate a Conda Python 3.9 env, or set STORYDEX_PYTHON_SOURCE to a Python 3.9 python.exe."
    }

    Write-Storydex "Rebuilding project-local Python runtime from $($candidate.Label): $($candidate.Executable) ($($candidate.Version))"

    if (Test-Path $pythonRoot) {
        Remove-Item -Recurse -Force $pythonRoot
    }

    $env:PYTHONNOUSERSITE = "1"
    & $candidate.Executable -m venv $pythonRoot --clear
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $pythonExe)) {
        throw "Failed to create project-local Python runtime at $pythonRoot"
    }

    & $pythonExe -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) {
        throw "ensurepip failed in project-local Python runtime."
    }

    & $pythonExe -m pip install --upgrade pip setuptools wheel
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to upgrade pip tooling in project-local Python runtime."
    }
}

function Test-RequirementsInstalled {
    if (-not (Test-InternalPython)) {
        return $false
    }

    $env:PYTHONNOUSERSITE = "1"
    & $pythonExe -m pip check --disable-pip-version-check | Out-Null
    if ($LASTEXITCODE -ne 0) {
        return $false
    }

    & $pythonExe -c "from importlib.metadata import version; import anthropic, fastapi, pydantic, pydantic_core, pydantic_settings, sqlalchemy, uvicorn; raise SystemExit(0 if version('coomi-agent') == '1.1.2' else 1)"
    return $LASTEXITCODE -eq 0
}

function Install-RequirementsWithRetry {
    param(
        [string]$Path
    )

    $env:PYTHONNOUSERSITE = "1"
    $attempts = 3
    for ($attempt = 1; $attempt -le $attempts; $attempt++) {
        Write-Storydex "Installing locked Python dependencies into $pythonRoot (attempt $attempt/$attempts)"
        & $pythonExe -m pip install --disable-pip-version-check --only-binary=:all: --retries 8 --timeout 30 -r $Path
        if ($LASTEXITCODE -eq 0) {
            return
        }

        if ($attempt -lt $attempts) {
            Write-Storydex "Dependency install failed; retrying after a short delay..."
            Start-Sleep -Seconds (2 * $attempt)
        }
    }

    throw "Failed to install Python dependencies from $Path"
}

if ($Recreate -or -not (Test-InternalPython)) {
    New-InternalPython
}

if ($InstallRequirements) {
    if (-not (Test-Path $requirementsFile)) {
        throw "requirements.txt was not found: $requirementsFile"
    }

    if (Test-RequirementsInstalled) {
        Write-Storydex "Python dependencies already satisfy startup requirements."
    } else {
        Install-RequirementsWithRetry -Path $requirementsFile
    }
}

$env:PYTHONNOUSERSITE = "1"
$runtimeSummary = & $pythonExe -c "import sys, site; print(sys.version); print(sys.executable); print(f'ENABLE_USER_SITE={site.ENABLE_USER_SITE}')"
$runtimeSummary | ForEach-Object { Write-Storydex $_ }
