# V13.6.1 部署说明

## 1. 上传代码包

1. 解压 `Noode-CG-V13.6.1-UTF8Fix.zip`。
2. 打开解压后的 `Noode-CG` 文件夹。
3. 在 GitHub 仓库根目录选择 **Add file → Upload files**。
4. 上传 `Noode-CG` 文件夹里面的全部内容，而不是上传 ZIP 文件或再套一层 `Noode-CG` 目录。
5. 覆盖同名文件并提交到 `main`。

仓库根目录应该直接看到 `.github`、`core`、`scripts`、`tests`、`config.yaml`、`main.py` 和 `README.md`。

## 2. 一次性清理旧文件

进入 **Actions → Cleanup obsolete files → Run workflow**。该工作流会删除旧版流水线、旧 CFData 本地任务脚本和旧发布 ZIP，但保留当前历史订阅与上一轮 TOP100。清理成功后它会把自己一并删除，所以只需执行一次。

## 3. 安装 Windows 自托管 Runner

在仓库打开 **Settings → Actions → Runners → New self-hosted runner**，选择 **Windows / x64**。在管理员 PowerShell 中逐条执行 GitHub 页面为这个仓库生成的下载、解压和配置命令。配置时必须满足：

- Runner 名称可自定义，例如 `Noode-CG-Home`；
- 额外标签加入 `noode-cg`；默认的 `self-hosted`、`windows`、`x64` 标签要保留；
- 选择把 Runner 安装为 Windows 服务，并让服务自动启动；
- 服务账号可以使用默认的 `NT AUTHORITY\NETWORK SERVICE`；V13.6.1 安装器会只对 `D:\桌面\软件\Noode-CG-Local` 授予所需权限；
- Runner 的工作目录使用独立目录，例如 `D:\actions-runner`，不要放在本项目或下载目录中。

如果 GitHub 页面给出的配置命令没有标签参数，可在命令尾加入：

```powershell
--labels noode-cg
```

注册 Token 是短期的一次性凭据，只粘贴到本机配置命令，不写入仓库、配置文件或截图。安装完成后，Runners 页面应显示该 Runner 为 **Idle**。

## 4. 保证本地直连

先安装 Windows 控制器：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-windows-controller.ps1
```

它会创建 `D:\桌面\软件\Noode-CG-Local\开始云端和本地优选.cmd`，在当前用户启动目录注册无托盘图标的通知接收器，并准备两个固定目录：

- `D:\桌面\软件\Noode-CG-Local\runtime\python`：Runner 专用 Python；
- `D:\桌面\软件\Noode-CG-Local\app`：本地测速应用副本。

V13.6.1 的 Windows 作业直接使用这两个目录，不再从 `github.com` 执行 `git fetch`、`git pull` 或 `git push`。安装器只对这个本地目录授予 `NETWORK SERVICE` 所需的修改/执行权限，不会让 Actions 修改注册表或临时安装 Python。每次上传新版本后都必须重新运行一次安装器，确保 GitHub 工作流版本和 D 盘应用版本一致。V13.6.1 还强制 Windows PowerShell 5.1 以 UTF-8 读取 Python 生成的 JSON，避免中文警告导致 `ConvertFrom-Json` 误判文件损坏。

手动程序使用正常可见窗口显示 Actions 各阶段状态；成功后自动关闭，失败时保留窗口和日志。自托管 Runner 作为 Windows 服务时不显示单独测速窗口。

运行任务前关闭 Clash 系统代理、TUN/虚拟网卡代理和 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量。工作流检测到代理后会发出 Windows 通知并等待关闭，而不是立刻用代理线路测速；30 分钟仍未关闭时，本轮停止并保留历史订阅。

交接池读取支持直连和 `gh-proxy.com`、`ghfast.top`、`gh.ddlc.top` 三个镜像。镜像结果必须通过云端 SHA-256 校验。第三方镜像只处理公开的压缩交接池，不会收到 GitHub Token；Actions 任务领取和最终推送仍需要 GitHub 连接。

## 5. 首次运行

可以双击 D 盘的 `开始云端和本地优选.cmd`，也可以进入 **Actions → Cloud TOP5000 to local TOP300 → Run workflow**，`continuation` 保持未选中。流程是：

1. Ubuntu 云端获取链接全量和官方随机池，生成新的 TOP5000；
2. Windows Runner 自动领取本地任务，复测 TOP5000 + 上次 TOP100；
3. Windows 把限定文件组成的压缩结果分块写入 Runner 作业输出，并附带 SHA-256；本机不直接连接 GitHub 仓库；
4. Ubuntu 云端重组并校验结果，然后负责提交订阅和累计数据；不足 300 条时再请求下一批不重复 TOP5000；
5. 只有 Ubuntu 发布成功后，Windows 才清理 pending、日志、缓存和运行数据，并发出完成通知；发布失败时本地 pending 会保留到下一次；
6. 订阅始终使用：

```text
https://raw.githubusercontent.com/jachjkl/Noode-CG/main/output/nodes.txt
```

不要使用 GitHub 的 `/blob/` 页面地址作为订阅。

## 6. 开机和每六小时运行

工作流使用固定六小时计划。电脑关机时 Windows 本地 job 会等待，仓库中的最近一次成功订阅不会被删除。并发规则只保留最新任务；电脑开机后 Runner 服务自动上线并立即领取等待中的最新 job。电脑持续开机时，计划任务每六小时重新运行。

如果关机超过 GitHub 对离线自托管 job 的排队期限，开机后可以手动点一次 **Run workflow**；正常每六小时调度会不断产生新的最新任务，因此通常不需要额外的 Windows 计划任务。

## 7. 查看状态

- `data/handoff/cloud-health.json`：云端输入数、官方抽样数和交接池数量；
- `output/health.json`：本地实际测试、累计合格数、最终数量和是否请求下一批；
- `output/nodes.txt`：最终订阅；
- `output/nodes.json`：每个节点的 TCP/TLS/TTFB、抖动、丢包、速度和 colo 明细。

如果本地步骤一直显示 queued，检查 Runner 服务是否正在运行以及标签是否包含 `noode-cg`。如果提示 D 盘应用版本不是 13.6.1，请在解压后的 V13.6.1 `Noode-CG` 目录重新运行 `scripts\install-windows-controller.ps1`。如果直接网络检查失败，关闭代理后在 Actions 页面重新运行。
