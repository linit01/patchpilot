# Single hidden entry: winget + optional WU orchestrators, then one JSON file for one Ansible slurp.
$ErrorActionPreference = 'SilentlyContinue'
$pp = $PSScriptRoot
if (-not $pp) { $pp = 'C:\ProgramData\PatchPilot' }

$wingetOrc = Join-Path $pp 'PatchPilot-WingetOrchestrator.ps1'
$wuOrc = Join-Path $pp 'PatchPilot-WinUpdateOrchestrator.ps1'
$bundle = Join-Path $pp 'ansible-scan-bundle.json'
$wuFlag = Join-Path $pp 'winupdate-enabled.flag'

if (Test-Path $wingetOrc) { & $wingetOrc }

$wuEnabled = ($env:WINUPDATE_ENABLED -eq 'true')
if (-not $wuEnabled -and (Test-Path $wuFlag)) {
    $wuEnabled = ((Get-Content $wuFlag -Raw -ErrorAction SilentlyContinue).Trim() -eq 'true')
}
if ($wuEnabled -and (Test-Path $wuOrc)) { & $wuOrc }

$wgPath = Join-Path $pp 'winget-ansible-stdout.txt'
$wuPath = Join-Path $pp 'winupdate-ansible-stdout.txt'
# Use [IO.File]::ReadAllText instead of Get-Content — Get-Content returns a
# PS object whose metadata (PSPath, PSDrive, etc.) bleeds into ConvertTo-Json
# on some PowerShell versions, producing a dict instead of a plain string.
$wg = if (Test-Path $wgPath) { [System.IO.File]::ReadAllText($wgPath) } else { '' }
$wu = if ($wuEnabled -and (Test-Path $wuPath)) { [System.IO.File]::ReadAllText($wuPath) } else { '' }

$json = ([pscustomobject]@{ winget = $wg; winupdate = $wu } | ConvertTo-Json -Compress)
[System.IO.File]::WriteAllText($bundle, $json, [System.Text.UTF8Encoding]::new($false))
