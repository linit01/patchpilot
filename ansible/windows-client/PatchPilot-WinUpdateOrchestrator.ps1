# Invoked hidden via PatchPilot-AnsibleHiddenLaunch.vbs — writes lines for Ansible slurp.
$ErrorActionPreference = 'SilentlyContinue'
$pp = 'C:\ProgramData\PatchPilot'
$outputFile = Join-Path $pp 'winupdate-check.txt'
$outStd = Join-Path $pp 'winupdate-ansible-stdout.txt'
$taskName = 'PP-WinUpdate'

$linesOut = [System.Collections.ArrayList]@()

$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $task) {
    [void]$linesOut.Add('NO_UPDATES')
    [void]$linesOut.Add('WINUPDATE_TASK_MISSING: PP-WinUpdate scheduled task not found.')
    $linesOut | Set-Content -Path $outStd -Encoding UTF8
    exit 0
}

# ── Auto-fix: migrate legacy task to wscript launcher ─────────────────────
# Existing installs registered PP-WinUpdate with powershell.exe as the
# executable, which flashes conhost on Win11.  Switch to wscript.exe.
# PSWindowsUpdate needs admin, so RunLevel stays Highest.
$currentExe = $task.Actions[0].Execute
$vbsPath = Join-Path $pp 'PatchPilot-AnsibleHiddenLaunch.vbs'
$wuScript = Join-Path $pp 'PatchPilot-WinUpdateTask.ps1'
if ($currentExe -match 'powershell' -and (Test-Path $vbsPath) -and (Test-Path $wuScript)) {
    try {
        $wscriptExe = Join-Path $env:SystemRoot 'System32\wscript.exe'
        $newArgs = '//nologo //B "' + $vbsPath + '" "' + $wuScript + '" Check'
        $newAction = New-ScheduledTaskAction -Execute $wscriptExe -Argument $newArgs
        $newSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
        $taskUser = $task.Principal.UserId
        Register-ScheduledTask -TaskName $taskName -Action $newAction -Settings $newSettings -User $taskUser -RunLevel Highest -Force | Out-Null
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    } catch {
        # Non-fatal — proceed with original task
    }
}

if (Test-Path $outputFile) { Remove-Item $outputFile -Force }

Start-ScheduledTask -TaskName $taskName
$timeout = 120
$elapsed = 0
while ($elapsed -lt $timeout) {
    Start-Sleep -Seconds 5
    $elapsed += 5
    $state = (Get-ScheduledTask -TaskName $taskName).State
    if ($state -ne 'Running') { break }
}

if (-not (Test-Path $outputFile)) {
    [void]$linesOut.Add('NO_UPDATES')
    $linesOut | Set-Content -Path $outStd -Encoding UTF8
    exit 0
}

$raw = Get-Content $outputFile -Raw -Encoding UTF8
$fileLines = $raw -split "`n"
$count = 0
foreach ($line in $fileLines) {
    $line = $line.Trim()
    if (-not $line) { continue }
    if ($line -match '^\s*-+') { continue }
    if ($line -match '^\s*Status\s+') { continue }
    if ($line -match '^\s*X\s+ComputerName') { continue }
    if ($line -match 'KB\d+') {
        $kbMatch = [regex]::Match($line, '(KB\d+)')
        $kb = $kbMatch.Value
        $sizeMatch = [regex]::Match($line, '(\d+(?:\.\d+)?)\s*([KMGT]i?B)', 'IgnoreCase')
        $size = if ($sizeMatch.Success) { $sizeMatch.Value } else { '' }
        $title = $line -replace '^\s*\d+\s+\S+\s+\S+\s+', '' `
            -replace 'KB\d+', '' `
            -replace '\d+(?:\.\d+)?\s*[KMGT]i?B', '' `
            -replace '^\s*[\-\s]+', '' `
            -replace '\s{2,}', ' '
        $title = $title.Trim()
        if (-not $title) { $title = 'Windows Update' }
        [void]$linesOut.Add("$title ($kb) installed -> available")
        $count++
    }
}

if ($count -eq 0) {
    [void]$linesOut.Add('NO_UPDATES')
}

$linesOut | Set-Content -Path $outStd -Encoding UTF8
