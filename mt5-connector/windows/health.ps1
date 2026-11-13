<#
.SYNOPSIS  Local health check of the MT5 connector: service state, MT5 package/terminal, journal, signed /health call.
#>
param([string] $InstallDir = "C:\RRConnector")
$ErrorActionPreference = "Continue"
$vpy = "$InstallDir\venv\Scripts\python.exe"
$svc = Get-Service RRMT5Connector -ErrorAction SilentlyContinue
Write-Host ("Service:        " + ($(if ($svc) { $svc.Status } else { "NOT INSTALLED" })))
$code = @'
import os, sys, json, time, ssl, urllib.request, keyring
sys.path.insert(0, r"__DIR__")
from dotenv_loader import load_env; load_env(r"__DIR__\connector.env")
sys.path.insert(0, r"__DIR__\..")
import hashlib, hmac, base64, secrets
secret = keyring.get_password("rr-mt5-connector", "bridge_secret")
ts, nonce = str(int(time.time())), secrets.token_hex(16)
msg = f"GET\n/health\n{ts}\n{nonce}\n{hashlib.sha256(b'').hexdigest()}".encode()
sig = base64.b64encode(hmac.new(secret.encode(), msg, hashlib.sha256).digest()).decode()
ctx = ssl.create_default_context(cafile=os.environ["RR_TLS_CERT"]); ctx.check_hostname = False
req = urllib.request.Request(f"https://127.0.0.1:{os.environ.get('RR_PORT','8443')}/health",
                             headers={"X-RR-Timestamp": ts, "X-RR-Nonce": nonce, "X-RR-Signature": sig})
print(json.dumps(json.loads(urllib.request.urlopen(req, context=ctx, timeout=10).read()), indent=1))
'@.Replace("__DIR__", $InstallDir)
Write-Host "Signed /health (requires RR_ALLOWED_IPS to include 127.0.0.1 for local checks):"
& $vpy -c $code
Write-Host "Recent log:"
Get-Content "$InstallDir\logs\connector.log" -Tail 15 -ErrorAction SilentlyContinue
