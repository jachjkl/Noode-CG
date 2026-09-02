param(
    [string]$InstallRoot = "D:\桌面\软件\Noode-CG-Local"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$source = Join-Path $repoRoot "windows-controller"
New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $InstallRoot "notifications") -Force | Out-Null

function Assert-ChildPath {
    param([string]$ParentPath, [string]$ChildPath)
    $parent = [IO.Path]::GetFullPath($ParentPath).TrimEnd([char[]]@('\', '/'))
    $child = [IO.Path]::GetFullPath($ChildPath)
    if (-not $child.StartsWith($parent + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe runtime path outside installation root: $child"
    }
}

function Prepare-RunnerPython {
    $pythonCommand = Get-Command python.exe -ErrorAction Stop
    $probe = & $pythonCommand.Source -c "import json,sys; print(json.dumps({'base':sys.base_prefix,'major':sys.version_info.major,'minor':sys.version_info.minor}))"
    if ($LASTEXITCODE -ne 0 -or -not $probe) {
        throw "无法启动本机 Python。请先安装 Python 3.11 或更高版本。"
    }
    $pythonInfo = (($probe | Out-String) | ConvertFrom-Json)
    if ([int]$pythonInfo.major -ne 3 -or [int]$pythonInfo.minor -lt 11) {
        throw "需要 Python 3.11 或更高版本，当前为 $($pythonInfo.major).$($pythonInfo.minor)。"
    }

    $sourceRoot = [IO.Path]::GetFullPath([string]$pythonInfo.base)
    $runtimeRoot = Join-Path $InstallRoot "runtime"
    $targetRoot = Join-Path $runtimeRoot "python"
    $stagingRoot = Join-Path $runtimeRoot ("python-staging-" + [Guid]::NewGuid().ToString("N"))
    Assert-ChildPath -ParentPath $InstallRoot -ChildPath $targetRoot
    Assert-ChildPath -ParentPath $InstallRoot -ChildPath $stagingRoot
    New-Item -ItemType Directory -Path $stagingRoot -Force | Out-Null

    try {
        Write-Host "正在准备 Runner 专用 Python：$targetRoot"
        & robocopy.exe $sourceRoot $stagingRoot /E /R:2 /W:1 /NFL /NDL /NJH /NJS /NP `
            /XD (Join-Path $sourceRoot "Doc") (Join-Path $sourceRoot "tcl") `
                (Join-Path $sourceRoot "include") (Join-Path $sourceRoot "libs") `
                (Join-Path $sourceRoot "Scripts") (Join-Path $sourceRoot "Lib\site-packages") `
                "__pycache__" `
            /XF "*.pyc" "*.pyo"
        if ($LASTEXITCODE -ge 8) {
            throw "复制 Runner Python 失败，robocopy 退出码 $LASTEXITCODE。"
        }

        $stagingPython = Join-Path $stagingRoot "python.exe"
        & $stagingPython -m ensurepip --upgrade
        if ($LASTEXITCODE -ne 0) { throw "Runner Python ensurepip 失败。" }
        & $stagingPython -m pip install --disable-pip-version-check -r (Join-Path $repoRoot "requirements.txt")
        if ($LASTEXITCODE -ne 0) { throw "Runner Python 依赖安装失败。" }
        & $stagingPython -c "import yaml; print('Runner Python ready: PyYAML ' + yaml.__version__)"
        if ($LASTEXITCODE -ne 0) { throw "Runner Python 自检失败。" }

        if (Test-Path -LiteralPath $targetRoot) {
            $resolvedInstall = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([char[]]@('\', '/'))
            $resolvedTarget = [IO.Path]::GetFullPath($targetRoot)
            if (-not $resolvedTarget.StartsWith($resolvedInstall + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
                throw "拒绝删除安装目录之外的旧运行环境：$resolvedTarget"
            }
            Remove-Item -LiteralPath $resolvedTarget -Recurse -Force
        }
        Move-Item -LiteralPath $stagingRoot -Destination $targetRoot

        # S-1-5-20 is NT AUTHORITY\NETWORK SERVICE on every Windows locale.
        & icacls.exe $InstallRoot /grant "*S-1-5-20:(OI)(CI)(M)" /T /C /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "无法授予 NETWORK SERVICE 使用 Runner 运行环境的权限。" }
    }
    finally {
        if (Test-Path -LiteralPath $stagingRoot) {
            Assert-ChildPath -ParentPath $InstallRoot -ChildPath $stagingRoot
            Remove-Item -LiteralPath $stagingRoot -Recurse -Force
        }
    }
}

function Install-LocalApplication {
    $appRoot = Join-Path $InstallRoot "app"
    $stagingRoot = Join-Path $InstallRoot ("app-staging-" + [Guid]::NewGuid().ToString("N"))
    Assert-ChildPath -ParentPath $InstallRoot -ChildPath $appRoot
    Assert-ChildPath -ParentPath $InstallRoot -ChildPath $stagingRoot
    New-Item -ItemType Directory -Path $stagingRoot -Force | Out-Null
    try {
        Write-Host "正在安装不依赖 GitHub checkout 的本地应用：$appRoot"
        & robocopy.exe $repoRoot $stagingRoot /E /R:2 /W:1 /NFL /NDL /NJH /NJS /NP `
            /XD (Join-Path $repoRoot ".git") (Join-Path $repoRoot ".venv") `
                (Join-Path $repoRoot "venv") (Join-Path $repoRoot "__pycache__") `
                (Join-Path $repoRoot ".pytest_cache") (Join-Path $repoRoot ".ruff_cache") `
                (Join-Path $repoRoot "data\checkpoints") `
            /XF "*.pyc" "*.pyo" "Noode-CG-*.zip" "Noode-CG.zip" `
                "nodes.txt" "nodes.json" "nodes.csv" "api.json" "health.json" "ip.zip" `
                "previous-top100.json" "previous-official-ips.txt" "previous-official-ips.txt.gz" `
                "cloud-raw10000.json.gz" "cloud-top5000.json.gz" "cloud-health.json" `
                "local-qualified.json.gz" "local-attempted-ips.txt.gz"
        if ($LASTEXITCODE -ge 8) {
            throw "复制本地应用失败，robocopy 退出码 $LASTEXITCODE。"
        }
        $existingRules = Join-Path $appRoot "data\local-rules.json"
        if (Test-Path -LiteralPath $existingRules -PathType Leaf) {
            $stagedRules = Join-Path $stagingRoot "data\local-rules.json"
            New-Item -ItemType Directory -Path (Split-Path -Parent $stagedRules) -Force | Out-Null
            Copy-Item -LiteralPath $existingRules -Destination $stagedRules -Force
            Write-Host "已保留本机自定义优选规则。"
        }
        $versionPath = Join-Path $stagingRoot "VERSION"
        if (-not (Test-Path -LiteralPath $versionPath -PathType Leaf)) {
            throw "本地应用缺少 VERSION 文件。"
        }
        if (Test-Path -LiteralPath $appRoot) {
            Assert-ChildPath -ParentPath $InstallRoot -ChildPath $appRoot
            Remove-Item -LiteralPath $appRoot -Recurse -Force
        }
        Move-Item -LiteralPath $stagingRoot -Destination $appRoot
        & icacls.exe $appRoot /grant "*S-1-5-20:(OI)(CI)(M)" /T /C /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "无法授予 NETWORK SERVICE 使用本地应用的权限。" }
    }
    finally {
        if (Test-Path -LiteralPath $stagingRoot) {
            Assert-ChildPath -ParentPath $InstallRoot -ChildPath $stagingRoot
            Remove-Item -LiteralPath $stagingRoot -Recurse -Force
        }
    }
}

function Save-ExistingRuntimeState {
    $appRoot = Join-Path $InstallRoot "app"
    if (-not (Test-Path -LiteralPath $appRoot -PathType Container)) { return }
    $stateFiles = @(
        "output\nodes.txt",
        "output\health.json",
        "data\previous-top100.json",
        "data\handoff\local-qualified.json.gz",
        "data\handoff\local-attempted-ips.txt.gz"
    )
    $hasState = @($stateFiles | Where-Object {
        Test-Path -LiteralPath (Join-Path $appRoot $_) -PathType Leaf
    }).Count -gt 0
    if (-not $hasState) {
        Write-Host "旧本地应用没有待保存的运行状态。"
        return
    }
    $runtimeScript = Join-Path $appRoot "scripts\local-runtime.ps1"
    if (-not (Test-Path -LiteralPath $runtimeScript -PathType Leaf)) {
        throw "检测到旧运行状态，但缺少保存脚本；为避免丢失数据，安装已停止。"
    }
    Write-Host "正在保存旧版本尚未发布的本地运行状态。"
    & $runtimeScript -Mode Save -RepoRoot $appRoot -LocalRoot $InstallRoot
    $manifest = Join-Path $InstallRoot "pending-publish\manifest.json"
    if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) {
        throw "旧运行状态保存失败；为避免丢失数据，安装已停止。"
    }
}

function Install-PowerShellScript {
    param([string]$SourcePath, [string]$DestinationPath)
    $content = [IO.File]::ReadAllText($SourcePath, [Text.Encoding]::UTF8)
    $utf8WithBom = New-Object Text.UTF8Encoding($true)
    [IO.File]::WriteAllText($DestinationPath, $content, $utf8WithBom)
}

Install-PowerShellScript -SourcePath (Join-Path $source "notification-watcher.ps1") `
    -DestinationPath (Join-Path $InstallRoot "notification-watcher.ps1")
Install-PowerShellScript -SourcePath (Join-Path $source "manual-start.ps1") `
    -DestinationPath (Join-Path $InstallRoot "manual-start.ps1")
Install-PowerShellScript -SourcePath (Join-Path $source "launch-dashboard.ps1") `
    -DestinationPath (Join-Path $InstallRoot "launch-dashboard.ps1")
Copy-Item -LiteralPath (Join-Path $source "dashboard_server.py") `
    -Destination (Join-Path $InstallRoot "dashboard_server.py") -Force
Copy-Item -LiteralPath (Join-Path $source "dashboard") `
    -Destination $InstallRoot -Recurse -Force
Copy-Item -LiteralPath (Join-Path $source "开始云端和本地优选.cmd") `
    -Destination (Join-Path $InstallRoot "开始云端和本地优选.cmd") -Force
Install-PowerShellScript -SourcePath (Join-Path $PSScriptRoot "notify-user.ps1") `
    -DestinationPath (Join-Path $InstallRoot "notify-user.ps1")
Save-ExistingRuntimeState
Install-LocalApplication
Prepare-RunnerPython

$startup = [Environment]::GetFolderPath("Startup")
$vbsPath = Join-Path $startup "Noode-CG-Notifier.vbs"
$watcher = Join-Path $InstallRoot "notification-watcher.ps1"
$oldManual = Join-Path $InstallRoot "开始云端和本地优选.ps1"
if (Test-Path -LiteralPath $oldManual) {
    Remove-Item -LiteralPath $oldManual -Force
}
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -eq "powershell.exe" -and $_.CommandLine -like "*$watcher*" } |
    ForEach-Object { Invoke-CimMethod -InputObject $_ -MethodName Terminate | Out-Null }
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
    -Message "通知接收器和可见日志启动器安装完成；通知器不再显示托盘图标。" -LocalRoot $InstallRoot
Write-Host "已安装到 $InstallRoot"
Write-Host "手动入口: $(Join-Path $InstallRoot '开始云端和本地优选.cmd')"
