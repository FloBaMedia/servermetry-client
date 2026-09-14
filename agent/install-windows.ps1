<#
.SYNOPSIS
    ServerMetry Agent Installer for Windows
.DESCRIPTION
    Downloads the agent.py, creates the config, and registers a Scheduled Task
    to run the agent every minute.
    API URL and API Key can be passed as parameters or environment variables
    (SERVERMETRY_URL / SERVERMETRY_KEY, or legacy SERVERPULSE_*) to run non-interactively.
.EXAMPLE
    # Interactive
    powershell -ExecutionPolicy Bypass -File install-windows.ps1

    # Non-interactive (e.g. from a setup command)
    powershell -ExecutionPolicy Bypass -File install-windows.ps1 -ApiUrl "https://api.example.com" -ApiKey "sp_live_..."

    # Via environment variables
    $env:SERVERMETRY_URL="https://api.example.com"; $env:SERVERMETRY_KEY="sp_live_..."; powershell -ExecutionPolicy Bypass -File install-windows.ps1
#>
param(
    [string]$ApiUrl = $(if ($env:SERVERMETRY_URL) { $env:SERVERMETRY_URL } else { $env:SERVERPULSE_URL }),
    [string]$ApiKey = $(if ($env:SERVERMETRY_KEY) { $env:SERVERMETRY_KEY } else { $env:SERVERPULSE_KEY })
)

$DefaultApiUrl = "https://api.servermetry.com"

$ErrorActionPreference = "Stop"

$InstallDir  = "C:\ProgramData\ServerMetry"
$AgentPath   = "$InstallDir\agent.py"
$ConfPath    = "$InstallDir\agent.conf"
$LogPath     = "$InstallDir\agent.log"
$GithubBase  = "https://raw.githubusercontent.com/FloBaMedia/servermetry-client/main/agent"
$TaskName    = "ServerMetryAgent"

# All module files that must be present alongside agent.py
$ModuleFiles = @(
    "client/__init__.py",
    "client/api.py",
    "models/__init__.py",
    "models/constants.py",
    "models/limits.py",
    "models/paths.py",
    "services/__init__.py",
    "services/config_applier.py",
    "services/linux.py",
    "services/storage.py",
    "services/darwin.py",
    "services/windows.py",
    "services/updater.py",
    "services/path_migration.py",
    "utils/__init__.py",
    "utils/config.py",
    "utils/logging.py",
    "utils/validation.py",
    "utils/lock.py",
    "utils/snapshot.py"
)

function Write-Info  { param($msg) Write-Host "[INFO]  $msg" -ForegroundColor Green }
function Write-Warn  { param($msg) Write-Host "[WARN]  $msg" -ForegroundColor Yellow }
function Write-Err   { param($msg) Write-Host "[ERROR] $msg" -ForegroundColor Red }

# Pause before exiting so the user can read the output, but only when running
# in a window that was opened just for this script (i.e. not in an existing terminal).
function Exit-Script {
    param([int]$Code = 0)
    # $IsNewWindow is true when the process was launched without an existing console
    # (double-click, Run dialog, Scheduled Task, etc.)
    $IsNewWindow = ($Host.Name -eq 'ConsoleHost') -and
                   ([System.Diagnostics.Process]::GetCurrentProcess().MainWindowHandle -ne [IntPtr]::Zero) -and
                   ($null -eq $env:WT_SESSION) -and           # not Windows Terminal
                   ($null -eq $env:TERM_PROGRAM)              # not VS Code / other terminals
    if ($IsNewWindow -or $Code -ne 0) {
        Write-Host ""
        Write-Host "Press Enter to close this window..." -ForegroundColor DarkGray
        $null = Read-Host
    }
    exit $Code
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Test-PythonUsableBySystem {
    param([Parameter(Mandatory = $true)][string]$PythonPath)
    if (-not $PythonPath) { return $false }
    # Prefer installs that SYSTEM can also execute (machine-wide).
    if ($PythonPath -match '(?i)\\WindowsApps\\') { return $false }
    if ($PythonPath -match '(?i)\\AppData\\(Local|Roaming)\\') { return $false }
    if ($PythonPath -match '(?i)\\Users\\[^\\]+\\') { return $false }
    return $true
}

Write-Host ""
Write-Host "ServerMetry Agent Installer for Windows" -ForegroundColor Cyan
Write-Host "─────────────────────────────────────────────"

# ── 0. Admin check ────────────────────────────────────────────────────────────
$currentPrincipal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Err "This installer must be run as Administrator."
    Write-Host ""
    Write-Host "Please right-click PowerShell and choose 'Run as Administrator'," -ForegroundColor White
    Write-Host "then re-run this script." -ForegroundColor White
    Exit-Script 1
}

# ── 1. Find Python ────────────────────────────────────────────────────────────
Write-Info "Looking for Python 3.6+..."

$PythonExe = $null
$PythonVer = $null
$candidates = @()

# Prefer well-known machine-wide locations (usable by SYSTEM scheduled task)
$machineRoots = @(
    "$env:ProgramFiles\Python*",
    "${env:ProgramFiles(x86)}\Python*",
    "$env:ProgramFiles\Python3*",
    "${env:ProgramFiles(x86)}\Python3*"
)
foreach ($pattern in $machineRoots) {
    Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue | ForEach-Object {
        $exe = Join-Path $_.FullName "python.exe"
        if (Test-Path $exe) { $candidates += $exe }
    }
}

foreach ($cmd in @("python", "python3", "py")) {
    try {
        $resolved = (Get-Command $cmd -ErrorAction SilentlyContinue).Source
        if ($resolved) { $candidates += $resolved }
    } catch { }
}

# Also try `py -3` launcher target
try {
    $pyTarget = & py -3 -c "import sys; print(sys.executable)" 2>$null
    if ($LASTEXITCODE -eq 0 -and $pyTarget) { $candidates += $pyTarget.Trim() }
} catch { }

$candidates = $candidates | Where-Object { $_ } | Select-Object -Unique
$fallbackUserPython = $null

foreach ($resolved in $candidates) {
    try {
        if ($resolved -match '(?i)\\WindowsApps\\') {
            Write-Warn "Skipping Microsoft Store Python stub at $resolved"
            continue
        }
        $ver = & $resolved -c "import sys; v=sys.version_info; print('{}.{}'.format(v.major,v.minor))" 2>$null
        $null = & $resolved -c "import sys; sys.exit(0 if sys.version_info>=(3,6) else 1)" 2>$null
        if ($LASTEXITCODE -ne 0) { continue }

        if (Test-PythonUsableBySystem $resolved) {
            $PythonExe = $resolved
            $PythonVer = $ver
            Write-Info "Found machine-wide Python $ver at $PythonExe"
            break
        }

        if (-not $fallbackUserPython) {
            $fallbackUserPython = @{ Path = $resolved; Ver = $ver }
        }
    } catch { }
}

if (-not $PythonExe -and $fallbackUserPython) {
    $PythonExe = $fallbackUserPython.Path
    $PythonVer = $fallbackUserPython.Ver
    Write-Warn "Using per-user Python $PythonVer at $PythonExe"
    Write-Warn "Scheduled Task runs as SYSTEM and may fail to execute a per-user Python."
    Write-Warn "Prefer installing Python for all users from https://www.python.org/downloads/"
}

if (-not $PythonExe) {
    Write-Err "Python 3.6+ not found."
    Write-Err "Download from https://www.python.org/downloads/ and re-run this installer."
    Write-Host ""
    Write-Host "Tip: When installing Python, tick 'Install for all users' and 'Add Python to PATH'." -ForegroundColor White
    Exit-Script 1
}

# ── 2. Create install directory ───────────────────────────────────────────────
Write-Info "Creating $InstallDir ..."
foreach ($sub in @("", "\client", "\models", "\services", "\utils")) {
    New-Item -ItemType Directory -Force -Path "$InstallDir$sub" | Out-Null
}

# ── 3. Download agent files ───────────────────────────────────────────────────
Write-Info "Downloading agent files ..."
try {
    Invoke-WebRequest -Uri "$GithubBase/agent.py" -OutFile $AgentPath -UseBasicParsing
    foreach ($mod in $ModuleFiles) {
        $dest = "$InstallDir\" + $mod.Replace("/", "\")
        Invoke-WebRequest -Uri "$GithubBase/$mod" -OutFile $dest -UseBasicParsing
    }
    Invoke-WebRequest -Uri "$GithubBase/uninstall-windows.ps1" -OutFile "$InstallDir\uninstall-windows.ps1" -UseBasicParsing
    Write-Info "Agent files downloaded to $InstallDir"
} catch {
    Write-Err "Download failed: $_"
    Write-Host ""
    Write-Host "Check your internet connection and try again." -ForegroundColor White
    Exit-Script 1
}

# ── 4. Config (params / env vars or interactive) ──────────────────────────────
# api_url is optional — agent defaults to https://api.servermetry.com unless
# -ApiUrl / SERVERMETRY_URL is set (override only).

$ApiUrl = $ApiUrl.TrimEnd("/")

if ($ApiKey) {
    Write-Info "Using API Key from parameters / environment variables."
} else {
    Write-Host ""
    Write-Host "Please enter your ServerMetry API Key:" -ForegroundColor White

    do {
        $ApiKeySecure = Read-Host "  API Key (sp_live_...)" -AsSecureString
        $ApiKeyBSTR   = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($ApiKeySecure)
        $ApiKey       = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($ApiKeyBSTR)
        [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ApiKeyBSTR)
        if ($ApiKey.Length -lt 8) {
            Write-Warn "API key seems too short. Please try again."
        }
    } while ($ApiKey.Length -lt 8)
}

if ($ApiUrl) {
    Write-Info "API URL override: $ApiUrl"
} else {
    Write-Info "Using default API URL: $DefaultApiUrl"
}

# ── 5. Write config (UTF-8 without BOM — required by Python configparser) ─────
$ConfLines = @(
    "[servermetry]",
    "api_key = $ApiKey"
)
if ($ApiUrl) {
    $ConfLines += "api_url = $ApiUrl"
}
$ConfContent = ($ConfLines -join "`n") + "`n"
Write-Utf8NoBom -Path $ConfPath -Content $ConfContent

# Restrict config: install user + SYSTEM + Administrators (task runs as SYSTEM)
# Use SIDs so this works on non-English Windows (BUILTIN\Administrators is localized).
try {
    $acl = Get-Acl $ConfPath
    $acl.SetAccessRuleProtection($true, $false)  # disable inheritance, remove inherited rules
    $sids = @(
        [System.Security.Principal.WindowsIdentity]::GetCurrent().User,
        (New-Object System.Security.Principal.SecurityIdentifier("S-1-5-18")),      # SYSTEM
        (New-Object System.Security.Principal.SecurityIdentifier("S-1-5-32-544"))   # Administrators
    )
    foreach ($sid in $sids) {
        if (-not $sid) { continue }
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $sid,
            "FullControl",
            "Allow"
        )
        $acl.AddAccessRule($rule) | Out-Null
    }
    Set-Acl -Path $ConfPath -AclObject $acl
    Write-Info "Config written to $ConfPath (restricted permissions for user/SYSTEM/Administrators)"
} catch {
    Write-Warn "Could not restrict config permissions: $_"
    Write-Info "Config written to $ConfPath"
}

# ── 5b. Connectivity check ────────────────────────────────────────────────────
$HealthBase = if ($ApiUrl) { $ApiUrl } else { $DefaultApiUrl }
Write-Info "Checking connectivity to $HealthBase ..."
try {
    $response = Invoke-WebRequest -Uri "$HealthBase/health" -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop
    Write-Info "API reachable (HTTP $($response.StatusCode))."
} catch {
    Write-Warn "Could not reach $HealthBase/health — $_"
    Write-Warn "The agent will be installed anyway but won't report until the API is reachable."
}

# ── 6. Register Scheduled Task ────────────────────────────────────────────────
Write-Info "Registering Scheduled Task '$TaskName' (runs every minute)..."

# Remove existing task if present
try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
} catch { }
# Also remove legacy ServerPulse task name if present
try {
    Unregister-ScheduledTask -TaskName "ServerPulseAgent" -Confirm:$false -ErrorAction SilentlyContinue
} catch { }

$action   = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "`"$AgentPath`" --config `"$ConfPath`""

# Trigger: repeat every 1 minute indefinitely
$trigger  = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 1) `
    -RepetitionDuration (New-TimeSpan -Days 9999)

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

$principal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel Highest

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "ServerMetry monitoring agent – sends system metrics every minute." `
        -Force | Out-Null
    Write-Info "Scheduled Task '$TaskName' created (runs as SYSTEM, every minute)."
} catch {
    Write-Warn "Could not register as SYSTEM. Trying current user..."
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -RunLevel Highest `
        -Description "ServerMetry monitoring agent – sends system metrics every minute." `
        -Force | Out-Null
    Write-Info "Scheduled Task '$TaskName' created (runs as current user, every minute)."
}

# ── 7. First test run (dry-run) ───────────────────────────────────────────────
Write-Host ""
Write-Info "Running first test (dry-run, no HTTP request)..."
Write-Host "─────────────────────────────────────────────"
try {
    & $PythonExe $AgentPath --dry-run --config $ConfPath
} catch {
    Write-Warn "Dry-run produced an error: $_"
}
Write-Host "─────────────────────────────────────────────"

Write-Host ""
Write-Info "Installation complete!"
Write-Info "The agent will run every minute via Scheduled Tasks."
Write-Info "Logs: $LogPath"
Write-Host ""
Write-Host "To run the agent manually now (sends real data):" -ForegroundColor White
Write-Host "  & `"$PythonExe`" `"$AgentPath`" --config `"$ConfPath`"" -ForegroundColor Gray
Write-Host ""
Write-Host "To view the Scheduled Task:" -ForegroundColor White
Write-Host "  Get-ScheduledTask -TaskName '$TaskName'" -ForegroundColor Gray
Write-Host ""

Exit-Script 0
