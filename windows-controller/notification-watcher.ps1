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

$queue = Join-Path $LocalRoot "notifications"
New-Item -ItemType Directory -Path $queue -Force | Out-Null

function Show-NativeToast {
    param([string]$Title, [string]$Message)
    [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
    [void][Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime]
    [void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    $safeTitle = [Security.SecurityElement]::Escape($Title)
    $safeMessage = [Security.SecurityElement]::Escape($Message)
    $xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$safeTitle</text><text>$safeMessage</text></binding></visual></toast>")
    $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Noode-CG").Show($toast)
}

try {
    while ($true) {
        $requests = @(Get-ChildItem -LiteralPath $queue -Filter "*.json" -File -ErrorAction SilentlyContinue |
            Sort-Object Name)
        foreach ($request in $requests) {
            try {
                $payload = Get-Content -Raw -Encoding UTF8 -LiteralPath $request.FullName |
                    ConvertFrom-Json
                Show-NativeToast -Title ([string]$payload.title) -Message ([string]$payload.message)
            }
            catch {
                & msg.exe $env:USERNAME "Noode-CG: 通知发送失败，请查看 GitHub Actions。" 2>$null
            }
            finally {
                Remove-Item -LiteralPath $request.FullName -Force -ErrorAction SilentlyContinue
            }
        }
        Start-Sleep -Seconds 2
    }
}
finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
