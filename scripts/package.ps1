param(
    [string]$Destination = "Noode-CG.zip"
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$DestinationPath = [IO.Path]::GetFullPath((Join-Path $RepoRoot $Destination))
$StagingRoot = Join-Path ([IO.Path]::GetTempPath()) ("noode-cg-package-" + [Guid]::NewGuid().ToString("N"))
$StagingProject = Join-Path $StagingRoot "Noode-CG"

function Get-ProjectRelativePath {
    param([string]$FullPath)
    return $FullPath.Substring($RepoRoot.Length).TrimStart([char[]]@('\', '/'))
}

New-Item -ItemType Directory -Path $StagingProject -Force | Out-Null
try {
    $ExcludedDirectories = @(".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache", "checkpoints")
    Get-ChildItem -LiteralPath $RepoRoot -File -Recurse -Force |
        Where-Object {
            $Relative = Get-ProjectRelativePath -FullPath $_.FullName
            $Segments = $Relative -split '[\\/]'
            $_.Name -ne "Noode-CG.zip" -and
                $_.Extension -notin @(".pyc", ".pyo") -and
                -not ($Segments | Where-Object { $_ -in $ExcludedDirectories })
        } |
        ForEach-Object {
            $Relative = Get-ProjectRelativePath -FullPath $_.FullName
            $Target = Join-Path $StagingProject $Relative
            $TargetDirectory = Split-Path -Parent $Target
            New-Item -ItemType Directory -Path $TargetDirectory -Force | Out-Null
            Copy-Item -LiteralPath $_.FullName -Destination $Target -Force
        }
    if (Test-Path -LiteralPath $DestinationPath) {
        Remove-Item -LiteralPath $DestinationPath -Force
    }
    Compress-Archive -LiteralPath $StagingProject -DestinationPath $DestinationPath -CompressionLevel Optimal
    Write-Host "已生成 $DestinationPath"
}
finally {
    if (Test-Path -LiteralPath $StagingRoot) {
        Remove-Item -LiteralPath $StagingRoot -Recurse -Force
    }
}
