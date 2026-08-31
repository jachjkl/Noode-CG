param(
    [string]$RepositoryPath = "",
    [string]$CfDataExe = "D:\桌面\软件\cfdata-windows-amd64.exe",
    [string]$TaskName = "Noode-CG Local CFData Refresh",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "已删除计划任务: $TaskName"
    exit 0
}

if (-not $RepositoryPath) {
    $RepositoryPath = Split-Path -Parent $PSScriptRoot
}
$RepoRoot = (Resolve-Path -LiteralPath $RepositoryPath).Path
$CfDataPath = (Resolve-Path -LiteralPath $CfDataExe).Path
$Runner = Join-Path $RepoRoot "scripts\run-local-cfdata.ps1"
if (-not (Test-Path -LiteralPath $Runner -PathType Leaf)) {
    throw "找不到本地运行脚本: $Runner"
}

$PowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$arguments = @(
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy Bypass",
    "-File `"$Runner`"",
    "-RepositoryPath `"$RepoRoot`"",
    "-CfDataExe `"$CfDataPath`""
) -join " "

$action = New-ScheduledTaskAction -Execute $PowerShell -Argument $arguments -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6)
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "电脑登录并联网后运行 CFData，本地优选候选并推送到 Noode-CG。" `
    -Force | Out-Null

Write-Host "已安装计划任务: $TaskName"
Write-Host "任务会在当前用户登录并且网络可用后运行。"
Write-Host "可在任务计划程序中手动运行一次验证。"
