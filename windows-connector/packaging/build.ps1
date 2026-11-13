<#
  Build RR_MT5_Connector.exe and RR_MT5_Connector_Setup.exe on Windows 10/11 (64-bit).
  Requirements: Python 3.12+ (64-bit) on PATH, Inno Setup 6 (iscc.exe) — installed automatically with winget if missing.
  Usage (from the repository root):   powershell -ExecutionPolicy Bypass -File windows-connector\packaging\build.ps1
#>
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$App  = Resolve-Path "$Here\.."
$Root = Resolve-Path "$App\.."
Set-Location $App

if (-not (Test-Path .venv)) { python -m venv .venv }
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

Write-Host "== tests"
.\.venv\Scripts\python.exe -m pytest -q tests
.\.venv\Scripts\python.exe -m pytest -q "$Root\mt5-connector\tests"

Write-Host "== resources"
.\.venv\Scripts\python.exe packaging\make_icon.py
$Version = (.\.venv\Scripts\python.exe packaging\version_info.py).Trim()

Write-Host "== PyInstaller ($Version)"
.\.venv\Scripts\pyinstaller.exe packaging\rr_mt5_connector.spec --noconfirm --clean --distpath dist --workpath build

Write-Host "== packaged self-test"
$Report = "$env:TEMP\rrmt5-selftest.json"
$p = Start-Process -FilePath "dist\RR_MT5_Connector\RR_MT5_Connector.exe" -ArgumentList "--selftest `"$Report`"" -Wait -PassThru
Get-Content $Report
if ($p.ExitCode -ne 0) { throw "Packaged self-test failed (exit $($p.ExitCode))" }

Write-Host "== installer"
$Iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
if (-not $Iscc) { $Iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" }
if (-not (Test-Path $Iscc)) {
  winget install --id JRSoftware.InnoSetup -e --silent --accept-package-agreements --accept-source-agreements
  $Iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
}
& $Iscc "/DAppVersion=$Version" packaging\installer.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }

$Setup = "dist\RR_MT5_Connector_Setup.exe"
$Hash = (Get-FileHash $Setup -Algorithm SHA256).Hash.ToLower()
"$Hash  RR_MT5_Connector_Setup.exe" | Out-File -Encoding ascii dist\SHA256SUMS.txt
Write-Host "Built $Setup  sha256=$Hash"
