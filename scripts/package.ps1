param(
    [string]$Destination = "Noode-CG-V13.3-PS51-Runner-Fix.zip"
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$DestinationPath = if ([IO.Path]::IsPathRooted($Destination)) {
    [IO.Path]::GetFullPath($Destination)
}
else {
    [IO.Path]::GetFullPath((Join-Path $RepoRoot $Destination))
}
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
                $_.FullName -ne $DestinationPath -and
                $Relative -notmatch '^[^\\/]+\.zip$' -and
                $Relative -notmatch '^data[\\/]input([\\/]|$)' -and
                $Relative -notmatch '^data[\\/]sources([\\/]|$)' -and
                $Relative -notmatch '^data[\\/]handoff([\\/]|$)' -and
                $Relative -notmatch '^data[\\/](previous-top100\.json|previous-official-ips\.txt(?:\.gz)?)$' -and
                $Relative -notmatch '^data[\\/]local-cfdata-candidates\.txt$' -and
                $Relative -notmatch '^data[\\/]local-cfdata-last\.log$' -and
                $Relative -notmatch '^output[\\/](nodes\.txt|nodes\.json|nodes\.csv|api\.json|health\.json|ip\.zip)$' -and
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
    $PackagedNodes = Join-Path $StagingProject "output\nodes.txt"
    if (Test-Path -LiteralPath $PackagedNodes) {
        $PackagedNodesContent = Get-Content -Raw -LiteralPath $PackagedNodes
        if ($null -ne $PackagedNodesContent -and $PackagedNodesContent.Trim()) {
            throw "拒绝打包：output/nodes.txt 不是空文件，压缩包不得携带候选 IP"
        }
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
