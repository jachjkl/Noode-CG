# GitHub 部署指南

## 1. 上传

解压发布包，将解压后 `Noode-CG` 文件夹内的全部内容上传到 GitHub 仓库根目录。根目录应直接出现：

```text
.github/
core/
data/
output/
config.yaml
main.py
requirements.txt
```

## 2. 检查目标域名

打开 `config.yaml`，确认 `project.target_domain` 是绑定到你自己 Worker/edgetunnel 的公开域名。这个项目不需要 UUID、WS 路径、订阅内容或 GitHub Secret。

## 3. 允许运行工作流

仓库进入 **Actions**。如果 GitHub 首次提示启用工作流，点击启用。选择 **Refresh verified endpoints**，点击 **Run workflow**。

如果是覆盖旧版仓库，先运行一次 **Cleanup obsolete V2.3 files**。GitHub 网页上传不会删除旧文件，这个一次性工作流会清除旧模块与旧测试，然后删除自身。清理提交触发的 CI 应只运行当前 19 项测试。

工作流会：

1. 更新 `zip.cm.edu.kg/all.txt` 缓存；
2. 读取仓库内的 TXT 和 ZIP 种子；
3. 从 Cloudflare 官方 IPv4 网段补足 100,000 个不同 IP；
4. 扫描、验证、测速并选择 TOP 300；
5. 自动提交 `output/` 和 `data/sources/` 的变化。

工作流使用 GitHub 机器人账户提交，权限只包含当前仓库的 `contents: write`。默认超时 75 分钟，同一仓库不会同时运行两份刷新任务。

## 4. 获取地址

```text
https://raw.githubusercontent.com/<用户名>/<仓库名>/main/output/nodes.txt
```

检查 `output/health.json`：

- `status` 为 `ok` 表示配置的基础数量门槛通过；
- `published` 表示本轮是否写入新列表；
- `counts.selected_countries.JP` 应至少为 10（前提是本轮确有 10 个合格日本节点）；
- `warnings` 会说明数据源回退或日本节点不足。

## 常见问题

如果 Actions 无法推送，进入仓库 **Settings → Actions → General → Workflow permissions**，允许工作流读写仓库内容。

如果本轮网络异常导致结果不足，程序会保留上一版非空订阅，不会用空文件覆盖。测速只参与评分，单次下载未完成不会让所有节点失效。

GitHub 托管 Runner 的网络位置与本地宽带不同，因此排名也会不同。若需要真实三网表现，应在对应运营商网络运行同一程序；GitHub 结果不能等同于电信、联通、移动本地结果。
