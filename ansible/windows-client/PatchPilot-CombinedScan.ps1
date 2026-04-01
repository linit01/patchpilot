# Single hidden entry: winget + optional WU orchestrators, then one JSON file
# for one Ansible slurp.  Config (exclusion list, WU flag) is passed via env
# vars and written to disk here so the playbook needs NO separate win_copy
# calls — each win_copy spawns powershell.exe which flashes conhost on Win11.
$ErrorActionPreference = 'SilentlyContinue'
$pp = $PSScriptRoot
if (-not $pp) { $pp = 'C:\ProgramData\PatchPilot' }

# ── Write config files from env vars (set by Ansible environment: block) ──
# This replaces per-cycle win_copy tasks that each flash conhost on Win11.
if ($env:PP_WINGET_EXCLUDED_IDS) {
    [System.IO.File]::WriteAllText(
        (Join-Path $pp 'winget-excluded-ids.txt'),
        "$($env:PP_WINGET_EXCLUDED_IDS)`n",
        [System.Text.UTF8Encoding]::new($false))
}

$wuEnabled = ($env:WINUPDATE_ENABLED -eq 'true')
$wuFlagPath = Join-Path $pp 'winupdate-enabled.flag'
if ($env:WINUPDATE_ENABLED) {
    [System.IO.File]::WriteAllText(
        $wuFlagPath,
        $env:WINUPDATE_ENABLED,
        [System.Text.UTF8Encoding]::new($false))
} elseif (Test-Path $wuFlagPath) {
    $wuEnabled = ((Get-Content $wuFlagPath -Raw -ErrorAction SilentlyContinue).Trim() -eq 'true')
}

# ── Check helpers version stamp ───────────────────────────────────────────
# Compare the on-disk stamp to the expected version from env.  If they differ,
# report needs_refresh=true so Ansible can push new helpers and re-run us.
$verFile = Join-Path $pp '.patchpilot_helpers_version'
$helpersVer = ''
if (Test-Path $verFile) {
    $helpersVer = (Get-Content $verFile -Raw -ErrorAction SilentlyContinue).Trim()
}
$expectedVer = if ($env:PATCHPILOT_APP_VERSION) { $env:PATCHPILOT_APP_VERSION.Trim() } else { 'unknown' }
$needsRefresh = ($helpersVer -ne $expectedVer)

# ── Run orchestrators ─────────────────────────────────────────────────────
$wingetOrc = Join-Path $pp 'PatchPilot-WingetOrchestrator.ps1'
$wuOrc = Join-Path $pp 'PatchPilot-WinUpdateOrchestrator.ps1'
$bundle = Join-Path $pp 'ansible-scan-bundle.json'

if (Test-Path $wingetOrc) { & $wingetOrc }
if ($wuEnabled -and (Test-Path $wuOrc)) { & $wuOrc }

# ── Collect results ───────────────────────────────────────────────────────
$wgPath = Join-Path $pp 'winget-ansible-stdout.txt'
$wuPath = Join-Path $pp 'winupdate-ansible-stdout.txt'
$wg = if (Test-Path $wgPath) { Get-Content $wgPath -Raw -Encoding UTF8 } else { '' }
$wu = if ($wuEnabled -and (Test-Path $wuPath)) { Get-Content $wuPath -Raw -Encoding UTF8 } else { '' }

$json = ([pscustomobject]@{
    winget = $wg
    winupdate = $wu
    helpers_version = $helpersVer
    needs_refresh = $needsRefresh
} | ConvertTo-Json -Compress)
[System.IO.File]::WriteAllText($bundle, $json, [System.Text.UTF8Encoding]::new($false))
