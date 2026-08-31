# GitHub 部署指南

## 覆盖上传

解压 `Noode-CG-V9-ThreePassJP15.zip`，把其中 `Noode-CG` 文件夹里的全部内容上传到 GitHub 仓库根目录。不要直接上传外层 ZIP。

根目录应直接出现：

```text
.github/
core/
data/
output/
config.yaml
main.py
requirements.txt
```

压缩包没有远程源缓存、官方网段快照、候选列表和历史抽样 IP。

## 清理旧版本

进入 **Actions → Cleanup obsolete files → Run workflow**。它会删除旧模块、旧本地输入、旧远程缓存、旧官方 CIDR 文件和旧版本 ZIP，并在提交后删除清理工作流自身。

新仓库可以跳过清理。旧仓库必须先完成清理，再运行刷新任务。

## 第一次刷新

进入 **Actions → Refresh verified endpoints → Run workflow**。初始 `output/nodes.txt` 为空，第一次成功运行后才会出现 500 条地址。

以后任务在每 6 小时的第 17 分钟自动执行。工作流会提交：

- `output/`；
- `data/previous-top100.json`；
- `data/previous-official-ips.txt`。

不会提交两个远程源的下载副本。

## 结果检查

订阅地址：

```text
https://raw.githubusercontent.com/<用户名>/<仓库名>/main/output/nodes.txt
```

在 `output/health.json` 查看：

- `rounds[].tcp_three_pass_success`：每批三次 TCPing 全部成功且满足门槛的数量；
- `rounds[].tcp_selected_for_tls_https`：进入 TLS/HTTPS 三次测试的数量；
- `metric_batches[].tls_three_pass_success`：TLS 三次成功数；
- `metric_batches[].https_ttfb_three_pass_success`：HTTPS TTFB 三次成功数；
- `counts.three_metric_qualified`：三项平均、单项、抖动和地区全部合格的累计数；
- `speed_batches[].speed_at_least_minimum`：单次下载达到 1Mbps 的数量；
- `rolling_attempts[].verified_accumulated`：最终复测成功累计数；
- `gates` 中的 `final_country:JP`：日本最低 15 条是否满足；
- `network_baseline`：Google、Cloudflare、GitHub 各三次 Runner 基线；
- `published`：本轮是否覆盖订阅。

若 `published: false`，说明 500 条、JP15、三次基线或其他严格条件没有全部满足，旧的非空订阅会保留。下一次任务使用新的官方随机样本继续优选。
