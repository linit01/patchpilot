# Patch apply: swap PP-WinUpdate to Apply via wscript + PatchPilot-WinUpdateTask.ps1, restore Check.
$ErrorActionPreference = 'SilentlyContinue'
$taskName = "PP-WinUpdate"
$outputFile = "C:\ProgramData\PatchPilot\winupdate-apply.txt"
$wscript = "$env:SystemRoot\System32\wscript.exe"
$vbs = "C:\ProgramData\PatchPilot\PatchPilot-AnsibleHiddenLaunch.vbs"
$wuScript = "C:\ProgramData\PatchPilot\PatchPilot-WinUpdateTask.ps1"

$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Output "WINUPDATE_TASK_MISSING: $taskName not found."
    exit 1
}

$applyAction = New-ScheduledTaskAction -Execute $wscript -Argument ('//nologo //B "' + $vbs + '" "' + $wuScript + '" Apply')
Set-ScheduledTask -TaskName $taskName -Action $applyAction | Out-Null

if (Test-Path $outputFile) { Remove-Item $outputFile -Force }

Start-ScheduledTask -TaskName $taskName
$timeout = 3600
$elapsed = 0
while ($elapsed -lt $timeout) {
    Start-Sleep -Seconds 15
    $elapsed += 15
    $state = (Get-ScheduledTask -TaskName $taskName).State
    if ($state -ne 'Running') { break }
}

if ($elapsed -ge $timeout) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Write-Output "WARNING: Windows Update apply timed out after 60 minutes."
}

$checkAction = New-ScheduledTaskAction -Execute $wscript -Argument ('//nologo //B "' + $vbs + '" "' + $wuScript + '" Check')
Set-ScheduledTask -TaskName $taskName -Action $checkAction | Out-Null

if (Test-Path $outputFile) {
    Get-Content $outputFile -Raw
} else {
    Write-Output "No output from Windows Update apply."
}

$taskInfo = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction SilentlyContinue
$exitCode = if ($taskInfo) { $taskInfo.LastTaskResult } else { -1 }
Write-Output "WINUPDATE_APPLY_EXIT: $exitCode"
