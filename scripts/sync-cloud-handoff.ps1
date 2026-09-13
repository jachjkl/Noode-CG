param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[A-Fa-f0-9]{64}$")]
    [string]$ExpectedSha256,
    [string]$Repository = "jachjkl/Noode-CG",
    [string]$Branch = "main",
    [string]$Destination = "data/handoff/cloud-raw10000.json.gz"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$python = if ($env:NOODE_PYTHON) { $env:NOODE_PYTHON } else { "python" }
& $python -u (Join-Path $PSScriptRoot "sync_cloud_handoff.py") --sha256 $ExpectedSha256 --repository $Repository --ref $Branch --destination $Destination
if ($LASTEXITCODE -ne 0) { throw "云端候选下载失败，请查看下载切换日志。" }
