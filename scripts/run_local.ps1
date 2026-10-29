# Set up and start the ScaleTrace web app locally (Windows PowerShell).
#
#   powershell -ExecutionPolicy Bypass -File scripts\run_local.ps1              # then open http://127.0.0.1:8000
#   powershell -ExecutionPolicy Bypass -File scripts\run_local.ps1 -Port 8010   # use another port
#
# Creates .venv (if missing), installs ScaleTrace with the web API, builds the frontend and starts the server
# on 127.0.0.1 only. Safe to run again: it reuses .venv and reinstalls quickly.
param(
    [int]$Port = 8000,
    [string]$Python = ""
)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

function Fail($message) {
    Write-Host $message -ForegroundColor Red
    exit 1
}

if (-not $Python) {
    if (Get-Command py -ErrorAction SilentlyContinue) { $Python = "py" }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { $Python = "python" }
    else { Fail "Python 3.10 or newer was not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH')." }
}
& $Python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) { Fail "ScaleTrace needs Python 3.10 or newer." }
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    Fail "Node.js 18 or newer (with npm) was not found. Install it from https://nodejs.org."
}

if (-not (Test-Path ".venv")) {
    Write-Host "Creating the virtual environment in .venv ..."
    & $Python -m venv .venv
    if ($LASTEXITCODE -ne 0) { Fail "Could not create .venv." }
}
$VenvPython = Join-Path (Get-Location) ".venv\Scripts\python.exe"

Write-Host "Installing ScaleTrace and the web API ..."
& $VenvPython -m pip install --quiet --upgrade pip
& $VenvPython -m pip install --quiet -e ".[api]"
if ($LASTEXITCODE -ne 0) { Fail "pip install failed." }

Write-Host "Building the frontend ..."
npm --prefix frontend ci --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { Fail "npm ci failed." }
npm --prefix frontend run build
if ($LASTEXITCODE -ne 0) { Fail "The frontend build failed." }

Write-Host ""
Write-Host "ScaleTrace is running at http://127.0.0.1:$Port  (press Ctrl+C to stop)"
& $VenvPython -m scaletrace.api --host 127.0.0.1 --port $Port
