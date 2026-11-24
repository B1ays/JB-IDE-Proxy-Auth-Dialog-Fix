$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    py -m venv .venv
}

$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $Python -m pip install --upgrade pip
& $Python -m pip install -r requirements.txt pyinstaller
& $Python -m PyInstaller `
    --onefile `
    --console `
    --clean `
    --name JetBrainsProxyPatcher `
    --collect-all textual `
    --collect-all rich `
    jetbrains_proxy.py

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

Write-Host "Built: $PSScriptRoot\dist\JetBrainsProxyPatcher.exe"
