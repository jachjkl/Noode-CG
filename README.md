# Noode-CG V2

Noode-CG V2 从本地种子、ZIP 数据和 Cloudflare 官方网段构建候选池，通过 TCP、目标域名 TLS/SNI、Cloudflare HTTP trace、可选 WebSocket 与小样本下载测速分层过滤，最后生成 edgetunnel 可读取的 TOP300 地址列表。

## 已实现

- 解析 `IP:端口#国家`、纯 IP、空格分隔、CSV、JSON。
- 解析 `端口/国家.txt` 结构的 ZIP；与你提供的 `ip.zip` 兼容。
- 从 Cloudflare 官方 IPv4/IPv6 网段确定性采样，把候选池扩展到配置规模。
- TCP、受信任证书、SNI、HTTP Host、`/cdn-cgi/trace` 和可选 WebSocket Upgrade 验证。
- 延迟、抖动、丢包、速度、地区、协议完整性评分。
- 国家和 IPv4 `/24` / IPv6 `/48` 去集中，避免 TOP300 被同一小网段占满。
- 结果不足时保留上一版非空订阅，并在 `health.json` 报告失败门槛。
- 生成 `nodes.txt`、`nodes.json`、`nodes.csv`、`api.json`、兼容结构 `ip.zip`。
- 每 6 小时执行的 GitHub Actions、CI、只读 HTTP API 和 Docker 配置。
- Cloudflare Pages Functions 路由，可直接提供 `/api/nodes` 与 `/api/health`。

## 重要说明

`output/nodes.txt` 是 edgetunnel 的优选地址源，不是可以直接导入 Clash/V2Ray 的完整节点订阅。真实客户端订阅仍由 edgetunnel 根据 UUID、WS 路径、Host、SNI 和 TLS 参数生成。

“5 万候选、3 万 TCP 可用”是质量门槛，不是可以无视网络现实的固定产量。运行地点不同，结果一定不同；GitHub Runner 的结果也不等于你本地运营商的结果。

## 快速开始

```powershell
python -m pip install -r requirements.txt
python main.py validate
python main.py run
```

首次运行前至少检查：

```yaml
project:
  target_domain: jackoyu.dpdns.org
```

如果这不是你当前绑定 edgetunnel Worker 的域名，请替换它。

输出地址：

```text
https://raw.githubusercontent.com/<owner>/<repo>/main/output/nodes.txt
```

如果按共享方案使用仓库 `jachjkl/Noode-CG`，可直接填写：

```text
https://raw.githubusercontent.com/jachjkl/Noode-CG/refs/heads/main/output/nodes.txt
```

完整部署见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)，判定逻辑见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。Cloudflare 官方网段在运行时从 [IPv4](https://www.cloudflare.com/ips-v4/) 和 [IPv6](https://www.cloudflare.com/ips-v6/) 刷新，失败时回退到仓库快照。

## 配置重点

- `sources.local`：本地 TXT/CSV/JSON/ZIP 种子。
- `sources.remote`：每次运行强制刷新 `https://zip.cm.edu.kg/all.txt`；全部有效条目优先进入候选，失败时回退仓库缓存。
- `pipeline.*`：各检测阶段并发、超时与质量门槛。
- `pipeline.websocket`：只有知道真实 Worker 路径后才开启。
- `score.*`：评分权重和上限。
- `output.*`：TOP 数量、发布下限、国家和网段去集中。
- `vantage.probe_files`：合并自托管运营商探针数据。

### 强制候选源

`zip.cm.edu.kg/all.txt` 中解析成功的全部 `IP:端口` 每次都会先加入检测池，再用 Cloudflare 官方网段补足 50,000 个不同 IP。`30,000` 是 TCP 通过数量的质量门槛；远程源中未通过本次 TCP/TLS/HTTP 检测的地址不会被强行发布。每次在线数据同时写入 `data/sources/zip-cm-edu-kg-all.txt`，Actions 会提交该缓存，保证服务临时不可用时仍有上一版数据。为防止服务器返回截断列表，在线结果少于 10,000 条或低于上一缓存的 80% 时不会覆盖完整缓存。

## 合规和负载

只测试你有权测试的域名与网络。默认候选来自 Cloudflare 公布的共享代理网段，程序带有并发、超时和下载字节上限；仍应根据自己的网络和适用规则降低负载。项目不包含第三方 CFData 可执行文件，也不接受在仓库内保存 Token。
