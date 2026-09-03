param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Save", "Restore", "ClearPending", "ClearRuntime")]
    [string]$Mode,
    [string]$RepoRoot = $(Resolve-Path (Join-Path $PSScriptRoot "..")),
    [string]$LocalRoot = $(
        if ($env:NOODE_LOCAL_ROOT) { $env:NOODE_LOCAL_ROOT }
        else { "D:\桌面\软件\Noode-CG-Local" }
    )
)

$ErrorActionPreference = "Stop"
$repo = [IO.Path]::GetFullPath($RepoRoot)
$pending = [IO.Path]::GetFullPath((Join-Path $LocalRoot "pending-publish"))
$files = @(
    "output/nodes.txt",
    "output/nodes.json",
    "output/nodes.csv",
    "output/api.json",
    "output/health.json",
    "output/ip.zip",
    "data/previous-top100.json",
    "data/handoff/local-qualified.json.gz",
    "data/handoff/local-attempted-ips.txt.gz"
)

function Copy-RelativeFiles {
    param([string]$From, [string]$To)
    foreach ($relative in $files) {
        $source = Join-Path $From $relative
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { continue }
        $target = Join-Path $To $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
        Copy-Item -LiteralPath $source -Destination $target -Force
    }
}

switch ($Mode) {
    "Save" {
        if (Test-Path -LiteralPath $pending) {
            Remove-Item -LiteralPath $pending -Recurse -Force
        }
        New-Item -ItemType Directory -Path $pending -Force | Out-Null
        Copy-RelativeFiles -From $repo -To $pending
        $existing = @($files | Where-Object {
            Test-Path -LiteralPath (Join-Path $repo $_) -PathType Leaf
        })
        @{ saved_at = [DateTime]::UtcNow.ToString("o"); existing = $existing } |
            ConvertTo-Json | Set-Content -LiteralPath (Join-Path $pending "manifest.json") -Encoding UTF8
    }
    "Restore" {
        $manifestPath = Join-Path $pending "manifest.json"
        if (Test-Path -LiteralPath $manifestPath) {
            $manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath $manifestPath | ConvertFrom-Json
            $existing = @($manifest.existing)
            foreach ($relative in $files) {
                $target = Join-Path $repo $relative
                if ($existing -notcontains $relative -and (Test-Path -LiteralPath $target -PathType Leaf)) {
                    Remove-Item -LiteralPath $target -Force
                }
            }
            Copy-RelativeFiles -From $pending -To $repo
            Write-Host "已恢复上次尚未推送的本地结果。"
        }
    }
    "ClearPending" {
        if (Test-Path -LiteralPath $pending) {
            Remove-Item -LiteralPath $pending -Recurse -Force
        }
    }
    "ClearRuntime" {
        $runtimeTargets = @(
            "data/handoff/cloud-raw10000.json.gz",
            "data/handoff/cloud-top5000.json.gz",
            "data/previous-top100.json",
            "data/checkpoints",
            "output/nodes.txt",
            "output/nodes.json",
            "output/nodes.csv",
            "output/api.json",
            "output/health.json",
            "output/ip.zip",
            ".pytest_cache",
            ".ruff_cache"
        )
        foreach ($relative in $runtimeTargets) {
            $target = Join-Path $repo $relative
            if (Test-Path -LiteralPath $target) {
                Remove-Item -LiteralPath $target -Recurse -Force
            }
        }
        Get-ChildItem -LiteralPath $repo -Directory -Filter "__pycache__" -Recurse -ErrorAction SilentlyContinue |
            ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
        # Session logs are owned by the dashboard launcher/controller and are
        # retained with timestamped names. Runtime cleanup must never erase
        # diagnostic evidence from the just-completed selection.
    }
}
