# Patch apply: swap PP-WingetCheck to winget Apply via wscript, run task, restore Check (no Ansible win_shell).
$ErrorActionPreference = 'SilentlyContinue'
$taskName = "PP-WingetCheck"
$outputFile = "C:\ProgramData\PatchPilot\winget-apply.txt"
$wscript = "$env:SystemRoot\System32\wscript.exe"
$vbs = "C:\ProgramData\PatchPilot\PatchPilot-AnsibleHiddenLaunch.vbs"
$wingetScript = "C:\ProgramData\PatchPilot\PatchPilot-WingetTask.ps1"

$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Output "WINGET_TASK_MISSING: $taskName not found."
    exit 1
}

$applyAction = New-ScheduledTaskAction -Execute $wscript -Argument ('//nologo //B "' + $vbs + '" "' + $wingetScript + '" Apply')
Set-ScheduledTask -TaskName $taskName -Action $applyAction | Out-Null

if (Test-Path $outputFile) { Remove-Item $outputFile -Force }

Start-ScheduledTask -TaskName $taskName
$timeout = 1800
$elapsed = 0
while ($elapsed -lt $timeout) {
    Start-Sleep -Seconds 10
    $elapsed += 10
    $state = (Get-ScheduledTask -TaskName $taskName).State
    if ($state -ne 'Running') { break }
}

if ($elapsed -ge $timeout) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Write-Output "WARNING: Winget apply timed out after 30 minutes."
}

$checkAction = New-ScheduledTaskAction -Execute $wscript -Argument ('//nologo //B "' + $vbs + '" "' + $wingetScript + '" Check')
Set-ScheduledTask -TaskName $taskName -Action $checkAction | Out-Null

if (Test-Path $outputFile) {
    Get-Content $outputFile -Raw
} else {
    Write-Output "No output from winget apply."
}

$taskInfo = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction SilentlyContinue
$exitCode = if ($taskInfo) { $taskInfo.LastTaskResult } else { -1 }
Write-Output "WINGET_APPLY_EXIT: $exitCode"
