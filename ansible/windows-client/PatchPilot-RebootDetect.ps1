# Apply path: reboot detection without Ansible win_shell (hidden launcher from playbook).
$ErrorActionPreference = 'SilentlyContinue'
$reboot = $false

$wuKey = Get-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired" -ErrorAction SilentlyContinue
if ($wuKey) { $reboot = $true }

$cbsKey = Get-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending" -ErrorAction SilentlyContinue
if ($cbsKey) { $reboot = $true }

$applyFile = "C:\ProgramData\PatchPilot\winget-apply.txt"
if (Test-Path $applyFile) {
    $content = Get-Content $applyFile -Raw -ErrorAction SilentlyContinue
    if ($content -match "Restart the application|restart.*to complete") {
        $reboot = $true
    }
}

if ($reboot) { Write-Output "REBOOT_REQUIRED" } else { Write-Output "NO_REBOOT" }
