#Requires -RunAsAdministrator
<# Removes the service and firewall rule. The journal and logs are kept for audit unless -Purge is given.
   Open positions at the broker are NOT affected: their SL/TP live on the broker side. #>
param([string] $InstallDir = "C:\RRConnector", [switch] $Purge)
& "$InstallDir\venv\Scripts\python.exe" "$InstallDir\windows\service.py" stop 2>$null
& "$InstallDir\venv\Scripts\python.exe" "$InstallDir\windows\service.py" remove
Get-NetFirewallRule -DisplayName "RR MT5 Connector" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
if ($Purge) {
  & "$InstallDir\venv\Scripts\python.exe" -c "import keyring; [keyring.delete_password('rr-mt5-connector', k) for k in ('mt5_password','bridge_secret')]"
  Remove-Item -Recurse -Force $InstallDir
}
