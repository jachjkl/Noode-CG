param(
    [string]$Repository = "jachjkl/Noode-CG",
    [string]$Branch = "main",
    [string]$LocalRoot = "D:\桌面\软件\Noode-CG-Local"
)

$ErrorActionPreference = "Stop"
$logDirectory = Join-Path $LocalRoot "logs"
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$log = Join-Path $logDirectory "manual-last.log"
$notifier = Join-Path $LocalRoot "notify-user.ps1"

function Notify {
    param([string]$Title, [string]$Message, [string]$Level = "Info")
    if (Test-Path -LiteralPath $notifier) {
        & $notifier -Title $Title -Message $Message -Level $Level -LocalRoot $LocalRoot
    }
}

try {
    $gh = Get-Command gh -ErrorAction Stop
    & $gh.Source auth status *> $null
    if ($LASTEXITCODE -ne 0) { throw "GitHub CLI 尚未登录，请先运行 gh auth login。" }
    Notify -Title "Noode-CG" -Message "正在请求云端生成新的 TOP5000。"
    "gh workflow run update.yml --repo $Repository --ref $Branch -f continuation=false" |
        Add-Content -LiteralPath $log -Encoding UTF8
    & $gh.Source workflow run update.yml --repo $Repository --ref $Branch `
        -f continuation=false *>> $log
    if ($LASTEXITCODE -ne 0) { throw "云端工作流触发失败，请查看 $log" }
    Notify -Title "Noode-CG" `
        -Message "云端优选已启动；完成后本机会自动开始，不足300会继续补池。"
}
catch {
    $_ | Out-String | Add-Content -LiteralPath $log -Encoding UTF8
    Notify -Title "Noode-CG 启动失败" -Message $_.Exception.Message -Level "Error"
    exit 1
}
