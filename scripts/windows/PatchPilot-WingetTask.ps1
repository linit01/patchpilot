# PatchPilot — run winget from a scheduled task without a visible console.
# Win11 often flashes a conhost window when winget is invoked from PowerShell
# with only -WindowStyle Hidden; ProcessStartInfo.CreateNoWindow avoids that.
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

$winget = $null
try {
    $c = Get-Command winget -ErrorAction Stop
    if ($c.Source -and (Test-Path $c.Source)) { $winget = $c.Source }
} catch { }

if (-not $winget) {
    $fallback = Join-Path $env:LocalAppData 'Microsoft\WindowsApps\winget.exe'
    if (Test-Path $fallback) { $winget = $fallback }
}

if (-not $winget -or -not (Test-Path $winget)) {
    $outMissing = Join-Path $ppData 'winget-check.txt'
    'WINGET_NOT_FOUND' | Out-File $outMissing -Encoding utf8
    exit 0
}

if ($Action -eq 'Apply') {
    $outFile = Join-Path $ppData 'winget-apply.txt'
    $argLine = 'upgrade --all --include-unknown --accept-source-agreements --accept-package-agreements --silent --force'
} else {
    $outFile = Join-Path $ppData 'winget-check.txt'
    $argLine = 'upgrade --include-unknown --accept-source-agreements'
}

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $winget
$psi.Arguments = $argLine
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true

$p = New-Object System.Diagnostics.Process
$p.StartInfo = $psi
try {
    [void]$p.Start()
} catch {
    "WINGET_START_FAILED: $_" | Out-File $outFile -Encoding utf8
    exit 0
}

$p.WaitForExit()
$stdout = $p.StandardOutput.ReadToEnd()
$stderr = $p.StandardError.ReadToEnd()
($stdout + $stderr) | Out-File $outFile -Encoding utf8
