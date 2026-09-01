param(
    [Parameter(Mandatory = $true)]
    [string]$Title,
    [Parameter(Mandatory = $true)]
    [string]$Message,
    [ValidateSet("Info", "Warning", "Error")]
    [string]$Level = "Info",
    [string]$LocalRoot = $(
        if ($env:NOODE_LOCAL_ROOT) { $env:NOODE_LOCAL_ROOT }
        else { "D:\桌面\软件\Noode-CG-Local" }
    )
)

$ErrorActionPreference = "Stop"
$queue = Join-Path $LocalRoot "notifications"
New-Item -ItemType Directory -Path $queue -Force | Out-Null
$name = "{0}-{1}.json" -f [DateTime]::UtcNow.ToString("yyyyMMddHHmmssfff"), [Guid]::NewGuid().ToString("N")
$destination = Join-Path $queue $name
$temporary = "$destination.tmp"
@{
    title = $Title
    message = $Message
    level = $Level
    created_at = [DateTime]::UtcNow.ToString("o")
} | ConvertTo-Json -Compress | Set-Content -LiteralPath $temporary -Encoding UTF8
Move-Item -LiteralPath $temporary -Destination $destination -Force
Write-Host "Windows 通知已排队: $Title - $Message"
