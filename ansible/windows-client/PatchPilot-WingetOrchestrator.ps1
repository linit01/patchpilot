# Invoked hidden via PatchPilot-AnsibleHiddenLaunch.vbs — writes lines for Ansible slurp.
$ErrorActionPreference = 'SilentlyContinue'
$pp = 'C:\ProgramData\PatchPilot'
$excludeFile = Join-Path $pp 'winget-excluded-ids.txt'
$outputFile = Join-Path $pp 'winget-check.txt'
$outStd = Join-Path $pp 'winget-ansible-stdout.txt'

$linesOut = [System.Collections.ArrayList]@()

$excluded = @()
if (Test-Path $excludeFile) {
    $excludedRaw = Get-Content $excludeFile -Raw -ErrorAction SilentlyContinue
    if ($excludedRaw) {
        $excluded = $excludedRaw.Trim() -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }
    }
}

$task = Get-ScheduledTask -TaskName 'PP-WingetCheck' -ErrorAction SilentlyContinue
if (-not $task) {
    [void]$linesOut.Add('NO_UPDATES')
    [void]$linesOut.Add('WINGET_TASK_MISSING: PP-WingetCheck scheduled task not found. Re-run Enable-PatchPilotSSH.ps1.')
    $linesOut | Set-Content -Path $outStd -Encoding UTF8
    exit 0
}

# ── Auto-fix: migrate legacy task to wscript launcher + Limited RunLevel ──
# Existing installs registered PP-WingetCheck with powershell.exe + Highest,
# which flashes conhost on Win11.  Fix in-place without requiring re-setup.
$needsReRegister = $false
$taskUser = $task.Principal.UserId

# Check 1: task executable should be wscript.exe, not powershell.exe
$currentExe = $task.Actions[0].Execute
$vbsPath = Join-Path $pp 'PatchPilot-AnsibleHiddenLaunch.vbs'
$wingetScript = Join-Path $pp 'PatchPilot-WingetTask.ps1'
if ($currentExe -match 'powershell' -and (Test-Path $vbsPath) -and (Test-Path $wingetScript)) {
    $needsReRegister = $true
}

# Check 2: RunLevel should be Limited (winget check doesn't need admin)
if ($task.Principal.RunLevel -eq 'Highest') {
    $needsReRegister = $true
}

if ($needsReRegister -and (Test-Path $vbsPath) -and (Test-Path $wingetScript)) {
    try {
        $wscriptExe = Join-Path $env:SystemRoot 'System32\wscript.exe'
        $newArgs = '//nologo //B "' + $vbsPath + '" "' + $wingetScript + '" Check'
        $newAction = New-ScheduledTaskAction -Execute $wscriptExe -Argument $newArgs
        $newSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
        Register-ScheduledTask -TaskName 'PP-WingetCheck' -Action $newAction -Settings $newSettings -User $taskUser -RunLevel Limited -Force | Out-Null
        # Re-fetch the task after re-registration
        $task = Get-ScheduledTask -TaskName 'PP-WingetCheck' -ErrorAction SilentlyContinue
    } catch {
        # Non-fatal — proceed with original task
    }
}

if (Test-Path $outputFile) { Remove-Item $outputFile -Force }

Start-ScheduledTask -TaskName 'PP-WingetCheck'
$timeout = 60
$elapsed = 0
while ($elapsed -lt $timeout) {
    Start-Sleep -Seconds 3
    $elapsed += 3
    $state = (Get-ScheduledTask -TaskName 'PP-WingetCheck').State
    if ($state -ne 'Running') { break }
}

if (-not (Test-Path $outputFile)) {
    [void]$linesOut.Add('NO_UPDATES')
    $linesOut | Set-Content -Path $outStd -Encoding UTF8
    exit 0
}

$raw = Get-Content $outputFile -Raw -Encoding UTF8
$lines = $raw -split "`n"
$headerIdx = -1
for ($i = 0; $i -lt $lines.Count; $i++) {
    if ($lines[$i] -match '^\s*Name\s+Id\s+Version\s+Available') {
        $headerIdx = $i
        break
    }
}
if ($headerIdx -lt 0) {
    [void]$linesOut.Add('NO_UPDATES')
    $linesOut | Set-Content -Path $outStd -Encoding UTF8
    exit 0
}

$header = $lines[$headerIdx]
$idStart = $header.IndexOf('Id')
$verStart = $header.IndexOf('Version')
$availStart = $header.IndexOf('Available')
$srcStart = $header.IndexOf('Source')

$count = 0
for ($i = $headerIdx + 1; $i -lt $lines.Count; $i++) {
    $line = $lines[$i]
    if ($line -match '^\s*-+' -or $line -match '^\s*$' -or $line -match 'upgrades? available') { continue }
    if ($line.Length -le $availStart) { continue }

    $name = $line.Substring(0, $idStart).Trim()
    $id = $line.Substring($idStart, $verStart - $idStart).Trim()
    $ver = $line.Substring($verStart, $availStart - $verStart).Trim()
    if ($srcStart -gt 0 -and $line.Length -gt $srcStart) {
        $avail = $line.Substring($availStart, $srcStart - $availStart).Trim()
    } else {
        $avail = $line.Substring($availStart).Trim()
    }

    $isExcluded = $false
    foreach ($exc in $excluded) {
        if ($id -eq $exc -or $id -like "$exc*" -or $exc -like "$id*") {
            $isExcluded = $true
            break
        }
    }

    if (-not $isExcluded -and $id -and $avail) {
        [void]$linesOut.Add("$name ($id) $ver -> $avail")
        $count++
    }
}

if ($count -eq 0) {
    [void]$linesOut.Add('NO_UPDATES')
}

$linesOut | Set-Content -Path $outStd -Encoding UTF8
