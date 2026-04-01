<#
.SYNOPSIS
    Prepares a Windows host for PatchPilot management by creating a dedicated
    service account, configuring OpenSSH Server, firewall rules, and public
    key authentication.

.DESCRIPTION
    This script automates the setup required for PatchPilot to manage a Windows host
    via SSH. It performs the following:

    1. Creates a dedicated 'patchpilot' local admin account (service account)
    2. Initializes the service account's user profile
    3. Installs OpenSSH Server (if not already present)
    4. Starts the sshd service and sets it to auto-start
    5. Creates/confirms a Windows Firewall rule for TCP port 22
    6. Configures sshd_config for public key authentication
    7. Sets PowerShell as the default SSH shell (required for Ansible)
    8. Installs the provided SSH public key for the patchpilot account
    9. Optionally installs the PSWindowsUpdate module (for OS-level patching)

    The 'patchpilot' account uses a random password (key-only auth), is added to
    the local Administrators group, and is configured with a non-expiring password.
    This avoids issues with spaces in usernames, provides isolation from personal
    accounts, and follows the standard pattern for remote management tooling.

    Run this script as Administrator on each Windows host you want to manage
    with PatchPilot.

.PARAMETER PublicKey
    The SSH public key string to authorize (e.g., "ssh-ed25519 AAAA... patchpilot").
    If not provided, the script will prompt for it.

.PARAMETER ServiceUser
    The name of the local service account to create. Default: "patchpilot".

.PARAMETER SSHPort
    The port sshd should listen on. Default: 22.

.PARAMETER SkipPSWindowsUpdate
    If set, skips installation of the PSWindowsUpdate PowerShell module.

.PARAMETER Unattended
    Run without interactive prompts. Requires -PublicKey to be provided.

.EXAMPLE
    .\Enable-PatchPilotSSH.ps1 -PublicKey "ssh-ed25519 AAAA..."

.EXAMPLE
    .\Enable-PatchPilotSSH.ps1 -PublicKey "ssh-ed25519 AAAA..." -Unattended -SkipPSWindowsUpdate

.EXAMPLE
    .\Enable-PatchPilotSSH.ps1 -PublicKey "ssh-ed25519 AAAA..." -ServiceUser "pp-svc"

.NOTES
    Requires: Windows 10 (build 1809+) or Windows 11
    Must run as: Administrator
    Project:    PatchPilot -- https://github.com/linit01/patchpilot
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$PublicKey,

    [Parameter(Mandatory = $false)]
    [string]$ServiceUser = "patchpilot",

    [Parameter(Mandatory = $false)]
    [int]$SSHPort = 22,

    [Parameter(Mandatory = $false)]
    [switch]$SkipPSWindowsUpdate,

    [Parameter(Mandatory = $false)]
    [switch]$Unattended
)

# -- Strict mode & constants ---------------------------------------------------
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$SSHD_CONFIG = "$env:ProgramData\ssh\sshd_config"
$ADMIN_KEYS  = "$env:ProgramData\ssh\administrators_authorized_keys"
$TOTAL_STEPS = 10

# -- Helper functions ----------------------------------------------------------

function Write-Step {
    param([string]$Num, [string]$Message)
    Write-Host "`n[$Num/$TOTAL_STEPS] $Message" -ForegroundColor Cyan
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
    $identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-WindowsVersion {
    $build = [int](Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion").CurrentBuildNumber
    # OpenSSH Server optional feature requires Windows 10 1809 (build 17763) or later
    return $build -ge 17763
}

function New-RandomPassword {
    param([int]$Length = 32)
    # Generate a cryptographically random password.
    # Uses uppercase, lowercase, digits, and symbols to satisfy complexity requirements.
    $chars  = 'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#$%^&*()-_=+'
    $bytes  = New-Object byte[] $Length
    $rng    = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $password = -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
    return $password
}

# -- Pre-flight checks --------------------------------------------------------

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  PatchPilot -- Windows Host Setup" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# Must be Administrator
if (-not (Test-Administrator)) {
    Write-Fail "This script must be run as Administrator."
    Write-Host "    Right-click PowerShell and select 'Run as Administrator', then try again."
    exit 1
}

# Must be Windows 10 1809+ or Windows 11
if (-not (Test-WindowsVersion)) {
    Write-Fail "Windows 10 build 17763 (version 1809) or later is required."
    Write-Host "    OpenSSH Server is not available as an optional feature on older builds."
    exit 1
}

# Validate service account name -- no spaces, reasonable length
if ($ServiceUser -match '\s') {
    Write-Fail "Service account name cannot contain spaces: '$ServiceUser'"
    exit 1
}
if ($ServiceUser.Length -gt 20) {
    Write-Fail "Service account name is too long (max 20 characters): '$ServiceUser'"
    exit 1
}

# Get public key if not provided
if ([string]::IsNullOrWhiteSpace($PublicKey)) {
    if ($Unattended) {
        Write-Fail "-PublicKey is required when running with -Unattended."
        exit 1
    }
    Write-Host "Enter the PatchPilot SSH public key."
    Write-Host "(Copy this from PatchPilot Settings > SSH Keys > Public Key)" -ForegroundColor DarkGray
    $PublicKey = Read-Host "Public key"
    if ([string]::IsNullOrWhiteSpace($PublicKey)) {
        Write-Fail "No public key provided. Aborting."
        exit 1
    }
}

# Basic public key format validation
$PublicKey = $PublicKey.Trim()
if ($PublicKey -notmatch "^ssh-(rsa|ed25519|ecdsa)\s+\S+") {
    Write-Fail "The provided key does not look like a valid SSH public key."
    Write-Host "    Expected format: ssh-ed25519 AAAA... [comment]"
    exit 1
}

Write-Host "Configuration:" -ForegroundColor DarkGray
Write-Host "    Service account:       $ServiceUser" -ForegroundColor DarkGray
Write-Host "    SSH Port:              $SSHPort" -ForegroundColor DarkGray
Write-Host "    PSWindowsUpdate:       $(if ($SkipPSWindowsUpdate) { 'skip' } else { 'install' })" -ForegroundColor DarkGray
Write-Host "    Public Key:            $($PublicKey.Substring(0, [Math]::Min(50, $PublicKey.Length)))..." -ForegroundColor DarkGray

if (-not $Unattended) {
    Write-Host ""
    $confirm = Read-Host "Proceed with setup? (Y/n)"
    if ($confirm -and $confirm -notin @("y", "Y", "yes", "Yes", "")) {
        Write-Host "Aborted by user."
        exit 0
    }
}

# -- Step 1: Create dedicated service account ----------------------------------

Write-Step "1" "Creating service account '$ServiceUser'..."

$password = $null
$securePassword = $null
$userExists = $false

try {
    $null = Get-LocalUser -Name $ServiceUser -ErrorAction Stop
    $userExists = $true
    Write-Ok "Account '$ServiceUser' already exists."
} catch {
    # User does not exist -- expected path for first run
}

if (-not $userExists) {
    try {
        $password = New-RandomPassword -Length 32
        $securePassword = ConvertTo-SecureString $password -AsPlainText -Force

        New-LocalUser `
            -Name $ServiceUser `
            -Password $securePassword `
            -FullName "PatchPilot Service Account" `
            -Description "PatchPilot remote management" `
            -PasswordNeverExpires `
            -UserMayNotChangePassword `
            -AccountNeverExpires `
            -ErrorAction Stop | Out-Null

        Write-Ok "Account '$ServiceUser' created."
        Write-Info "Password is random (32-char) and not stored -- SSH uses key auth only."
    } catch {
        Write-Fail "Could not create account '$ServiceUser': $_"
        exit 1
    }
} else {
    # Ensure password never expires on existing account
    try {
        Set-LocalUser -Name $ServiceUser -PasswordNeverExpires $true -ErrorAction SilentlyContinue
    } catch {
        Write-Info "Could not update password policy on existing account -- non-critical."
    }
}

# Add to Administrators group if not already a member
try {
    $isMember = Get-LocalGroupMember -Group "Administrators" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "*\$ServiceUser" }

    if ($isMember) {
        Write-Ok "'$ServiceUser' is already in the Administrators group."
    } else {
        Add-LocalGroupMember -Group "Administrators" -Member $ServiceUser -ErrorAction Stop
        Write-Ok "'$ServiceUser' added to Administrators group."
    }
} catch {
    Write-Fail "Could not add '$ServiceUser' to Administrators group: $_"
    Write-Host "    Manual fix: net localgroup Administrators $ServiceUser /add" -ForegroundColor DarkGray
}

# Hide the service account from the Windows login screen
try {
    $regHidePath = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList"
    if (-not (Test-Path $regHidePath)) {
        New-Item -Path $regHidePath -Force | Out-Null
    }
    New-ItemProperty -Path $regHidePath -Name $ServiceUser -Value 0 -PropertyType DWord -Force | Out-Null
    Write-Ok "'$ServiceUser' hidden from Windows login screen."
} catch {
    Write-Info "Could not hide account from login screen -- cosmetic, non-critical."
}

# -- Step 2: Initialize user profile -------------------------------------------
# The user profile directory must exist before we can place SSH keys in it.
# Windows creates profiles on first interactive login -- we force it here.

Write-Step "2" "Initializing user profile for '$ServiceUser'..."

$profilePath = "C:\Users\$ServiceUser"

if (Test-Path $profilePath) {
    Write-Ok "User profile already exists at $profilePath"
} else {
    Write-Host "    Creating user profile (first-time initialization)..."
    $profileCreated = $false

    # Method 1: If we just created the account, we have the password
    if ($password -and $securePassword) {
        try {
            $cred = New-Object System.Management.Automation.PSCredential(
                ".\$ServiceUser", $securePassword)
            # Start-Process with -LoadUserProfile triggers profile creation
            $proc = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "echo ok" `
                -Credential $cred -LoadUserProfile -NoNewWindow -Wait -PassThru `
                -ErrorAction Stop
            if (Test-Path $profilePath) {
                $profileCreated = $true
                Write-Ok "User profile created at $profilePath"
            }
        } catch {
            Write-Info "Start-Process profile creation failed: $_"
        }
    }

    # Method 2: Check registry for an existing profile path
    if (-not $profileCreated) {
        try {
            $profileList = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList"
            $userSID = (Get-LocalUser -Name $ServiceUser).SID.Value
            $regProfile = Get-ItemProperty -Path "$profileList\$userSID" -ErrorAction SilentlyContinue
            if ($regProfile -and $regProfile.ProfileImagePath -and (Test-Path $regProfile.ProfileImagePath)) {
                $profilePath = $regProfile.ProfileImagePath
                $profileCreated = $true
                Write-Ok "User profile found at $profilePath (from registry)"
            }
        } catch {
            Write-Info "Registry profile lookup failed -- non-critical."
        }
    }

    if (-not $profileCreated) {
        Write-Skip "Could not create user profile -- SSH keys will use administrators_authorized_keys only."
        Write-Info "This is fine -- admin users authenticate via the centralized key file."
    }
}

# Initialize winget for the service account (accept source agreements)
# This must happen after profile creation and while we have the credentials.
# Without this, winget hangs on first SSH run waiting for interactive acceptance.
if ($password -and $securePassword) {
    Write-Host "    Initializing winget for '$ServiceUser'..."
    try {
        $cred = New-Object System.Management.Automation.PSCredential(
            ".\$ServiceUser", $securePassword)
        $proc = Start-Process -FilePath "powershell.exe" `
            -ArgumentList "-NoProfile", "-Command", "winget list --accept-source-agreements --count 1 2>&1 | Out-Null; exit 0" `
            -Credential $cred -LoadUserProfile -NoNewWindow -Wait -PassThru `
            -ErrorAction Stop
        Write-Ok "winget source agreements accepted for '$ServiceUser'."
    } catch {
        Write-Info "winget initialization failed: $_ -- you can run 'winget list --accept-source-agreements' manually via SSH."
    }
} else {
    Write-Info "Skipping winget init -- no credentials available for existing account."
    Write-Info "Run 'winget list --accept-source-agreements' via SSH as '$ServiceUser' if needed."
}

# -- Step 3: Install OpenSSH Server --------------------------------------------

Write-Step "3" "Installing OpenSSH Server..."

$sshCapability = Get-WindowsCapability -Online | Where-Object { $_.Name -like "OpenSSH.Server*" }

if ($null -eq $sshCapability) {
    Write-Fail "OpenSSH Server capability not found on this system."
    exit 1
}

if ($sshCapability.State -eq "Installed") {
    Write-Ok "OpenSSH Server is already installed."
} else {
    Write-Host "    Installing OpenSSH Server (this may take a minute)..."
    $result = Add-WindowsCapability -Online -Name $sshCapability.Name
    if ($result.RestartNeeded) {
        Write-Host "    NOTE: A reboot may be required to complete installation." -ForegroundColor Yellow
    }
    Write-Ok "OpenSSH Server installed."
}

# -- Step 4: Start and enable sshd service -------------------------------------

Write-Step "4" "Configuring sshd service..."

try {
    $service = Get-Service -Name sshd -ErrorAction Stop

    # Set to auto-start
    Set-Service -Name sshd -StartupType Automatic
    Write-Ok "sshd startup type set to Automatic."

    # Start if not running
    if ($service.Status -ne "Running") {
        Start-Service sshd
        Write-Ok "sshd service started."
    } else {
        Write-Ok "sshd service is already running."
    }
} catch {
    Write-Fail "Could not configure sshd service: $_"
    exit 1
}

# Also enable ssh-agent for key management
try {
    Set-Service -Name ssh-agent -StartupType Automatic -ErrorAction SilentlyContinue
    Start-Service ssh-agent -ErrorAction SilentlyContinue
    Write-Ok "ssh-agent service started."
} catch {
    Write-Skip "ssh-agent service not available -- non-critical."
}

# -- Step 5: Configure firewall rule -------------------------------------------

Write-Step "5" "Configuring Windows Firewall..."

# Ensure the active network profile is Private (not Public).
# Windows blocks many inbound connections on Public networks even with
# explicit firewall rules. Homelab/LAN networks should be Private.
$netProfiles = Get-NetConnectionProfile -ErrorAction SilentlyContinue
foreach ($profile in $netProfiles) {
    if ($profile.NetworkCategory -eq "Public") {
        try {
            Set-NetConnectionProfile -InterfaceIndex $profile.InterfaceIndex -NetworkCategory Private
            Write-Ok "Network '$($profile.Name)' on $($profile.InterfaceAlias) changed from Public to Private."
        } catch {
            Write-Fail "Could not change network profile for '$($profile.InterfaceAlias)' from Public to Private: $_"
            Write-Info "SSH may not work until you change this manually:"
            Write-Info "  Set-NetConnectionProfile -InterfaceIndex $($profile.InterfaceIndex) -NetworkCategory Private"
        }
    } else {
        Write-Ok "Network '$($profile.Name)' on $($profile.InterfaceAlias) is already $($profile.NetworkCategory)."
    }
}

$ruleName = "PatchPilot-SSH-Inbound-TCP-$SSHPort"

# Check for existing rules on the target port
$existingRules = Get-NetFirewallRule -Direction Inbound -ErrorAction SilentlyContinue |
    Where-Object { $_.Enabled -eq "True" } |
    Get-NetFirewallPortFilter -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -eq $SSHPort -and $_.Protocol -eq "TCP" }

if ($existingRules) {
    Write-Ok "Firewall rule for TCP port $SSHPort already exists."
} else {
    New-NetFirewallRule `
        -Name $ruleName `
        -DisplayName "PatchPilot SSH ($SSHPort/TCP)" `
        -Description "Allow inbound SSH for PatchPilot remote management" `
        -Direction Inbound `
        -Protocol TCP `
        -LocalPort $SSHPort `
        -Action Allow `
        -Profile Domain, Private, Public `
        -ErrorAction Stop | Out-Null
    Write-Ok "Firewall rule created: $ruleName -- all network profiles."
}

# -- Step 6: Configure sshd_config ---------------------------------------------

Write-Step "6" "Configuring sshd_config..."

if (-not (Test-Path $SSHD_CONFIG)) {
    Write-Fail "sshd_config not found at $SSHD_CONFIG"
    Write-Host "    Try restarting sshd first: Restart-Service sshd"
    exit 1
}

# Backup original config
$backupPath = "$SSHD_CONFIG.bak.$(Get-Date -Format 'yyyyMMddHHmmss')"
Copy-Item -Path $SSHD_CONFIG -Destination $backupPath -Force
Write-Ok "Backed up sshd_config to $backupPath"

$config = Get-Content $SSHD_CONFIG -Raw

# Settings we need to ensure
$requiredSettings = @{
    "PubkeyAuthentication"  = "yes"
    "PasswordAuthentication" = "no"
    "AuthorizedKeysFile"    = ".ssh/authorized_keys"
    "Port"                  = "$SSHPort"
}

foreach ($key in $requiredSettings.Keys) {
    $value = $requiredSettings[$key]
    # Match both commented and uncommented lines
    $pattern = "(?m)^#?\s*$key\s+.*$"
    $replacement = "$key $value"

    if ($config -match $pattern) {
        $config = $config -replace $pattern, $replacement
    } else {
        # Append if not present at all
        $config += "`n$replacement"
    }
}

# CRITICAL: The default sshd_config on Windows has a Match Group administrators
# block at the bottom that overrides AuthorizedKeysFile to
# __PROGRAMDATA__/ssh/administrators_authorized_keys for admin users.
# We keep this block -- it ensures our patchpilot admin user reads keys from
# the central administrators_authorized_keys file which we control the ACL on.
# We install keys in BOTH locations for maximum robustness.

Set-Content -Path $SSHD_CONFIG -Value $config -Force
Write-Ok "sshd_config updated: PubkeyAuth=yes, PasswordAuth=no, Port=$SSHPort."

# -- Step 7: Set PowerShell as default SSH shell -------------------------------

Write-Step "7" "Setting PowerShell as default SSH shell..."

$regPath = "HKLM:\SOFTWARE\OpenSSH"
$psPath  = "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

# Prefer PowerShell 7 if installed
$pwsh7 = "C:\Program Files\PowerShell\7\pwsh.exe"
if (Test-Path $pwsh7) {
    $psPath = $pwsh7
    Write-Ok "Found PowerShell 7 -- using $psPath"
} else {
    Write-Ok "Using Windows PowerShell -- $psPath"
}

if (-not (Test-Path $regPath)) {
    New-Item -Path $regPath -Force | Out-Null
}

New-ItemProperty -Path $regPath -Name DefaultShell -Value $psPath -PropertyType String -Force | Out-Null
Write-Ok "Default SSH shell set to: $psPath"

# -- Step 8: Install SSH public key --------------------------------------------

Write-Step "8" "Installing SSH public key for '$ServiceUser'..."

# Install in the service user's .ssh/authorized_keys (if profile exists)
if (Test-Path $profilePath) {
    $userSshDir = Join-Path $profilePath ".ssh"
    $userKeys   = Join-Path $userSshDir "authorized_keys"

    if (-not (Test-Path $userSshDir)) {
        New-Item -Path $userSshDir -ItemType Directory -Force | Out-Null
    }

    $keyInstalled = $false
    if (Test-Path $userKeys) {
        $existingKeys = Get-Content $userKeys -Raw -ErrorAction SilentlyContinue
        if ($existingKeys -and $existingKeys.Contains($PublicKey)) {
            Write-Ok "Key already present in $userKeys"
            $keyInstalled = $true
        }
    }

    if (-not $keyInstalled) {
        Add-Content -Path $userKeys -Value $PublicKey -Encoding UTF8
        Write-Ok "Key added to $userKeys"
    }

    # Fix ownership -- must be locked down or sshd ignores the file
    try {
        $acl = New-Object System.Security.AccessControl.FileSecurity
        $acl.SetAccessRuleProtection($true, $false)
        $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
            "NT AUTHORITY\SYSTEM", "FullControl", "Allow")))
        $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
            "BUILTIN\Administrators", "FullControl", "Allow")))
        $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
            $ServiceUser, "Read", "Allow")))
        Set-Acl -Path $userKeys -AclObject $acl
        Write-Ok "ACL set on $userKeys"
    } catch {
        Write-Info "Could not set ACL on user authorized_keys -- non-critical if admin keys work."
    }
} else {
    Write-Info "User profile not available -- relying on administrators_authorized_keys."
}

# Always install in administrators_authorized_keys (covers the Match Group block).
# This is the PRIMARY auth path for admin users on Windows.
$adminKeyInstalled = $false
if (Test-Path $ADMIN_KEYS) {
    $existingAdminKeys = Get-Content $ADMIN_KEYS -Raw -ErrorAction SilentlyContinue
    if ($existingAdminKeys -and $existingAdminKeys.Contains($PublicKey)) {
        Write-Ok "Key already present in $ADMIN_KEYS"
        $adminKeyInstalled = $true
    }
}

if (-not $adminKeyInstalled) {
    $adminSshDir = Split-Path $ADMIN_KEYS -Parent
    if (-not (Test-Path $adminSshDir)) {
        New-Item -Path $adminSshDir -ItemType Directory -Force | Out-Null
    }
    Add-Content -Path $ADMIN_KEYS -Value $PublicKey -Encoding UTF8
    Write-Ok "Key added to $ADMIN_KEYS"
}

# Fix permissions on administrators_authorized_keys
# This is the #1 gotcha on Windows SSH -- if the ACL is wrong, sshd silently
# ignores the file and key auth fails with no useful error message.
try {
    $acl = New-Object System.Security.AccessControl.FileSecurity
    # Disable inheritance
    $acl.SetAccessRuleProtection($true, $false)
    # SYSTEM: Full Control
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
        "NT AUTHORITY\SYSTEM", "FullControl", "Allow")))
    # Administrators: Full Control
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
        "BUILTIN\Administrators", "FullControl", "Allow")))
    Set-Acl -Path $ADMIN_KEYS -AclObject $acl
    Write-Ok "ACL set on $ADMIN_KEYS (SYSTEM + Administrators only)."
} catch {
    Write-Fail "Could not set ACL on $ADMIN_KEYS -- SSH key auth may not work."
    Write-Host "    Error: $_" -ForegroundColor DarkGray
    Write-Host "    Manual fix: icacls `"$ADMIN_KEYS`" /inheritance:r /grant `"SYSTEM:(F)`" /grant `"Administrators:(F)`"" -ForegroundColor DarkGray
}

# -- Step 9: Optional -- Install PSWindowsUpdate --------------------------------

Write-Step "9" "PSWindowsUpdate module..."

if ($SkipPSWindowsUpdate) {
    Write-Skip "Skipped (-SkipPSWindowsUpdate was set)."
    Write-Info "PatchPilot can still check winget app updates without this module."
    Write-Info "OS-level Windows Updates will require PSWindowsUpdate in a future release."
} else {
    $module = Get-Module -ListAvailable -Name PSWindowsUpdate -ErrorAction SilentlyContinue
    if ($module) {
        Write-Ok "PSWindowsUpdate is already installed (v$($module.Version))."
    } else {
        Write-Host "    Installing PSWindowsUpdate from PSGallery..."
        try {
            # Ensure NuGet provider is available (required for Install-Module)
            $null = Install-PackageProvider -Name NuGet -MinimumVersion 2.8.5.201 -Force -ErrorAction SilentlyContinue
            Install-Module -Name PSWindowsUpdate -Force -Scope AllUsers -AllowClobber
            Write-Ok "PSWindowsUpdate installed."
        } catch {
            Write-Fail "Could not install PSWindowsUpdate: $_"
            Write-Info "You can install it manually later: Install-Module PSWindowsUpdate -Force"
            Write-Info "PatchPilot will still work for winget app updates without it."
        }
    }
}

# -- Step 10: Create winget check scheduled task --------------------------------
# winget's MSIX sandbox requires a full interactive user session and cannot run
# in a non-interactive SSH session under a service account. The workaround is a
# scheduled task that runs as the user who installed PatchPilot (the current
# admin running this script) whose account has a working winget context.
# PatchPilot's Ansible playbook triggers this task on-demand, waits for it to
# complete, and reads the output file.

Write-Step "10" "Creating winget check scheduled task..."

$ppDataDir = "C:\ProgramData\PatchPilot"
if (-not (Test-Path $ppDataDir)) {
    New-Item -Path $ppDataDir -ItemType Directory -Force | Out-Null
}

# Grant patchpilot user write access to the data directory so Ansible can
# clean up stale output files before triggering a new check.
try {
    $dirAcl = Get-Acl $ppDataDir
    $dirAcl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
        $ServiceUser, "Modify", "ContainerInherit,ObjectInherit", "None", "Allow")))
    Set-Acl -Path $ppDataDir -AclObject $dirAcl
    Write-Ok "Granted '$ServiceUser' write access to $ppDataDir"
} catch {
    Write-Info "Could not set ACL on $ppDataDir -- winget output may require manual cleanup."
}

# Set machine-wide execution policy to Bypass so PSWindowsUpdate and other
# modules can load without prompts in scheduled tasks and SSH sessions.
try {
    Set-ExecutionPolicy Bypass -Scope LocalMachine -Force
    Write-Ok "Execution policy set to Bypass (machine-wide)."
} catch {
    Write-Info "Could not set execution policy -- PSWindowsUpdate may not load in scheduled tasks."
}

# The task runs as the current admin user (who has working winget and WU access).
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

# ---- Install hidden launcher VBS + helper scripts ----
# wscript.exe is a GUI subsystem app — it does NOT allocate a console, so
# Win11 will not flash conhost when Task Scheduler starts it.  The .vbs script
# launches powershell.exe with window style 0 (hidden), which in turn uses
# ProcessStartInfo.CreateNoWindow for child processes like winget.exe.
$wscriptExe = Join-Path $env:SystemRoot "System32\wscript.exe"
$hiddenVbsSrc = Join-Path $PSScriptRoot "PatchPilot-AnsibleHiddenLaunch.vbs"
$hiddenVbsDest = Join-Path $ppDataDir "PatchPilot-AnsibleHiddenLaunch.vbs"
if (Test-Path $hiddenVbsSrc) {
    Copy-Item -Path $hiddenVbsSrc -Destination $hiddenVbsDest -Force
    Write-Ok "Installed PatchPilot-AnsibleHiddenLaunch.vbs → $hiddenVbsDest"
} else {
    Write-Info "PatchPilot-AnsibleHiddenLaunch.vbs not found beside this script."
    Write-Info "Expected path: $hiddenVbsSrc"
    Write-Info "Scheduled tasks will fall back to powershell.exe (may flash on Win11)."
}

# ---- PP-WingetCheck scheduled task ----
# winget check does NOT need admin — register at Limited RunLevel.
# Using wscript as the executable eliminates the conhost flash entirely.
$taskName = "PP-WingetCheck"
$wingetScriptSrc = Join-Path $PSScriptRoot "PatchPilot-WingetTask.ps1"
$wingetScriptDest = Join-Path $ppDataDir "PatchPilot-WingetTask.ps1"
if (Test-Path $wingetScriptSrc) {
    Copy-Item -Path $wingetScriptSrc -Destination $wingetScriptDest -Force
    Write-Ok "Installed PatchPilot-WingetTask.ps1 → $wingetScriptDest"
}

if ((Test-Path $hiddenVbsDest) -and (Test-Path $wingetScriptDest)) {
    $wingetTaskArgs = '//nologo //B "' + $hiddenVbsDest + '" "' + $wingetScriptDest + '" Check'
    $taskAction = New-ScheduledTaskAction -Execute $wscriptExe -Argument $wingetTaskArgs
} elseif (Test-Path $wingetScriptDest) {
    Write-Info "VBS launcher not available — using powershell.exe directly for PP-WingetCheck."
    $wingetPsArgs = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$wingetScriptDest`" Check"
    $taskAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $wingetPsArgs
} else {
    Write-Info "PatchPilot-WingetTask.ps1 not found — using legacy winget command line."
    $wingetCmd = "winget upgrade --include-unknown --accept-source-agreements 2>&1 | Out-File '$ppDataDir\winget-check.txt' -Encoding UTF8"
    $wingetPsArgs = "-NoProfile -WindowStyle Hidden -Command `"$wingetCmd`""
    $taskAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $wingetPsArgs
}
$taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

$existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Write-Ok "Scheduled task '$taskName' already exists -- updating."
}

try {
    if ($Unattended) {
        Write-Info "Registering scheduled task as '$currentUser'..."
        Write-Info "In unattended mode, the task may need to be re-registered with credentials."
    }
    # Limited RunLevel — winget check does not need elevation.
    # Apply (winget upgrade) temporarily swaps the action to Highest via the playbook.
    Register-ScheduledTask -TaskName $taskName -Action $taskAction -Settings $taskSettings -User $currentUser -RunLevel Limited -Force | Out-Null
    Write-Ok "Scheduled task '$taskName' registered as '$currentUser' (RunLevel: Limited)."
    Write-Info "PatchPilot triggers this task on-demand to check for winget updates."
} catch {
    Write-Fail "Could not create scheduled task '$taskName': $_"
    Write-Info "You can create it manually:"
    Write-Info "  `$w='$wscriptExe'; `$a='//nologo //B `"$hiddenVbsDest`" `"$wingetScriptDest`" Check'"
    Write-Info "  Register-ScheduledTask -TaskName '$taskName' -Action (New-ScheduledTaskAction -Execute `$w -Argument `$a) -User '$currentUser' -RunLevel Limited"
}

# ---- PP-WinUpdate scheduled task ----
# PSWindowsUpdate needs admin → RunLevel Highest.  Still use wscript launcher
# to avoid the conhost flash; the task itself requests elevation silently
# because it runs under the registered user's stored credentials.
if (-not $SkipPSWindowsUpdate) {
    $wuTaskName = "PP-WinUpdate"
    $wuScriptSrc = Join-Path $PSScriptRoot "PatchPilot-WinUpdateTask.ps1"
    $wuScriptDest = Join-Path $ppDataDir "PatchPilot-WinUpdateTask.ps1"
    if (Test-Path $wuScriptSrc) {
        Copy-Item -Path $wuScriptSrc -Destination $wuScriptDest -Force
        Write-Ok "Installed PatchPilot-WinUpdateTask.ps1 → $wuScriptDest"
    }

    if ((Test-Path $hiddenVbsDest) -and (Test-Path $wuScriptDest)) {
        $wuTaskArgs = '//nologo //B "' + $hiddenVbsDest + '" "' + $wuScriptDest + '" Check'
        $wuTaskAction = New-ScheduledTaskAction -Execute $wscriptExe -Argument $wuTaskArgs
    } else {
        $wuCmd = "Import-Module PSWindowsUpdate; Get-WindowsUpdate -MicrosoftUpdate 2>&1 | Out-File '$ppDataDir\winupdate-check.txt' -Encoding UTF8"
        $wuTaskAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$wuCmd`""
    }
    $wuTaskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

    $existingWuTask = Get-ScheduledTask -TaskName $wuTaskName -ErrorAction SilentlyContinue
    if ($existingWuTask) {
        Write-Ok "Scheduled task '$wuTaskName' already exists -- updating."
    }

    try {
        Register-ScheduledTask -TaskName $wuTaskName -Action $wuTaskAction -Settings $wuTaskSettings -User $currentUser -RunLevel Highest -Force | Out-Null
        Write-Ok "Scheduled task '$wuTaskName' registered as '$currentUser' (RunLevel: Highest)."
        Write-Info "PatchPilot triggers this task on-demand to check for Windows Updates."
    } catch {
        Write-Fail "Could not create scheduled task '$wuTaskName': $_"
        Write-Info "You can create it manually:"
        Write-Info "  `$w='$wscriptExe'; `$a='//nologo //B `"$hiddenVbsDest`" `"$wuScriptDest`" Check'"
        Write-Info "  Register-ScheduledTask -TaskName '$wuTaskName' -Action (New-ScheduledTaskAction -Execute `$w -Argument `$a) -User '$currentUser' -RunLevel Highest"
    }
} else {
    Write-Info "Skipping PP-WinUpdate scheduled task (PSWindowsUpdate was skipped)."
}

# -- Restart sshd to apply config changes --------------------------------------

Write-Host ""
Write-Host "Restarting sshd to apply configuration..." -ForegroundColor Cyan
Restart-Service sshd
Write-Ok "sshd restarted."

# -- Verification --------------------------------------------------------------

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  Verification" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan

$allPassed = $true

# Check service account exists and is admin
$svcUser = Get-LocalUser -Name $ServiceUser -ErrorAction SilentlyContinue
if ($svcUser) {
    Write-Ok "Service account '$ServiceUser' exists (enabled: $($svcUser.Enabled))."
    $isAdmin = Get-LocalGroupMember -Group "Administrators" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "*\$ServiceUser" }
    if ($isAdmin) {
        Write-Ok "'$ServiceUser' is in the Administrators group."
    } else {
        Write-Fail "'$ServiceUser' is NOT in the Administrators group."
        $allPassed = $false
    }
} else {
    Write-Fail "Service account '$ServiceUser' does NOT exist."
    $allPassed = $false
}

# Check sshd is running
$sshdStatus = (Get-Service sshd).Status
if ($sshdStatus -eq "Running") {
    Write-Ok "sshd service is running."
} else {
    Write-Fail "sshd service is NOT running (status: $sshdStatus)."
    $allPassed = $false
}

# Check port is listening
$listener = Get-NetTCPConnection -LocalPort $SSHPort -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    Write-Ok "Port $SSHPort is listening."
} else {
    Write-Fail "Port $SSHPort is NOT listening."
    Write-Info "Check Windows Event Viewer > Applications and Services > OpenSSH for errors."
    $allPassed = $false
}

# Check firewall
$fwRules = Get-NetFirewallRule -Direction Inbound -Enabled True -ErrorAction SilentlyContinue |
    Where-Object { $_.Action -eq "Allow" } |
    Get-NetFirewallPortFilter -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -eq $SSHPort -and $_.Protocol -eq "TCP" }
if ($fwRules) {
    Write-Ok "Firewall allows inbound TCP $SSHPort."
} else {
    Write-Fail "No firewall rule found for inbound TCP $SSHPort."
    $allPassed = $false
}

# Check default shell
$defaultShell = (Get-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell -ErrorAction SilentlyContinue).DefaultShell
if ($defaultShell) {
    Write-Ok "Default SSH shell: $defaultShell"
} else {
    Write-Fail "Default SSH shell not configured."
    $allPassed = $false
}

# Check authorized keys file exists and contains our key
if (Test-Path $ADMIN_KEYS) {
    $keyContent = Get-Content $ADMIN_KEYS -Raw -ErrorAction SilentlyContinue
    if ($keyContent -and $keyContent.Contains($PublicKey)) {
        Write-Ok "SSH public key installed in $ADMIN_KEYS"
    } else {
        Write-Fail "SSH public key NOT found in $ADMIN_KEYS"
        $allPassed = $false
    }
} else {
    Write-Fail "$ADMIN_KEYS does not exist."
    $allPassed = $false
}

# Check scheduled tasks
$ppTask = Get-ScheduledTask -TaskName "PP-WingetCheck" -ErrorAction SilentlyContinue
if ($ppTask) {
    Write-Ok "Scheduled task 'PP-WingetCheck' exists (runs as: $($ppTask.Principal.UserId))."
} else {
    Write-Fail "Scheduled task 'PP-WingetCheck' not found -- winget checks will not work."
    $allPassed = $false
}

$wuTask = Get-ScheduledTask -TaskName "PP-WinUpdate" -ErrorAction SilentlyContinue
if ($wuTask) {
    Write-Ok "Scheduled task 'PP-WinUpdate' exists (runs as: $($wuTask.Principal.UserId))."
} elseif (-not $SkipPSWindowsUpdate) {
    Write-Fail "Scheduled task 'PP-WinUpdate' not found -- Windows Update checks will not work."
    $allPassed = $false
} else {
    Write-Info "Scheduled task 'PP-WinUpdate' skipped (PSWindowsUpdate not installed)."
}

# Summary
Write-Host ""
if ($allPassed) {
    Write-Host "================================================" -ForegroundColor Green
    Write-Host "  Setup Complete -- All checks passed!" -ForegroundColor Green
    Write-Host "================================================" -ForegroundColor Green
} else {
    Write-Host "================================================" -ForegroundColor Yellow
    Write-Host "  Setup Complete -- Some checks failed" -ForegroundColor Yellow
    Write-Host "================================================" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Review the failures above before adding this" -ForegroundColor Yellow
    Write-Host "  host to PatchPilot." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Next steps in PatchPilot:" -ForegroundColor Cyan
Write-Host "  1. Add this host using its hostname or IP address"
Write-Host "  2. Set the SSH user to:  $ServiceUser"
Write-Host "  3. Set the SSH port to:  $SSHPort"
Write-Host "  4. Run a host check to verify connectivity"
Write-Host ""
Write-Host "Test SSH from your PatchPilot server:" -ForegroundColor Cyan
Write-Host "  ssh -i <your-private-key> $ServiceUser@$(hostname)" -ForegroundColor White
Write-Host ""

if (-not $allPassed) {
    Write-Host "Troubleshooting:" -ForegroundColor Yellow
    Write-Host "  - ACL:  icacls `"$ADMIN_KEYS`""
    Write-Host "  - Logs: Windows Event Viewer > Applications and Services > OpenSSH"
    Write-Host "  - Key mismatch: Ensure the PatchPilot private key matches the public key above"
    Write-Host ""
}
