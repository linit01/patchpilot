<#
.SYNOPSIS
    PatchPilot -- Bootstrap Installer for Windows.

.DESCRIPTION
    irm https://getpatchpilot.app/install.ps1 | iex
      -> auto-detects best download method (no prompts)

    Or download and run directly:
      irm https://getpatchpilot.app/install.ps1 -OutFile install-patchpilot.ps1
      .\install-patchpilot.ps1

    This is the Windows equivalent of:
      curl -fsSL https://getpatchpilot.app/install.sh | bash

    What it does:
      1. Downloads PatchPilot (git clone or release zip -- auto-detected)
      2. Checks prerequisites (Docker, Python)
      3. Launches the full installer (scripts\windows\Install-PatchPilot.ps1)

    The full installer handles everything from there: Python install, Docker
    Desktop install, .env generation, and Docker Compose startup.

.PARAMETER InstallDir
    Where to install PatchPilot. Default: .\patchpilot (current directory).
    Override with: $env:PATCHPILOT_DIR = "C:\PatchPilot" before running.

.PARAMETER GitHubToken
    GitHub personal access token. Optional — only needed if cloning from a
    private fork; the upstream PatchPilot repository is public.

.NOTES
    Requires: Windows 10 (build 1809+) or Windows 11
    Must run as: Administrator
    Project: https://github.com/linit01/patchpilot
#>

[CmdletBinding()]
param(
    [string]$InstallDir = "",
    [string]$GitHubToken = ""
)

$ErrorActionPreference = "Stop"

$REPO_URL     = "https://github.com/linit01/patchpilot.git"
$RELEASE_API  = "https://api.github.com/repos/linit01/patchpilot/releases/latest"
$REPO_OWNER   = "linit01"
$REPO_NAME    = "patchpilot"

# Default install dir: env override > param > .\patchpilot
if (-not $InstallDir) {
    $InstallDir = if ($env:PATCHPILOT_DIR) { $env:PATCHPILOT_DIR } else { Join-Path $PWD "patchpilot" }
}

# -- Output helpers (match install.sh style) -----------------------------------
function Write-Ok   { param([string]$m) Write-Host "[OK] $m" -ForegroundColor Green }
function Write-Err  { param([string]$m) Write-Host "[FAIL] $m" -ForegroundColor Red }
function Write-Warn { param([string]$m) Write-Host "[WARN] $m" -ForegroundColor Yellow }
function Write-Info { param([string]$m) Write-Host "[INFO] $m" -ForegroundColor Blue }

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]$identity
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# -- Banner --------------------------------------------------------------------
Write-Host ""
Write-Host "    ____        __       __    ____  _ __      __" -ForegroundColor Magenta
Write-Host "   / __ \____ _/ /______/ /_  / __ \(_) /___  / /_" -ForegroundColor Magenta
Write-Host "  / /_/ / __  / __/ ___/ __ \/ /_/ / / / __ \/ __/" -ForegroundColor Magenta
Write-Host " / ____/ /_/ / /_/ /__/ / / / ____/ / / /_/ / /_" -ForegroundColor Magenta
Write-Host "/_/    \__,_/\__/\___/_/ /_/_/   /_/_/\____/\__/" -ForegroundColor Magenta
Write-Host ""
Write-Host "Bootstrap Installer (Windows) -- https://getpatchpilot.app" -ForegroundColor Blue
Write-Host ""

# -- Pre-flight ----------------------------------------------------------------
if (-not (Test-Administrator)) {
    Write-Err "This script must be run as Administrator."
    Write-Host "    Right-click PowerShell and select 'Run as Administrator', then try again."
    exit 1
}

# -- Handle existing directory -------------------------------------------------
if (Test-Path $InstallDir) {
    Write-Warn "Directory '$InstallDir' already exists."

    # Check if we're in a terminal (interactive)
    if ([Environment]::UserInteractive -and $Host.Name -eq "ConsoleHost") {
        $confirm = Read-Host "    Overwrite? [y/N]"
        if ($confirm -match "^[Yy]") {
            Remove-Item $InstallDir -Recurse -Force
        } else {
            Write-Info "Aborted."
            exit 0
        }
    } else {
        Write-Err "Directory already exists. Remove it or set PATCHPILOT_DIR env var to a different path."
        exit 1
    }
}

# -- Detect available tools ----------------------------------------------------
$HasGit = $false
try { $null = Get-Command git -ErrorAction Stop; $HasGit = $true } catch { }

# -- GitHub API headers --------------------------------------------------------
$GHHeaders = @{ "User-Agent" = "PatchPilot-Installer"; "Accept" = "application/vnd.github+json" }
if ($GitHubToken) { $GHHeaders["Authorization"] = "Bearer $GitHubToken" }

# -- Choose download method ----------------------------------------------------
$Method = ""

if ([Environment]::UserInteractive -and $Host.Name -eq "ConsoleHost" -and $HasGit) {
    # Interactive -- let user choose
    Write-Host "How would you like to download PatchPilot?" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  1) " -NoNewline
    Write-Host "git clone" -ForegroundColor Green -NoNewline
    Write-Host "        -- latest code, easy to update with git pull"
    Write-Host "  2) " -NoNewline
    Write-Host "Release zip" -ForegroundColor Blue -NoNewline
    Write-Host "      -- stable release archive, no git required"
    Write-Host ""

    $choice = ""
    while ($choice -ne "1" -and $choice -ne "2") {
        $choice = Read-Host "Choose [1/2]"
    }
    switch ($choice) {
        "1" { $Method = "clone" }
        "2" { $Method = "zip" }
    }
} elseif ($HasGit) {
    $Method = "clone"
    Write-Info "Using git clone"
} else {
    $Method = "zip"
    Write-Info "Using release zip (git not found)"
}

# -- Download ------------------------------------------------------------------
Write-Host ""

if ($Method -eq "clone") {
    Write-Info "Cloning PatchPilot..."
    try {
        if ($GitHubToken) {
            # Inject token for private repo clone
            $authUrl = "https://${GitHubToken}@github.com/${REPO_OWNER}/${REPO_NAME}.git"
            git clone --depth 1 $authUrl $InstallDir 2>&1 | Out-Null
        } else {
            git clone --depth 1 $REPO_URL $InstallDir 2>&1 | Out-Null
        }
        Write-Ok "Cloned to $InstallDir"
    } catch {
        Write-Err "git clone failed: $_"
        if (-not $GitHubToken) {
            Write-Host "    If the repo is private, re-run with -GitHubToken." -ForegroundColor Yellow
        }
        exit 1
    }
} else {
    Write-Info "Fetching latest release..."

    # Resolve the tag name first (metadata endpoints need fewer permissions)
    $tag = $null
    try {
        $release = Invoke-RestMethod -Uri $RELEASE_API -Headers $GHHeaders -TimeoutSec 15
        $tag = $release.tag_name
        Write-Info "Latest release: $tag"
    } catch {
        # Fallback: try tags
        try {
            $tags = Invoke-RestMethod -Uri "https://api.github.com/repos/$REPO_OWNER/$REPO_NAME/tags?per_page=1" -Headers $GHHeaders -TimeoutSec 15
            if ($tags.Count -gt 0) {
                $tag = $tags[0].name
                Write-Info "Latest tag: $tag"
            }
        } catch { }
    }

    if (-not $tag) {
        $tag = "main"
        Write-Warn "Could not determine version -- using main branch"
    }

    $tempZip = Join-Path $env:TEMP "patchpilot-download.zip"
    $tempExtract = Join-Path $env:TEMP ("patchpilot-extract-" + [System.IO.Path]::GetRandomFileName())
    $downloaded = $false

    # Strategy 1: GitHub API zipball endpoint
    # Requires fine-grained PAT with Contents: Read-only
    $zipballUrl = "https://api.github.com/repos/$REPO_OWNER/$REPO_NAME/zipball/$tag"
    try {
        Write-Info "Downloading via API zipball..."
        Invoke-WebRequest -Uri $zipballUrl -Headers $GHHeaders -OutFile $tempZip -TimeoutSec 120
        $downloaded = $true
        Write-Ok "Downloaded via API zipball"
    } catch {
        Write-Info "API zipball failed (token may need Contents:Read permission)"
    }

    # Strategy 2: Direct archive URL with token in header
    if (-not $downloaded) {
        $archiveUrl = "https://github.com/$REPO_OWNER/$REPO_NAME/archive/refs/tags/$tag.zip"
        if ($tag -eq "main") {
            $archiveUrl = "https://github.com/$REPO_OWNER/$REPO_NAME/archive/refs/heads/main.zip"
        }
        $archiveHeaders = @{ "User-Agent" = "PatchPilot-Installer" }
        if ($GitHubToken) { $archiveHeaders["Authorization"] = "Bearer $GitHubToken" }

        try {
            Write-Info "Trying direct archive URL..."
            Invoke-WebRequest -Uri $archiveUrl -Headers $archiveHeaders -OutFile $tempZip -TimeoutSec 120
            $downloaded = $true
            Write-Ok "Downloaded via archive URL"
        } catch {
            Write-Info "Direct archive URL failed"
        }
    }

    # Strategy 3: Fall back to git clone if zip methods both failed
    if (-not $downloaded) {
        Write-Warn "Zip download failed -- falling back to git clone"

        # Check if git is available now (user might have it even though we chose zip)
        $gitAvail = $false
        try { $null = Get-Command git -ErrorAction Stop; $gitAvail = $true } catch { }

        if ($gitAvail -and $GitHubToken) {
            try {
                $authUrl = "https://${GitHubToken}@github.com/${REPO_OWNER}/${REPO_NAME}.git"
                git clone --depth 1 --branch $tag $authUrl $InstallDir 2>&1 | Out-Null
                Write-Ok "Cloned $tag to $InstallDir"
                # Skip the extract step below -- we already have the repo
                $downloaded = $null  # sentinel: clone succeeded, skip extract
            } catch {
                Write-Err "git clone also failed: $_"
            }
        }

        if ($downloaded -eq $false) {
            Write-Err "All download methods failed."
            Write-Host ""
            Write-Host "    For private repos, your GitHub token needs these permissions:" -ForegroundColor Yellow
            Write-Host "      Fine-grained PAT: Contents -> Read-only" -ForegroundColor Yellow
            Write-Host "      Classic PAT:      repo scope" -ForegroundColor Yellow
            Write-Host ""
            Write-Host "    Update token at: https://github.com/settings/tokens" -ForegroundColor Yellow
            exit 1
        }
    }

    # Extract zip (skip if git clone was used above)
    if ($downloaded -eq $true) {
        try {
            Write-Info "Extracting..."
            Expand-Archive -Path $tempZip -DestinationPath $tempExtract -Force

            # GitHub zips extract to "owner-repo-hash/" -- find the actual content dir
            $innerDir = Get-ChildItem -Path $tempExtract -Directory | Select-Object -First 1
            if (-not $innerDir) {
                Write-Err "Could not find extracted content."
                exit 1
            }

            # Move to install dir
            Move-Item -Path $innerDir.FullName -Destination $InstallDir -Force
            Write-Ok "Extracted to $InstallDir"
        } catch {
            Write-Err "Extraction failed: $_"
            exit 1
        } finally {
            Remove-Item $tempZip -Force -ErrorAction SilentlyContinue
            Remove-Item $tempExtract -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

# -- Check prerequisites -------------------------------------------------------
Write-Host ""
Write-Info "Checking prerequisites..."

$Missing = @()
try { $null = Get-Command docker -ErrorAction Stop } catch { $Missing += "docker" }

$pyFound = $false
foreach ($cmd in @("python", "python3", "py")) {
    try {
        $ver = & $cmd --version 2>&1
        if ($ver -match "Python 3\.") { $pyFound = $true; break }
    } catch { }
}
if (-not $pyFound) { $Missing += "python3" }

if ($Missing.Count -gt 0) {
    Write-Host ""
    Write-Warn "Missing prerequisites: $($Missing -join ', ')"
    Write-Host ""
    Write-Host "    That's OK -- the full installer will install them automatically via winget." -ForegroundColor Cyan
    Write-Host ""
} else {
    Write-Ok "All prerequisites found"
}

# -- Launch full installer -----------------------------------------------------
Write-Host ""
Write-Info "Launching PatchPilot installer..."
Write-Host ""

$installerScript = Join-Path (Join-Path (Join-Path $InstallDir "scripts") "windows") "Install-PatchPilot.ps1"

if (-not (Test-Path $installerScript)) {
    Write-Err "Install-PatchPilot.ps1 not found at $installerScript"
    Write-Host "    The download may be incomplete. Try running this script again." -ForegroundColor Yellow
    exit 1
}

Set-Location $InstallDir
& "$installerScript"
