#Requires -RunAsAdministrator
<# Independently upgrade the connector code. The execution journal, TLS material, configuration and
   Credential Manager secrets are preserved. Unresolved journal entries are reconciled on restart. #>
param([string] $InstallDir = "C:\RRConnector")
$ErrorActionPreference = "Stop"
$src = Split-Path -Parent $PSScriptRoot
$stamp = Get-Date -Format "yyyyMMddHHmmss"
Stop-Service RRMT5Connector
Copy-Item "$InstallDir\journal.sqlite3" "$InstallDir\journal.sqlite3.$stamp.bak" -ErrorAction SilentlyContinue
Copy-Item -Recurse -Force "$src\connector", "$src\windows", "$src\run.py", "$src\dotenv_loader.py", "$src\requirements.txt" $InstallDir
& "$InstallDir\venv\Scripts\python.exe" -m pip install -r "$InstallDir\requirements.txt"
Start-Service RRMT5Connector
Start-Sleep 5
Get-Service RRMT5Connector
