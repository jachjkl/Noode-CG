# GitHub 部署

1. 创建空仓库，把本项目根目录全部上传到默认分支。
2. 修改 `config.yaml` 的 `project.target_domain`，必须是绑定到 edgetunnel Worker 的纯域名。
3. 在仓库的 **Settings → Actions → General → Workflow permissions** 中允许 GitHub Actions 写入仓库内容。
4. 打开 **Actions → Refresh verified endpoints → Run workflow** 做首次运行。
5. 在 `output/health.json` 确认 `http_valid` 和 `published`，再把下面地址填到 edgetunnel 的优选 IP 订阅：

   ```text
   https://raw.githubusercontent.com/<owner>/<repo>/main/output/nodes.txt
   ```

   共享方案中的固定地址是：

   ```text
   https://raw.githubusercontent.com/jachjkl/Noode-CG/refs/heads/main/output/nodes.txt
   ```

计划任务每 6 小时运行一次。GitHub 的 cron 使用 UTC。

每次任务都会先刷新 `https://zip.cm.edu.kg/all.txt`，将其中全部有效地址加入候选，并把最新副本保存到 `data/sources/zip-cm-edu-kg-all.txt`。如果该站暂时无法访问，任务使用仓库缓存；远程源和缓存同时不可用时任务会失败并保留旧订阅。

## 本机运行

Windows PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run-local.ps1
```

手动命令：

```powershell
python -m pip install -r requirements.txt
python main.py validate
python main.py run
python main.py serve --host 127.0.0.1 --port 8080
```

API 路由：

- `/api/nodes`
- `/api/health`
- `/nodes.txt`
- `/nodes.json`
- `/nodes.csv`
- `/ip.zip`

## Cloudflare Pages API

项目包含 `functions/api/`，可把静态结果映射成 `/api/nodes` 和 `/api/health`：

1. 在 Cloudflare **Workers & Pages** 中连接同一个 GitHub 仓库。
2. Framework preset 选 None，Build command 填 `exit 0`，Build output directory 填 `output`。
3. 部署后可访问 `https://<你的 Pages 域名>/api/nodes`。

Pages/API 域名应与 edgetunnel Worker 的 `project.target_domain` 分开。例如 `data.example.com` 给 Pages，`proxy.example.com` 给 tunnel，避免静态站点抢占 WebSocket 路由。

## WebSocket 实测

基础验证默认检查证书、SNI、Host 与 Cloudflare trace。若要验证 edgetunnel 的真实 WebSocket 路径：

```yaml
pipeline:
  websocket:
    enabled: true
    path: /你的实际路径
```

路径不正确会导致所有节点失败，因此不要保留示例值后直接开启。

## CFData 可选预筛

`integrations/cfdata-config.example.json` 是按 v1.7.7 字段整理的非标 TLS/HTTPing 配置。不要把本地 EXE、GitHub Token 或真实 `cfdata-config.json` 提交进仓库。CFData 可做预筛，但最终发布仍以 Noode-CG 对 `target_domain` 的独立验证为准。
