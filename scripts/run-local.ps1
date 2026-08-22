param(
    [string]$Config = "config.yaml"
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPath = Join-Path $RepoRoot ".venv"
$PythonPath = Join-Path $VenvPath "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $PythonPath)) {
    python -m venv $VenvPath
}

& $PythonPath -m pip install --disable-pip-version-check -r (Join-Path $RepoRoot "requirements.txt")
& $PythonPath (Join-Path $RepoRoot "main.py") --config (Join-Path $RepoRoot $Config) validate
& $PythonPath (Join-Path $RepoRoot "main.py") --config (Join-Path $RepoRoot $Config) run

