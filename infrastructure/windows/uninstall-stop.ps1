<#
  Run by the RR Trading One uninstaller before it removes files (desktop/src-tauri/windows/hooks.nsh).
  Stops what the app started, in the app's own stop order, and deletes nothing:
    1. the MT5 connector process launched from this install folder (graceful stop request, then forced after 20 s);
       a standalone RR MT5 Connector installed elsewhere is never touched;
    2. the engine containers: trading worker first, then the rest. Containers and volumes are stopped, not removed,
       so the database, cache, settings and connector data survive an uninstall or reinstall.
  Always exits 0: a stop failure must not block uninstalling.
#>
param([Parameter(Mandatory = $true)][string]$InstallDir)
$ErrorActionPreference = "Continue"
$InstallDir = $InstallDir.TrimEnd('\') + '\'

function Get-OurConnector {
  Get-Process -Name RR_MT5_Connector -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($InstallDir, [StringComparison]::OrdinalIgnoreCase) }
}

if (Get-OurConnector) {
  $connectorData = Join-Path $env:LOCALAPPDATA "RR Trading One\connector"
  if (Test-Path $connectorData) {
    New-Item -ItemType File -Force -Path (Join-Path $connectorData "stop.request") | Out-Null
  }
  $deadline = (Get-Date).AddSeconds(20)
  while ((Get-OurConnector) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
  Get-OurConnector | Stop-Process -Force -ErrorAction SilentlyContinue
}

$docker = (Get-Command docker -ErrorAction SilentlyContinue).Source
if (-not $docker) {
  $candidate = Join-Path $env:ProgramFiles "Docker\Docker\resources\bin\docker.exe"
  if (Test-Path $candidate) { $docker = $candidate }
}
if ($docker) {
  $project = "label=com.docker.compose.project=rr-trading-one"
  $worker = & $docker ps -q --filter $project --filter "label=com.docker.compose.service=trading-worker" 2>$null
  if ($worker) { & $docker stop --time 30 $worker 2>$null | Out-Null }
  $rest = & $docker ps -q --filter $project 2>$null
  if ($rest) { & $docker stop --time 30 $rest 2>$null | Out-Null }
}
exit 0
