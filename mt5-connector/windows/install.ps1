#Requires -RunAsAdministrator
<#
.SYNOPSIS  Install the RR Trading Engine MT5 connector as a Windows service.
.EXAMPLE   .\install.ps1 -LinuxServerIp 10.0.0.10 -ConnectorHost 10.0.0.20 -Mt5Login 12345678 -Mt5Server "Broker-Demo" `
             -Mt5TerminalPath "C:\Program Files\MetaTrader 5\terminal64.exe" -ServiceUser ".\rrsvc"
Secrets (MT5 password, bridge shared secret) are prompted and stored in Windows Credential Manager (DPAPI) —
never written to disk in plaintext.
#>
param(
  [Parameter(Mandatory)] [string] $LinuxServerIp,
  [Parameter(Mandatory)] [string] $ConnectorHost,
  [Parameter(Mandatory)] [long]   $Mt5Login,
  [Parameter(Mandatory)] [string] $Mt5Server,
  [string] $Mt5TerminalPath = "",
  [int]    $Port = 8443,
  [string] $ServiceUser = ".\rrsvc",
  [string] $InstallDir = "C:\RRConnector"
)
$ErrorActionPreference = "Stop"
$src = Split-Path -Parent $PSScriptRoot

Write-Host "==> Verifying Python (64-bit, >= 3.10)"
if (Get-Command py -ErrorAction SilentlyContinue) { $py = "py -3" } else { $py = "python" }
$ver = & cmd /c "$py -c ""import sys,struct;print(sys.version_info[0],sys.version_info[1],struct.calcsize('P')*8)"""
$parts = $ver.Split(" ")
if ([int]$parts[0] -lt 3 -or [int]$parts[1] -lt 10 -or [int]$parts[2] -ne 64) { throw "64-bit Python >= 3.10 required (found $ver)" }

Write-Host "==> Copying connector to $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir, "$InstallDir\logs", "$InstallDir\tls" | Out-Null
Copy-Item -Recurse -Force "$src\connector", "$src\windows", "$src\run.py", "$src\dotenv_loader.py", "$src\requirements.txt" $InstallDir

Write-Host "==> Creating virtual environment"
& cmd /c "$py -m venv `"$InstallDir\venv`""
$vpy = "$InstallDir\venv\Scripts\python.exe"
& $vpy -m pip install --upgrade pip | Out-Null
& $vpy -m pip install -r "$InstallDir\requirements.txt" cryptography
& $vpy -c "import MetaTrader5 as mt5; print('MetaTrader5 package', mt5.__version__)"
& $vpy "$InstallDir\venv\Scripts\pywin32_postinstall.py" -install | Out-Null

Write-Host "==> Storing secrets in Windows Credential Manager"
$mt5pw = Read-Host "MT5 account password" -AsSecureString
$bridge = Read-Host "Bridge shared secret (RR_BRIDGE_SHARED_SECRET from the Linux .env)" -AsSecureString
function Plain($s) { [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)) }
$env:RR_TMP_PW = Plain $mt5pw; $env:RR_TMP_SECRET = Plain $bridge
& $vpy -c "import keyring,os; keyring.set_password('rr-mt5-connector','mt5_password',os.environ['RR_TMP_PW']); keyring.set_password('rr-mt5-connector','bridge_secret',os.environ['RR_TMP_SECRET'])"
Remove-Item Env:RR_TMP_PW, Env:RR_TMP_SECRET
if ((Plain $bridge).Length -lt 32) { throw "Bridge secret must be at least 32 characters" }

Write-Host "==> TLS certificate"
if (-not (Test-Path "$InstallDir\tls\connector.crt")) { & $vpy "$InstallDir\windows\gen_cert.py" "$InstallDir\tls" $ConnectorHost }
Write-Host "    Copy $InstallDir\tls\connector.crt to the Linux server as secrets/connector-ca.pem"

Write-Host "==> Writing non-secret configuration"
@"
RR_MT5_LOGIN=$Mt5Login
RR_MT5_SERVER=$Mt5Server
RR_MT5_TERMINAL_PATH=$Mt5TerminalPath
RR_ALLOWED_IPS=$LinuxServerIp/32
RR_BIND=0.0.0.0
RR_PORT=$Port
RR_TLS_CERT=$InstallDir\tls\connector.crt
RR_TLS_KEY=$InstallDir\tls\connector.key
RR_JOURNAL_PATH=$InstallDir\journal.sqlite3
RR_LOG_FILE=$InstallDir\logs\connector.log
"@ | Set-Content -Encoding UTF8 "$InstallDir\connector.env"
icacls "$InstallDir\tls" /inheritance:r /grant:r "Administrators:(OI)(CI)F" "${ServiceUser}:(OI)(CI)R" | Out-Null

Write-Host "==> Firewall: only the Linux server may reach port $Port"
Get-NetFirewallRule -DisplayName "RR MT5 Connector" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "RR MT5 Connector" -Direction Inbound -Protocol TCP -LocalPort $Port -RemoteAddress $LinuxServerIp -Action Allow | Out-Null

Write-Host "==> Installing Windows service (runs as $ServiceUser, automatic start, restart on failure)"
$svcPw = Read-Host "Windows password for service account $ServiceUser (needs 'Log on as a service')" -AsSecureString
& $vpy "$InstallDir\windows\service.py" --startup auto install
# Set the run-as account via CIM so the password never appears on a command line
$cim = Get-CimInstance Win32_Service -Filter "Name='RRMT5Connector'"
$r = Invoke-CimMethod -InputObject $cim -MethodName Change -Arguments @{ StartName = $ServiceUser; StartPassword = (Plain $svcPw) }
if ($r.ReturnValue -ne 0) { throw "Setting service account failed (code $($r.ReturnValue))" }
sc.exe failure RRMT5Connector reset= 86400 actions= restart/5000/restart/15000/restart/60000 | Out-Null
sc.exe config RRMT5Connector depend= Tcpip | Out-Null
Start-Service RRMT5Connector
Start-Sleep 5
Get-Service RRMT5Connector
Write-Host "Done. Run .\health.ps1 to verify. Keep the MT5 terminal logged in with 'Algo Trading' enabled."
