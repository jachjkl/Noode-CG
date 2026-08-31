param(
    [string]$RepositoryPath = "",
    [string]$CfDataExe = "D:\桌面\软件\cfdata-windows-amd64.exe",
    [int]$CandidateTarget = 300,
    [double]$MinimumSpeedMBps = 0.125,
    [int]$DelayMilliseconds = 1000,
    [int]$Threads = 300,
    [switch]$SkipPush
)

$ErrorActionPreference = "Stop"
if (-not $RepositoryPath) {
    $RepositoryPath = Split-Path -Parent $PSScriptRoot
}
$RepoRoot = (Resolve-Path -LiteralPath $RepositoryPath).Path
$CfDataPath = (Resolve-Path -LiteralPath $CfDataExe).Path
$CandidatePath = Join-Path $RepoRoot "data\local-cfdata-candidates.txt"
$LogPath = Join-Path $RepoRoot "data\local-cfdata-last.log"
New-Item -ItemType Directory -Path (Split-Path -Parent $CandidatePath) -Force | Out-Null

if ($CandidateTarget -le 0) { throw "CandidateTarget 必须大于 0" }
if ($MinimumSpeedMBps -lt 0) { throw "MinimumSpeedMBps 不能小于 0" }
if ($DelayMilliseconds -le 0) { throw "DelayMilliseconds 必须大于 0" }
if ($Threads -le 0) { throw "Threads 必须大于 0" }

function Wait-ForNetwork {
    param([int]$MaximumAttempts = 60)
    for ($attempt = 1; $attempt -le $MaximumAttempts; $attempt++) {
        $connected = Test-NetConnection -ComputerName "github.com" -Port 443 `
            -InformationLevel Quiet -WarningAction SilentlyContinue
        if ($connected) { return }
        Start-Sleep -Seconds 10
    }
    throw "等待网络连接超时，未能连接 github.com:443"
}

function Get-RowValue {
    param($Row, [string[]]$Names)
    foreach ($name in $Names) {
        $property = $Row.PSObject.Properties[$name]
        if ($null -ne $property -and -not [string]::IsNullOrWhiteSpace([string]$property.Value)) {
            return [string]$property.Value
        }
    }
    return ""
}

function Parse-Number {
    param([string]$Text, [double]$Fallback)
    if ($Text -match '([0-9]+(?:\.[0-9]+)?)') {
        return [double]::Parse($Matches[1], [Globalization.CultureInfo]::InvariantCulture)
    }
    return $Fallback
}

Wait-ForNetwork

if (-not $SkipPush) {
    git -C $RepoRoot rev-parse --is-inside-work-tree | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "RepositoryPath 不是 Git 仓库，请先使用 git clone 下载 Noode-CG"
    }
    git -C $RepoRoot pull --ff-only origin main
    if ($LASTEXITCODE -ne 0) {
        throw "无法 fast-forward 到远程 main；请先处理本地未提交修改或分支差异"
    }
}

$RunRoot = Join-Path ([IO.Path]::GetTempPath()) ("noode-cg-cfdata-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $RunRoot -Force | Out-Null
try {
    $CsvName = "cfdata-local.csv"
    $arguments = @(
        "-cli",
        "-skipgeo",
        "-mode=official",
        "-scanmode=tcping",
        "-offiptype=4",
        "-offthreads=$Threads",
        "-offport=443",
        "-offdelay=$DelayMilliseconds",
        "-offspeedlimit=$CandidateTarget",
        "-offspeedmin=$MinimumSpeedMBps",
        "-offurl=auto",
        "-offout=$CsvName",
        "-format=csv",
        "-fields=ip,port,latency,speed,dc,dcCountry",
        "-github=false",
        "-progress=true",
        "-nocolor=true"
    )

    $proxyNames = @("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    $savedProxyValues = @{}
    foreach ($name in $proxyNames) {
        $savedProxyValues[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
        [Environment]::SetEnvironmentVariable($name, $null, "Process")
    }
    try {
        Push-Location $RunRoot
        try {
            & $CfDataPath @arguments 2>&1 | Tee-Object -FilePath $LogPath
            $cfdataExitCode = $LASTEXITCODE
        }
        finally {
            Pop-Location
        }
    }
    finally {
        foreach ($name in $proxyNames) {
            [Environment]::SetEnvironmentVariable($name, $savedProxyValues[$name], "Process")
        }
    }
    if ($cfdataExitCode -ne 0) {
        throw "CFData 运行失败，退出码 $cfdataExitCode；请检查 $LogPath"
    }

    $CsvPath = Join-Path $RunRoot $CsvName
    if (-not (Test-Path -LiteralPath $CsvPath -PathType Leaf)) {
        throw "CFData 没有生成 $CsvName；请检查 $LogPath"
    }

    $ranked = foreach ($row in (Import-Csv -LiteralPath $CsvPath -Encoding UTF8)) {
        $ip = Get-RowValue -Row $row -Names @("IP地址", "IP", "ip")
        $portText = Get-RowValue -Row $row -Names @("端口号", "Port", "port")
        $speedText = Get-RowValue -Row $row -Names @("下载速度", "Speed", "speed")
        $latencyText = Get-RowValue -Row $row -Names @("网络延迟", "Latency", "latency")
        $parsedAddress = $null
        if (-not [Net.IPAddress]::TryParse($ip, [ref]$parsedAddress)) { continue }
        $port = 0
        if (-not [int]::TryParse($portText, [ref]$port) -or $port -lt 1 -or $port -gt 65535) { continue }
        $speed = Parse-Number -Text $speedText -Fallback -1
        if ($speed -lt $MinimumSpeedMBps) { continue }
        [pscustomobject]@{
            IP = $parsedAddress.ToString()
            Port = $port
            Speed = $speed
            Latency = Parse-Number -Text $latencyText -Fallback ([double]::PositiveInfinity)
        }
    }

    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $lines = @(
        $ranked |
            Sort-Object @{ Expression = "Speed"; Descending = $true }, @{ Expression = "Latency"; Ascending = $true } |
            Where-Object { $seen.Add($_.IP) } |
            Select-Object -First $CandidateTarget |
            ForEach-Object { "$($_.IP):$($_.Port)" }
    )
    if ($lines.Count -eq 0) {
        throw "CFData 没有产生达到 $MinimumSpeedMBps MB/s 的候选；保留仓库中的上一版候选"
    }

    $CandidateDirectory = Split-Path -Parent $CandidatePath
    New-Item -ItemType Directory -Path $CandidateDirectory -Force | Out-Null
    $TemporaryCandidate = Join-Path $CandidateDirectory (".local-cfdata-" + [Guid]::NewGuid().ToString("N") + ".tmp")
    [IO.File]::WriteAllLines($TemporaryCandidate, $lines, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $TemporaryCandidate -Destination $CandidatePath -Force
    Write-Host "已生成 $($lines.Count) 条本地 CFData 候选: $CandidatePath"
}
finally {
    if (Test-Path -LiteralPath $RunRoot) {
        Remove-Item -LiteralPath $RunRoot -Recurse -Force
    }
}

if ($SkipPush) {
    Write-Host "SkipPush 已启用，没有提交到 GitHub"
    exit 0
}

git -C $RepoRoot add -- data/local-cfdata-candidates.txt
git -C $RepoRoot diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Host "本地 CFData 候选没有变化，无需提交"
    exit 0
}
git -C $RepoRoot commit -m "chore(data): refresh local CFData candidates"
if ($LASTEXITCODE -ne 0) { throw "提交本地 CFData 候选失败" }
git -C $RepoRoot push origin main
if ($LASTEXITCODE -ne 0) {
    git -C $RepoRoot pull --rebase origin main
    if ($LASTEXITCODE -ne 0) { throw "远程仓库已经更新，自动 rebase 失败" }
    git -C $RepoRoot push origin main
    if ($LASTEXITCODE -ne 0) { throw "推送本地 CFData 候选失败" }
}
Write-Host "本地候选已推送；GitHub Actions 将自动优选 300 个严格非 JP 地址和 JP10"
