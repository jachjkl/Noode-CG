param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[A-Fa-f0-9]{64}$")]
    [string]$ExpectedSha256,
    [string]$Repository = "jachjkl/Noode-CG",
    [string]$Branch = "main",
    [string]$Destination = "data/handoff/cloud-raw10000.json.gz"
)

$ErrorActionPreference = "Stop"
$expected = $ExpectedSha256.ToUpperInvariant()
$destinationPath = [IO.Path]::GetFullPath((Join-Path (Get-Location) $Destination))
$destinationDirectory = Split-Path -Parent $destinationPath
New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null

function Test-ExpectedHash {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash -eq $expected
}

if (Test-ExpectedHash -Path $destinationPath) {
    Write-Host "本地交接池哈希正确，无需重新下载。"
    exit 0
}

$raw = "https://raw.githubusercontent.com/$Repository/$Branch/data/handoff/cloud-raw10000.json.gz"
$urls = @(
    $raw,
    "https://gh-proxy.com/$raw",
    "https://ghfast.top/$raw",
    "https://gh.ddlc.top/$raw"
)
$failures = @()
foreach ($url in $urls) {
    $temporary = Join-Path $destinationDirectory (".cloud-raw10000-" + [Guid]::NewGuid().ToString("N") + ".tmp")
    try {
        Write-Host "尝试下载交接池: $url"
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $temporary -TimeoutSec 90
        if (-not (Test-ExpectedHash -Path $temporary)) {
            $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $temporary).Hash
            throw "SHA-256 不匹配，期望 $expected，实际 $actual"
        }
        Move-Item -LiteralPath $temporary -Destination $destinationPath -Force
        Write-Host "交接池下载并校验成功。"
        exit 0
    }
    catch {
        $failures += "$url -> $($_.Exception.Message)"
    }
    finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}
throw "所有直连/镜像地址均失败，未执行未校验数据。`n$($failures -join "`n")"
