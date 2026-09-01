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
$workflow = "update.yml"

try { $Host.UI.RawUI.WindowTitle = "Noode-CG 手动优选 - 可在任务管理器结束" } catch { }

Set-Content -LiteralPath $log -Value "" -Encoding UTF8

function Write-Status {
    param([string]$Message, [ConsoleColor]$Color = [ConsoleColor]::Gray)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line -ForegroundColor $Color
    $line | Add-Content -LiteralPath $log -Encoding UTF8
}

function Notify {
    param([string]$Title, [string]$Message, [string]$Level = "Info")
    if (Test-Path -LiteralPath $notifier) {
        & $notifier -Title $Title -Message $Message -Level $Level -LocalRoot $LocalRoot |
            Add-Content -LiteralPath $log -Encoding UTF8
    }
}

function Get-Runs {
    $output = & $script:gh.Source run list --repo $Repository --workflow $workflow --limit 20 `
        --json databaseId,status,conclusion,url,createdAt,event 2>> $log
    if ($LASTEXITCODE -ne 0) { throw "无法读取 GitHub Actions 运行列表。" }
    if (-not $output) { return @() }
    return @($output | ConvertFrom-Json)
}

try {
    Write-Status "Noode-CG 手动控制器已启动。此窗口会显示状态，成功后自动关闭。" Cyan
    Write-Status "日志文件：$log"
    $script:gh = Get-Command gh -ErrorAction Stop
    & $script:gh.Source auth status *> $null
    if ($LASTEXITCODE -ne 0) { throw "GitHub CLI 尚未登录，请先运行 gh auth login。" }
    Write-Status "GitHub CLI 登录检查通过。" Green

    $runs = Get-Runs
    $active = $runs |
        Where-Object { $_.status -in @("queued", "in_progress", "waiting", "pending") } |
        Sort-Object createdAt -Descending |
        Select-Object -First 1

    if ($active) {
        $runId = [long]$active.databaseId
        $runUrl = [string]$active.url
        Write-Status "检测到已有任务正在运行，不重复触发；正在连接运行 #$runId。" Yellow
    }
    else {
        $knownIds = @($runs | ForEach-Object { [long]$_.databaseId })
        Notify -Title "Noode-CG" -Message "正在请求云端生成新的 TOP5000。"
        Write-Status "正在触发 GitHub Actions 云端 TOP5000 工作流……" Yellow
        & $script:gh.Source workflow run $workflow --repo $Repository --ref $Branch `
            -f continuation=false 2>&1 | Tee-Object -FilePath $log -Append
        if ($LASTEXITCODE -ne 0) { throw "云端工作流触发失败。" }

        $deadline = (Get-Date).AddSeconds(60)
        $newRun = $null
        do {
            Start-Sleep -Seconds 2
            $newRun = Get-Runs |
                Where-Object { [long]$_.databaseId -notin $knownIds -and $_.event -eq "workflow_dispatch" } |
                Sort-Object createdAt -Descending |
                Select-Object -First 1
        } while (-not $newRun -and (Get-Date) -lt $deadline)
        if (-not $newRun) { throw "命令已发送，但60秒内没有在 Actions 中找到新任务。" }
        $runId = [long]$newRun.databaseId
        $runUrl = [string]$newRun.url
        Write-Status "云端工作流已成功触发，运行编号 #$runId。" Green
        Notify -Title "Noode-CG" -Message "云端优选已启动，运行编号 #$runId。"
    }

    Write-Status "运行页面：$runUrl" Cyan
    Write-Status "下面持续显示各阶段状态；关闭此窗口不会停止 GitHub Actions。" DarkGray
    & $script:gh.Source run watch $runId --repo $Repository --compact --exit-status 2>&1 |
        Tee-Object -FilePath $log -Append
    if ($LASTEXITCODE -ne 0) {
        Write-Status "任务失败，正在下载详细日志……" Red
        & $script:gh.Source run view $runId --repo $Repository --log-failed 2>&1 |
            Tee-Object -FilePath $log -Append
        throw "GitHub Actions 运行失败，请查看上方错误或日志 $log"
    }

    Write-Status "本轮 GitHub Actions 已成功完成。" Green
    Write-Status "如果合格IP不足300，后续补池任务会由云端自动发起。" Yellow
    Notify -Title "Noode-CG" -Message "本轮云端和本地任务已成功完成。"
    Write-Status "窗口将在5秒后自动关闭。" DarkGray
    Start-Sleep -Seconds 5
    exit 0
}
catch {
    $message = $_.Exception.Message
    Write-Status "启动或运行失败：$message" Red
    $_ | Out-String | Add-Content -LiteralPath $log -Encoding UTF8
    Notify -Title "Noode-CG 启动失败" -Message $message -Level "Error"
    Write-Status "错误窗口会保留；按任意键后关闭。" Yellow
    exit 1
}
