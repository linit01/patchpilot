# Used by scheduled task PP-WinUpdate and apply playbook — run via wscript hidden launcher.
param(
    [Parameter(Position = 0)]
    [ValidateSet('Check', 'Apply')]
    [string]$Action = 'Check'
)

$ErrorActionPreference = 'SilentlyContinue'
$ppData = 'C:\ProgramData\PatchPilot'
if (-not (Test-Path $ppData)) {
    New-Item -Path $ppData -ItemType Directory -Force | Out-Null
}

Import-Module PSWindowsUpdate -ErrorAction SilentlyContinue

if ($Action -eq 'Apply') {
    $outFile = Join-Path $ppData 'winupdate-apply.txt'
    Install-WindowsUpdate -MicrosoftUpdate -AcceptAll -AutoReboot:$false -Confirm:$false 2>&1 |
        Out-File $outFile -Encoding UTF8
} else {
    $outFile = Join-Path $ppData 'winupdate-check.txt'
    Get-WindowsUpdate -MicrosoftUpdate 2>&1 | Out-File $outFile -Encoding UTF8
}
