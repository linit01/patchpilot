<#
.SYNOPSIS
    Installs PatchPilot on Windows via Docker (Desktop or WSL2 Engine).

.DESCRIPTION
    Bootstrap script for deploying PatchPilot on a fresh Windows 11 host.
    Handles the entire stack from zero:

    1. Detects or installs Python 3 (via winget)
    2. Detects Docker runtime (Docker Desktop or WSL2 Docker Engine)
       -- installs Docker Desktop via winget if neither is found
    3. Waits for Docker daemon to be ready
    4. Generates .env from .env.example (Fernet key, install dir)
    5. Creates the ansible/ directory with default inventory
    6. Starts PatchPilot via Docker Compose
    7. Optionally launches the Web Installer UI for guided setup

    Run from the PatchPilot repository root as Administrator.

.PARAMETER SkipDockerInstall
    Skip Docker installation -- use if Docker is already installed and you
    want to avoid the winget check.

.PARAMETER SkipPython
    Skip Python installation -- use if Python 3 is already on PATH.

.PARAMETER WebInstaller
    Launch the web installer UI instead of running Docker Compose directly.
    The web installer provides a browser-based setup wizard.

.PARAMETER Unattended
    Run without interactive prompts. Requires Docker and Python to be
    pre-installed, or will install them silently via winget.

.PARAMETER Port
    HTTP port for PatchPilot. Default: 8080.

.EXAMPLE
    .\Install-PatchPilot.ps1

.EXAMPLE
    .\Install-PatchPilot.ps1 -WebInstaller

.EXAMPLE
    .\Install-PatchPilot.ps1 -Unattended -SkipPython

.NOTES
    Requires: Windows 10 (build 1809+) or Windows 11
    Must run as: Administrator (for Docker Desktop install and firewall)
    Project:    PatchPilot -- https://github.com/linit01/patchpilot
#>

[CmdletBinding()]
param(
    [switch]$SkipDockerInstall,
    [switch]$SkipPython,
    [switch]$WebInstaller,
    [switch]$Unattended,
    [int]$Port = 8080
)

# -- Strict mode & constants ---------------------------------------------------
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Resolve repo root: scripts/windows/Install-PatchPilot.ps1 -> repo root (3 levels up)
# If that doesn't look right (e.g., called from a different location), fall back to CWD.
$REPO_ROOT = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSCommandPath))
if (-not (Test-Path (Join-Path $REPO_ROOT "docker-compose.yml"))) {
    # Fallback: maybe we're being run from the repo root directly
    if (Test-Path (Join-Path $PWD "docker-compose.yml")) {
        $REPO_ROOT = $PWD.Path
    }
}

$VERSION_FILE = Join-Path $REPO_ROOT "VERSION"
$ENV_EXAMPLE  = Join-Path $REPO_ROOT ".env.example"
$ENV_FILE     = Join-Path $REPO_ROOT ".env"
$COMPOSE_FILE = Join-Path $REPO_ROOT "docker-compose.yml"
$ANSIBLE_DIR  = Join-Path $REPO_ROOT "ansible"

if (Test-Path $VERSION_FILE) {
    $PP_VERSION = (Get-Content $VERSION_FILE -Raw).Trim()
} else {
    $PP_VERSION = "0.0.0-dev"
}

$TOTAL_STEPS = 7

# -- Output helpers (match Enable-PatchPilotSSH.ps1 style) ---------------------
function Write-Step {
    param([int]$Num, [string]$Message)
    Write-Host ""
    Write-Host "[$Num/$TOTAL_STEPS] $Message" -ForegroundColor Cyan
}
function Write-Ok {
    param([string]$Message)
    Write-Host "    OK: $Message" -ForegroundColor Green
}
function Write-Skip {
    param([string]$Message)
    Write-Host "    SKIP: $Message" -ForegroundColor Yellow
}
function Write-Fail {
    param([string]$Message)
    Write-Host "    FAIL: $Message" -ForegroundColor Red
}
function Write-Info {
    param([string]$Message)
    Write-Host "    $Message" -ForegroundColor DarkGray
}

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]$identity
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-WindowsVersion {
    $build = [System.Environment]::OSVersion.Version.Build
    return $build -ge 17763  # 1809+
}

# -- Banner --------------------------------------------------------------------
Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  PatchPilot v$PP_VERSION -- Windows Docker Setup" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# -- Clean up auto-resume scheduled task if we're running from one -----------
$resumeTaskName = "PatchPilotInstallResume"
$resumeTask = Get-ScheduledTask -TaskName $resumeTaskName -ErrorAction SilentlyContinue
if ($resumeTask) {
    Write-Info "Resuming install after reboot..."
    Unregister-ScheduledTask -TaskName $resumeTaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Ok "Auto-resume task cleaned up"
    Write-Host ""
}

# -- Pre-flight checks ---------------------------------------------------------
if (-not (Test-Administrator)) {
    Write-Fail "This script must be run as Administrator."
    Write-Host "    Right-click PowerShell and select 'Run as Administrator', then try again."
    exit 1
}

if (-not (Test-WindowsVersion)) {
    Write-Fail "Windows 10 build 1809 or later is required."
    exit 1
}

if (-not (Test-Path $COMPOSE_FILE)) {
    Write-Fail "docker-compose.yml not found at $COMPOSE_FILE"
    Write-Host "    Run this script from the PatchPilot repository root, or ensure the repo is cloned."
    exit 1
}

Write-Info "Repository root: $REPO_ROOT"
Write-Info "Version:         $PP_VERSION"
Write-Host ""

# ==============================================================================
# STEP 1: Python
# ==============================================================================
Write-Step -Num 1 -Message "Checking Python 3"

$PythonCmd = $null

if ($SkipPython) {
    Write-Skip "Python check skipped (-SkipPython)"
} else {
    # Check common python command names
    foreach ($cmd in @("python", "python3", "py")) {
        try {
            $ver = & $cmd --version 2>&1
            if ($ver -match "Python 3\.") {
                $PythonCmd = $cmd
                Write-Ok "Found: $ver ($cmd)"
                break
            }
        } catch {
            # command not found, try next
        }
    }

    if (-not $PythonCmd) {
        Write-Info "Python 3 not found -- installing via winget (this may take a minute)..."

        # Check winget is available
        try {
            $null = Get-Command winget -ErrorAction Stop
        } catch {
            Write-Fail "winget is not available. Install Python 3.11+ manually, then re-run with -SkipPython."
            Write-Host "    Download from: https://www.python.org/downloads/"
            exit 1
        }

        try {
            winget install --id Python.Python.3.12 --source winget --accept-source-agreements --accept-package-agreements --silent
            Write-Ok "Python 3.12 installed via winget"

            # Refresh PATH for this session from registry
            $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
            $userPath    = [Environment]::GetEnvironmentVariable("Path", "User")
            $env:Path    = "$machinePath;$userPath"

            # Also add common Python install locations that winget uses
            # winget installs Python to AppData\Local\Programs\Python\PythonXY
            $pythonSearchPaths = @(
                "$env:LOCALAPPDATA\Programs\Python\Python312",
                "$env:LOCALAPPDATA\Programs\Python\Python312\Scripts",
                "$env:LOCALAPPDATA\Programs\Python\Python311",
                "$env:LOCALAPPDATA\Programs\Python\Python311\Scripts",
                "$env:ProgramFiles\Python312",
                "$env:ProgramFiles\Python312\Scripts",
                "$env:ProgramFiles\Python311",
                "$env:ProgramFiles\Python311\Scripts"
            )
            foreach ($p in $pythonSearchPaths) {
                if ((Test-Path $p) -and ($env:Path -notlike "*$p*")) {
                    $env:Path = "$p;$env:Path"
                }
            }

            # Re-detect
            foreach ($cmd in @("python", "python3", "py")) {
                try {
                    $ver = & $cmd --version 2>&1
                    if ($ver -match "Python 3\.") {
                        $PythonCmd = $cmd
                        Write-Ok "Verified: $ver ($cmd)"
                        break
                    }
                } catch { }
            }

            # Last resort: search the filesystem directly
            if (-not $PythonCmd) {
                $found = Get-ChildItem -Path "$env:LOCALAPPDATA\Programs\Python" -Filter "python.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
                if (-not $found) {
                    $found = Get-ChildItem -Path "$env:ProgramFiles" -Filter "python.exe" -Recurse -Depth 3 -ErrorAction SilentlyContinue | Where-Object { $_.FullName -match "Python3" } | Select-Object -First 1
                }
                if ($found) {
                    $pyDir = $found.DirectoryName
                    $env:Path = "$pyDir;$env:Path"
                    $PythonCmd = $found.FullName
                    $ver = & $PythonCmd --version 2>&1
                    Write-Ok "Found at: $PythonCmd ($ver)"
                }
            }

            if (-not $PythonCmd) {
                Write-Fail "Python installed but not found on PATH."
                Write-Host "    Close and reopen PowerShell as Admin, then re-run this script."
                exit 1
            }
        } catch {
            Write-Fail "Failed to install Python: $_"
            Write-Host "    Install Python 3.11+ manually: https://www.python.org/downloads/"
            exit 1
        }
    }
}

# If we still don't have a Python command (skipped + not found), set a fallback
if (-not $PythonCmd) {
    foreach ($cmd in @("python", "python3", "py")) {
        try {
            $ver = & $cmd --version 2>&1
            if ($ver -match "Python 3\.") { $PythonCmd = $cmd; break }
        } catch { }
    }
}

# ==============================================================================
# STEP 2: Docker runtime detection
# ==============================================================================
Write-Step -Num 2 -Message "Checking Docker runtime"

$DockerReady = $false
$DockerType  = "unknown"

function Test-DockerReady {
    try {
        $info = docker info 2>&1
        if ($LASTEXITCODE -eq 0) { return $true }
    } catch { }
    return $false
}

function Get-DockerType {
    <#
        Detect whether Docker is running via Docker Desktop or WSL2 Engine.
        Returns: "desktop", "wsl2-engine", or "none"
    #>
    # Check for Docker Desktop process
    $desktopProc = Get-Process -Name "Docker Desktop" -ErrorAction SilentlyContinue
    if ($desktopProc) { return "desktop" }

    # Check if docker CLI is available and wsl-based
    try {
        $dockerPath = (Get-Command docker -ErrorAction Stop).Source
        # Docker Desktop installs to Program Files\Docker\Docker
        if ($dockerPath -match "Docker Desktop|Docker\\Docker") { return "desktop" }
        # If docker is in a WSL path or wsl.exe wrapper
        if ($dockerPath -match "wsl") { return "wsl2-engine" }
    } catch { }

    # Check if Docker is accessible via WSL
    try {
        $wslDocker = wsl -e which docker 2>&1
        if ($LASTEXITCODE -eq 0 -and $wslDocker -match "/usr/bin/docker") {
            return "wsl2-engine"
        }
    } catch { }

    return "none"
}

if ($SkipDockerInstall) {
    Write-Skip "Docker install check skipped (-SkipDockerInstall)"
    if (Test-DockerReady) {
        $DockerReady = $true
        $DockerType = Get-DockerType
        Write-Ok "Docker is running ($DockerType)"
    } else {
        Write-Fail "Docker is not running. Start Docker and try again."
        exit 1
    }
} else {
    $DockerType = Get-DockerType

    if ($DockerType -ne "none") {
        Write-Ok "Docker detected: $DockerType"

        if (Test-DockerReady) {
            $DockerReady = $true
            Write-Ok "Docker daemon is responsive"
        } else {
            Write-Info "Docker found but daemon not responding -- waiting..."
            if ($DockerType -eq "desktop") {
                # Try to start Docker Desktop
                $ddPath = "${env:ProgramFiles}\Docker\Docker\Docker Desktop.exe"
                if (Test-Path $ddPath) {
                    Write-Info "Starting Docker Desktop..."
                    Start-Process $ddPath
                }
            }
            # Wait up to 120 seconds
            $waited = 0
            while ($waited -lt 120) {
                Start-Sleep -Seconds 5
                $waited += 5
                Write-Host "." -NoNewline
                if (Test-DockerReady) {
                    $DockerReady = $true
                    Write-Host ""
                    Write-Ok "Docker daemon ready (waited ${waited}s)"
                    break
                }
            }
            if (-not $DockerReady) {
                Write-Host ""
                Write-Fail "Docker daemon did not start within 120 seconds."
                Write-Host "    Start Docker manually and re-run this script."
                exit 1
            }
        }
    } else {
        # No Docker found -- need to install Docker Desktop
        # This requires: Windows features enabled -> WSL2 installed -> Docker Desktop

        try {
            $null = Get-Command winget -ErrorAction Stop
        } catch {
            Write-Fail "winget is not available. Install Docker Desktop manually:"
            Write-Host "    https://docs.docker.com/desktop/install/windows-install/"
            exit 1
        }

        # -- Step 2a: Enable required Windows features --------------------------
        Write-Info "Checking Windows virtualization features..."

        $needsReboot = $false

        # Virtual Machine Platform (required for WSL2)
        $vmpFeature = Get-WindowsOptionalFeature -Online -FeatureName "VirtualMachinePlatform" -ErrorAction SilentlyContinue
        if ($vmpFeature -and $vmpFeature.State -ne "Enabled") {
            Write-Info "Enabling Virtual Machine Platform..."
            $result = Enable-WindowsOptionalFeature -Online -FeatureName "VirtualMachinePlatform" -NoRestart -ErrorAction SilentlyContinue
            if ($result.RestartNeeded) { $needsReboot = $true }
            Write-Ok "Virtual Machine Platform enabled"
        } else {
            Write-Ok "Virtual Machine Platform: already enabled"
        }

        # Windows Hypervisor Platform (optional but improves performance)
        $whpFeature = Get-WindowsOptionalFeature -Online -FeatureName "HypervisorPlatform" -ErrorAction SilentlyContinue
        if ($whpFeature -and $whpFeature.State -ne "Enabled") {
            Write-Info "Enabling Windows Hypervisor Platform..."
            $result = Enable-WindowsOptionalFeature -Online -FeatureName "HypervisorPlatform" -NoRestart -ErrorAction SilentlyContinue
            if ($result.RestartNeeded) { $needsReboot = $true }
            Write-Ok "Windows Hypervisor Platform enabled"
        } else {
            Write-Ok "Windows Hypervisor Platform: already enabled"
        }

        # Microsoft-Windows-Subsystem-Linux (WSL base feature)
        $wslFeature = Get-WindowsOptionalFeature -Online -FeatureName "Microsoft-Windows-Subsystem-Linux" -ErrorAction SilentlyContinue
        if ($wslFeature -and $wslFeature.State -ne "Enabled") {
            Write-Info "Enabling Windows Subsystem for Linux..."
            $result = Enable-WindowsOptionalFeature -Online -FeatureName "Microsoft-Windows-Subsystem-Linux" -NoRestart -ErrorAction SilentlyContinue
            if ($result.RestartNeeded) { $needsReboot = $true }
            Write-Ok "Windows Subsystem for Linux enabled"
        } else {
            Write-Ok "Windows Subsystem for Linux: already enabled"
        }

        # If features were just enabled, a reboot is required before WSL2 works
        if ($needsReboot) {
            Write-Host ""
            Write-Host "    +--------------------------------------------------------------+" -ForegroundColor Yellow
            Write-Host "    |  Windows features were enabled that require a reboot.         |" -ForegroundColor Yellow
            Write-Host "    |                                                              |" -ForegroundColor Yellow
            Write-Host "    |  The installer will automatically resume after you log back   |" -ForegroundColor Yellow
            Write-Host "    |  in. A PowerShell window will open to continue the install.   |" -ForegroundColor Yellow
            Write-Host "    +--------------------------------------------------------------+" -ForegroundColor Yellow
            Write-Host ""

            # Build the command that will run after reboot
            # Use the full path to this script so it works regardless of CWD
            $scriptPath = $PSCommandPath
            $resumeArgs = ""
            if ($SkipPython)        { $resumeArgs += " -SkipPython" }
            if ($WebInstaller)      { $resumeArgs += " -WebInstaller" }
            if ($Unattended)        { $resumeArgs += " -Unattended" }
            if ($Port -ne 8080)     { $resumeArgs += " -Port $Port" }

            $taskName = "PatchPilotInstallResume"
            $psCommand = "Set-ExecutionPolicy Bypass -Scope Process -Force; & '$scriptPath'$resumeArgs"

            # Create a scheduled task that runs once at logon, as the current user, elevated
            try {
                # Remove any leftover task from a previous attempt
                Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

                $action  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoExit -NoProfile -Command `"$psCommand`""
                $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
                $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

                # RunLevel Highest = run as admin
                $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Highest -LogonType Interactive

                Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
                Write-Ok "Scheduled auto-resume task for after reboot"
                Write-Info "Task name: $taskName (will self-delete on next run)"
            } catch {
                Write-Skip "Could not create resume task: $_"
                Write-Host "    After reboot, manually re-run:" -ForegroundColor Cyan
                Write-Host "      & '$scriptPath'" -ForegroundColor Cyan
            }

            if (-not $Unattended) {
                $rebootNow = Read-Host "    Reboot now? [Y/n]"
                if ($rebootNow -notmatch "^[Nn]") {
                    Write-Info "Rebooting in 10 seconds... (Ctrl+C to cancel)"
                    Start-Sleep -Seconds 10
                    Restart-Computer -Force
                }
            }
            Write-Host "    Reboot when ready -- the installer will resume automatically." -ForegroundColor Cyan
            exit 0
        }

        # -- Step 2b: Ensure WSL2 is ready ----------------------------------------
        # Docker Desktop manages its own WSL2 distro, so we only need the kernel
        # updated and WSL2 set as default. We do NOT need to install Ubuntu or
        # any other distro -- that's what causes wsl --install to hang.
        Write-Info "Configuring WSL2..."

        # Check if WSL2 is already functional
        $wslReady = $false
        try {
            $wslStatus = wsl --status 2>&1
            if ($LASTEXITCODE -eq 0) { $wslReady = $true }
        } catch { }

        if ($wslReady) {
            Write-Ok "WSL2 is already configured"
        } else {
            # Set WSL2 as default version (this works even without a distro)
            try {
                wsl --set-default-version 2 2>&1 | Out-Null
                Write-Ok "WSL2 set as default version"
            } catch {
                Write-Info "Could not set WSL2 default (may need kernel update)"
            }

            # Update the WSL kernel (lightweight, no distro download)
            try {
                Write-Info "Updating WSL kernel (this may take a minute)..."
                wsl --update --no-launch 2>&1 | Out-Null
                Write-Ok "WSL kernel updated"
            } catch {
                Write-Info "WSL kernel update returned non-zero (may already be current)"
            }
        }

        # -- Step 2c: Install Docker Desktop ------------------------------------
        Write-Info "Installing Docker Desktop via winget (this may take several minutes)..."
        try {
            winget install --id Docker.DockerDesktop --source winget --accept-source-agreements --accept-package-agreements --silent
            Write-Ok "Docker Desktop installed via winget"
        } catch {
            Write-Fail "Failed to install Docker Desktop: $_"
            Write-Host "    Install manually: https://docs.docker.com/desktop/install/windows-install/"
            exit 1
        }

        # -- Step 2d: Launch and wait for Docker Desktop ------------------------
        Write-Host ""

        # Refresh PATH so we can find Docker Desktop
        $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
        $userPath    = [Environment]::GetEnvironmentVariable("Path", "User")
        $env:Path    = "$machinePath;$userPath"

        # Auto-launch Docker Desktop
        $ddPath = "${env:ProgramFiles}\Docker\Docker\Docker Desktop.exe"
        if (-not (Test-Path $ddPath)) {
            $ddPath = Join-Path $env:LOCALAPPDATA "Docker\Docker Desktop.exe"
        }
        if (Test-Path $ddPath) {
            Write-Info "Launching Docker Desktop..."
            Start-Process $ddPath
        } else {
            Write-Skip "Could not find Docker Desktop executable to auto-launch."
            Write-Host "    Please start Docker Desktop manually." -ForegroundColor Yellow
        }

        Write-Info "Waiting for Docker daemon to start (this may take 60-90 seconds on first run)..."

        # Wait for Docker to come up
        $waited = 0
        $maxWait = 180
        while ($waited -lt $maxWait) {
            Start-Sleep -Seconds 5
            $waited += 5
            Write-Host "." -NoNewline
            if (Test-DockerReady) {
                $DockerReady = $true
                $DockerType = Get-DockerType
                Write-Host ""
                Write-Ok "Docker daemon ready ($DockerType, waited ${waited}s)"
                break
            }
        }

        if (-not $DockerReady) {
            Write-Host ""
            Write-Fail "Docker daemon did not start within ${maxWait} seconds."
            Write-Host ""
            Write-Host "    This can happen if:" -ForegroundColor Yellow
            Write-Host "      - Docker Desktop needs you to accept the license on first run" -ForegroundColor Yellow
            Write-Host "      - A reboot is still pending" -ForegroundColor Yellow
            Write-Host "      - Virtualization is not enabled in BIOS/UEFI settings" -ForegroundColor Yellow
            Write-Host "      - You are running inside a VM without nested virtualization" -ForegroundColor Yellow
            Write-Host ""
            Write-Host "    Next steps:" -ForegroundColor Cyan
            Write-Host "      1. Open Docker Desktop and complete any first-run prompts" -ForegroundColor Cyan
            Write-Host "      2. Re-run this script with: -SkipDockerInstall" -ForegroundColor Cyan
            exit 1
        }
    }
}

# Verify Docker Compose is available
$ComposeCmd = $null
try {
    docker compose version 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $ComposeCmd = "docker compose"
        Write-Ok "Docker Compose plugin: $(docker compose version 2>&1)"
    }
} catch { }

if (-not $ComposeCmd) {
    Write-Fail "Docker Compose not available."
    Write-Host "    Docker Desktop includes Compose by default. Ensure it is enabled in Docker Desktop settings."
    exit 1
}

# ==============================================================================
# STEP 3: Verify Docker + WSL2 status
# ==============================================================================
Write-Step -Num 3 -Message "Verifying Docker and WSL2"

try {
    $wslStatus = wsl --status 2>&1
    if ($wslStatus -match "Default Version: 2|WSL version: 2|WSL 2") {
        Write-Ok "WSL2 is active"
    } elseif ($wslStatus -match "WSL") {
        Write-Ok "WSL detected (Docker Desktop manages the WSL2 lifecycle)"
    } else {
        Write-Info "WSL status check inconclusive -- Docker Desktop will manage this"
    }
} catch {
    Write-Info "Could not query WSL status -- Docker Desktop manages WSL2 automatically"
}

Write-Ok "Docker type: $DockerType"
Write-Ok "Docker Compose: $ComposeCmd"

# ==============================================================================
# STEP 4: Generate .env
# ==============================================================================
Write-Step -Num 4 -Message "Configuring environment (.env)"

if (Test-Path $ENV_FILE) {
    Write-Skip "Existing .env found -- preserving current configuration"
} else {
    if (-not (Test-Path $ENV_EXAMPLE)) {
        Write-Fail ".env.example not found at $ENV_EXAMPLE"
        exit 1
    }

    Copy-Item $ENV_EXAMPLE $ENV_FILE
    Write-Ok "Copied .env.example -> .env"

    # Generate Fernet encryption key
    $fernetKey = $null
    if ($PythonCmd) {
        try {
            $fernetKey = & $PythonCmd -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>&1
            if ($LASTEXITCODE -ne 0) { $fernetKey = $null }
        } catch { }

        # Fallback: base64 random bytes (works without cryptography package)
        if (-not $fernetKey -or $fernetKey -match "ModuleNotFoundError") {
            try {
                $fernetKey = & $PythonCmd -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())" 2>&1
            } catch { }
        }
    }

    # Final fallback: pure PowerShell
    if (-not $fernetKey -or $LASTEXITCODE -ne 0) {
        $bytes = New-Object byte[] 32
        [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $fernetKey = [Convert]::ToBase64String($bytes).Replace("+", "-").Replace("/", "_")
        Write-Info "Generated key via PowerShell (cryptography module not available)"
    }

    # Replace placeholders in .env
    $envContent = Get-Content $ENV_FILE -Raw
    $envContent = $envContent -replace "PATCHPILOT_ENCRYPTION_KEY=CHANGE_ME_FERNET_KEY", "PATCHPILOT_ENCRYPTION_KEY=$fernetKey"
    # Use forward slashes in INSTALL_DIR for Docker mount compatibility
    $installDir = $REPO_ROOT -replace "\\", "/"
    $envContent = $envContent -replace "INSTALL_DIR=/path/to/patchpilot", "INSTALL_DIR=$installDir"
    Set-Content -Path $ENV_FILE -Value $envContent -NoNewline

    Write-Ok "Fernet encryption key generated and saved"
    Write-Ok "INSTALL_DIR set to $REPO_ROOT"
    Write-Host ""
    Write-Host "    ** IMPORTANT: Keep .env safe -- it contains your encryption key **" -ForegroundColor Yellow
}

# ==============================================================================
# STEP 5: Ansible directory
# ==============================================================================
Write-Step -Num 5 -Message "Setting up Ansible directory"

if (-not (Test-Path $ANSIBLE_DIR)) {
    New-Item -ItemType Directory -Path $ANSIBLE_DIR -Force | Out-Null
    Write-Ok "Created $ANSIBLE_DIR"
} else {
    Write-Skip "Ansible directory already exists"
}

# Copy hosts file if not present
$hostsFile = Join-Path $ANSIBLE_DIR "hosts"
$hostsExample = Join-Path $ANSIBLE_DIR "hosts.example"
if (-not (Test-Path $hostsFile)) {
    if (Test-Path $hostsExample) {
        Copy-Item $hostsExample $hostsFile
        Write-Ok "Copied hosts.example -> hosts"
    } else {
        # Create minimal inventory
        Set-Content -Path $hostsFile -Value @"
# PatchPilot Ansible Inventory
# Managed automatically by PatchPilot -- manual edits may be overwritten.
[all]
"@
        Write-Ok "Created empty Ansible inventory"
    }
} else {
    Write-Skip "Ansible hosts file already exists"
}

# Copy playbook if not present
$playbookSrc  = Join-Path $REPO_ROOT "ansible" "check-os-updates.yml"
if (-not (Test-Path $playbookSrc)) {
    # Playbook should already be in the repo's ansible/ dir
    Write-Info "Playbook will be available from the repo's ansible/ directory"
}

# ==============================================================================
# STEP 6: Launch -- Web Installer or Docker Compose
# ==============================================================================
Write-Step -Num 6 -Message "Starting PatchPilot"

if ($WebInstaller) {
    # -- Web Installer path --
    Write-Info "Launching Web Installer UI..."

    $webinstallDir = Join-Path $REPO_ROOT "webinstall"
    $webinstallReqs = Join-Path $webinstallDir "requirements.txt"

    if (-not $PythonCmd) {
        Write-Fail "Python is required for the Web Installer. Install Python 3 and re-run."
        exit 1
    }

    # Install Python dependencies
    Write-Info "Installing web installer dependencies..."
    try {
        & $PythonCmd -m pip install -r $webinstallReqs --quiet 2>&1 | Out-Null
        Write-Ok "Python dependencies installed"
    } catch {
        Write-Fail "Failed to install Python dependencies: $_"
        Write-Host "    Try: $PythonCmd -m pip install -r $webinstallReqs"
        exit 1
    }

    $webPort = 9090
    Write-Ok "Starting web installer on http://localhost:${webPort}"
    Write-Host ""
    Write-Host "    +--------------------------------------------------------------+" -ForegroundColor Green
    Write-Host "    |  Web Installer running at:                                   |" -ForegroundColor Green
    Write-Host "    |    http://localhost:$webPort                                     |" -ForegroundColor Green
    Write-Host "    |                                                              |" -ForegroundColor Green
    Write-Host "    |  Open this URL in your browser to complete setup.            |" -ForegroundColor Green
    Write-Host "    |  Press Ctrl+C to stop the web installer.                     |" -ForegroundColor Green
    Write-Host "    +--------------------------------------------------------------+" -ForegroundColor Green
    Write-Host ""

    # Open browser
    Start-Process "http://localhost:$webPort"

    # Run uvicorn (blocks until Ctrl+C)
    Set-Location $REPO_ROOT
    $env:PATCHPILOT_ROOT = $REPO_ROOT
    & $PythonCmd -m uvicorn webinstall.server:app --host 0.0.0.0 --port $webPort

} else {
    # -- Direct Docker Compose path --
    Write-Info "Starting services via Docker Compose..."

    Set-Location $REPO_ROOT

    # Pull images (don't build -- use pre-built from Docker Hub)
    Write-Info "Pulling container images..."
    Invoke-Expression "$ComposeCmd pull"
    Write-Ok "Images pulled"

    # Start services
    Write-Info "Starting containers..."
    Invoke-Expression "$ComposeCmd up -d"
    Write-Ok "Containers started"

    # Wait for backend health
    Write-Info "Waiting for backend to be healthy..."
    $waited = 0
    $healthy = $false
    while ($waited -lt 120) {
        Start-Sleep -Seconds 3
        $waited += 3
        try {
            $resp = Invoke-WebRequest -Uri "http://localhost:$Port/api/auth/check-setup" -UseBasicParsing -TimeoutSec 3 -ErrorAction SilentlyContinue
            if ($resp.StatusCode -eq 200) {
                $healthy = $true
                break
            }
        } catch { }
        Write-Host "." -NoNewline
    }

    Write-Host ""
    if ($healthy) {
        Write-Ok "Backend healthy (waited ${waited}s)"
    } else {
        Write-Host "    Backend didn't respond in 120s -- check: $ComposeCmd logs backend" -ForegroundColor Yellow
    }

    # Check frontend
    try {
        $null = Invoke-WebRequest -Uri "http://localhost:$Port/" -UseBasicParsing -TimeoutSec 5 -ErrorAction SilentlyContinue
        Write-Ok "Frontend healthy"
    } catch {
        Write-Info "Frontend may still be starting -- check: $ComposeCmd logs frontend"
    }

    # Open browser
    Start-Process "http://localhost:$Port"
}

# ==============================================================================
# STEP 7: Summary
# ==============================================================================
Write-Step -Num 7 -Message "Installation complete"

Write-Host ""
Write-Host "    =============================================" -ForegroundColor Green
Write-Host "      PatchPilot v$PP_VERSION ready (Windows Docker)" -ForegroundColor Green
Write-Host "    =============================================" -ForegroundColor Green
Write-Host ""
Write-Host "    Dashboard:   http://localhost:$Port" -ForegroundColor Cyan
Write-Host "    Docker type: $DockerType" -ForegroundColor DarkGray
Write-Host "    Install dir: $REPO_ROOT" -ForegroundColor DarkGray
Write-Host ""
Write-Host "    Commands:" -ForegroundColor DarkGray
Write-Host "      $ComposeCmd logs -f        # follow logs" -ForegroundColor DarkGray
Write-Host "      $ComposeCmd down           # stop" -ForegroundColor DarkGray
Write-Host "      $ComposeCmd restart        # restart" -ForegroundColor DarkGray
Write-Host "      $ComposeCmd pull && $ComposeCmd up -d  # update" -ForegroundColor DarkGray
Write-Host ""

# Emit credentials marker for web installer SSE (if piped)
if ($WebInstaller) {
    Write-Output "__CREDENTIALS__{""url"":""http://localhost:$Port"",""mode"":""docker-windows""}"
}
