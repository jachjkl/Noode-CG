param(
    [string]$LocalRoot = "D:\桌面\软件\Noode-CG-Local"
)

$ErrorActionPreference = "Stop"
$createdNew = $false
$mutex = [System.Threading.Mutex]::new(
    $true,
    "Local\NoodeCGNotificationWatcher",
    [ref]$createdNew
)
if (-not $createdNew) { exit 0 }

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$queue = Join-Path $LocalRoot "notifications"
New-Item -ItemType Directory -Path $queue -Force | Out-Null
$icon = New-Object System.Windows.Forms.NotifyIcon
$icon.Icon = [System.Drawing.SystemIcons]::Information
$icon.Text = "Noode-CG 本地优选"
$icon.Visible = $true
try {
    while ($true) {
        $requests = @(Get-ChildItem -LiteralPath $queue -Filter "*.json" -File -ErrorAction SilentlyContinue |
            Sort-Object Name)
        foreach ($request in $requests) {
            try {
                $payload = Get-Content -Raw -Encoding UTF8 -LiteralPath $request.FullName |
                    ConvertFrom-Json
                $icon.BalloonTipTitle = [string]$payload.title
                $icon.BalloonTipText = [string]$payload.message
                $icon.BalloonTipIcon = switch ([string]$payload.level) {
                    "Warning" { [System.Windows.Forms.ToolTipIcon]::Warning }
                    "Error" { [System.Windows.Forms.ToolTipIcon]::Error }
                    default { [System.Windows.Forms.ToolTipIcon]::Info }
                }
                $icon.ShowBalloonTip(10000)
                Start-Sleep -Milliseconds 800
            }
            catch {
                Write-Warning "忽略损坏的通知请求 $($request.Name): $($_.Exception.Message)"
            }
            finally {
                Remove-Item -LiteralPath $request.FullName -Force -ErrorAction SilentlyContinue
            }
        }
        Start-Sleep -Seconds 2
    }
}
finally {
    $icon.Visible = $false
    $icon.Dispose()
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
