param(
    [string]$Root = $PSScriptRoot,
    [int]$Port = 13336,
    [switch]$NoStart,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$resolvedRoot = [IO.Path]::GetFullPath($Root)
$logDirectory = Join-Path $resolvedRoot "logs"
$logPath = Join-Path $logDirectory "dashboard-launch.log"
$python = Join-Path $resolvedRoot "runtime\python\python.exe"
$dashboard = Join-Path $resolvedRoot "dashboard_server.py"
$url = "http://127.0.0.1:$Port/"
$stateUrl = "${url}api/state"

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

function Write-LaunchLog {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
    Write-Host $line
}

try {
    Write-LaunchLog "Launcher started. Root=$resolvedRoot"
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Bundled Python was not found: $python"
    }
    if (-not (Test-Path -LiteralPath $dashboard -PathType Leaf)) {
        throw "Dashboard server was not found: $dashboard"
    }

    try {
        $existing = Invoke-RestMethod -Uri $stateUrl -TimeoutSec 2
        if ($null -ne $existing) {
            Write-LaunchLog "Dashboard is already running. Opening $url"
            if (-not $NoBrowser) {
                Start-Process -FilePath $url | Out-Null
            }
            exit 0
        }
    }
    catch {
        Write-LaunchLog "No existing dashboard responded on port $Port. Starting a new server."
    }

    Write-LaunchLog "Starting dashboard server in this window. Selection waits for the Start button."
    $serverArguments = @(
        $dashboard,
        "--root", $resolvedRoot,
        "--port", $Port,
        "--repository", "jachjkl/Noode-CG",
        "--branch", "main",
        "--no-start"
    )
    if ($NoBrowser) {
        $serverArguments += "--no-browser"
    }
    & $python @serverArguments
    $exitCode = $LASTEXITCODE
    Write-LaunchLog "Dashboard server exited with code $exitCode."
    exit $exitCode
}
catch {
    Write-LaunchLog ("Launcher failed: " + $_.Exception.Message)
    Write-Host ""
    Write-Host "Noode-CG could not start. This window will stay open." -ForegroundColor Red
    Write-Host "Log: $logPath"
    exit 1
}
