param(
    [int]$MaxWaitSeconds = 1800,
    [int]$PollSeconds = 15
)

$ErrorActionPreference = "Stop"
$notifier = Join-Path $PSScriptRoot "notify-user.ps1"

function Send-NoodeNotification {
    param([string]$Title, [string]$Message, [string]$Level = "Info")
    if (Test-Path -LiteralPath $notifier) {
        try {
            & $notifier -Title $Title -Message $Message -Level $Level
        }
        catch {
            Write-Warning "Windows 通知发送失败: $($_.Exception.Message)"
        }
    }
}

function Get-ProxyReasons {
    $reasons = @()
    $proxyVariables = @(
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy"
    )
    $activeVariables = @(
        $proxyVariables | Where-Object {
            -not [string]::IsNullOrWhiteSpace(
                [Environment]::GetEnvironmentVariable($_, "Process")
            )
        }
    )
    if ($activeVariables.Count -gt 0) {
        $reasons += "代理环境变量: $($activeVariables -join ', ')"
    }

    $internetSettings = Get-ItemProperty `
        -LiteralPath "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" `
        -ErrorAction SilentlyContinue
    if ($null -ne $internetSettings -and [int]$internetSettings.ProxyEnable -eq 1) {
        $reasons += "Windows 系统代理"
    }

    # A self-hosted runner may be a service whose HKCU is not the interactive
    # desktop user. Read that user's Internet Settings as well.
    try {
        $interactiveUser = (Get-CimInstance Win32_ComputerSystem -ErrorAction Stop).UserName
        if ($interactiveUser) {
            $account = [Security.Principal.NTAccount]::new($interactiveUser)
            $sid = $account.Translate([Security.Principal.SecurityIdentifier]).Value
            $userSettings = Get-ItemProperty `
                -LiteralPath "Registry::HKEY_USERS\$sid\Software\Microsoft\Windows\CurrentVersion\Internet Settings" `
                -ErrorAction SilentlyContinue
            if ($null -ne $userSettings -and [int]$userSettings.ProxyEnable -eq 1 -and
                $reasons -notcontains "Windows 系统代理") {
                $reasons += "Windows 系统代理"
            }
        }
    }
    catch {
        Write-Verbose "无法读取交互用户代理设置: $($_.Exception.Message)"
    }

    $winHttp = (& netsh winhttp show proxy 2>$null | Out-String)
    if ($winHttp -match "(?im)^\s*(Proxy Server|代理服务器)\s*:") {
        $reasons += "WinHTTP 代理"
    }

    $tunnel = Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object {
        $_.Status -eq "Up" -and
        ("$($_.Name) $($_.InterfaceDescription)" -match "(?i)(clash|mihomo|wintun|wireguard|tun adapter)")
    }
    if ($tunnel) {
        $reasons += "TUN/虚拟代理网卡: $($tunnel.Name -join ', ')"
    }
    return $reasons
}

$started = [DateTime]::UtcNow
$lastNotification = [DateTime]::MinValue
$wasBlocked = $false
while ($true) {
    $reasons = @(Get-ProxyReasons)
    if ($reasons.Count -eq 0) { break }
    $wasBlocked = $true
    $elapsed = ([DateTime]::UtcNow - $started).TotalSeconds
    if ($elapsed -ge $MaxWaitSeconds) {
        Send-NoodeNotification -Title "Noode-CG 优选暂停" `
            -Message "等待关闭代理超时，本次保留历史订阅。" -Level "Error"
        throw "等待关闭代理超时: $($reasons -join '；')"
    }
    if (([DateTime]::UtcNow - $lastNotification).TotalSeconds -ge 300) {
        Send-NoodeNotification -Title "Noode-CG 即将优选" `
            -Message "请关闭代理，即将开始优选。检测到：$($reasons -join '；')" `
            -Level "Warning"
        $lastNotification = [DateTime]::UtcNow
    }
    Write-Host "检测到代理，等待关闭：$($reasons -join '；')"
    Start-Sleep -Seconds $PollSeconds
}

if ($wasBlocked) {
    Send-NoodeNotification -Title "Noode-CG" -Message "代理已关闭，开始本地优选。"
}
else {
    Send-NoodeNotification -Title "Noode-CG" -Message "即将开始本地直连优选。"
}
Write-Host "未检测到系统代理或常见 TUN 网卡，开始本地直连测试。"
