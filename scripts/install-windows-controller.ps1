param(
    [string]$InstallRoot = "D:\桌面\软件\Noode-CG-Local"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$source = Join-Path $repoRoot "windows-controller"
New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $InstallRoot "notifications") -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $source "notification-watcher.ps1") `
    -Destination (Join-Path $InstallRoot "notification-watcher.ps1") -Force
Copy-Item -LiteralPath (Join-Path $source "开始云端和本地优选.ps1") `
    -Destination (Join-Path $InstallRoot "开始云端和本地优选.ps1") -Force
Copy-Item -LiteralPath (Join-Path $source "开始云端和本地优选.cmd") `
    -Destination (Join-Path $InstallRoot "开始云端和本地优选.cmd") -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "notify-user.ps1") `
    -Destination (Join-Path $InstallRoot "notify-user.ps1") -Force

$startup = [Environment]::GetFolderPath("Startup")
$vbsPath = Join-Path $startup "Noode-CG-Notifier.vbs"
$watcher = Join-Path $InstallRoot "notification-watcher.ps1"
$command = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $watcher + '"'
$escapedCommand = $command.Replace('"', '""')
$vbsLines = @(
    'Set shell = CreateObject("WScript.Shell")',
    ('shell.Run "{0}", 0, False' -f $escapedCommand)
)
$vbsLines | Set-Content -LiteralPath $vbsPath -Encoding Unicode

Start-Process powershell.exe -ArgumentList @(
    "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
    "-File", ('"' + $watcher + '"')
) -WindowStyle Hidden
& (Join-Path $InstallRoot "notify-user.ps1") -Title "Noode-CG" `
    -Message "Windows 通知器和手动优选程序安装完成。" -LocalRoot $InstallRoot
Write-Host "已安装到 $InstallRoot"
Write-Host "手动入口: $(Join-Path $InstallRoot '开始云端和本地优选.cmd')"
