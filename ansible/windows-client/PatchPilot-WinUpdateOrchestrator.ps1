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
