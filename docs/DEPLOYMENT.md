# GitHub 部署指南

## 1. 覆盖上传

解压 `Noode-CG-V8-OnePassForeign.zip`，把其中 `Noode-CG` 文件夹里的全部内容上传到 GitHub 仓库根目录。不要上传外层 ZIP 代替代码。

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

## 2. 清理旧版本

进入 **Actions → Cleanup obsolete files → Run workflow**。它会删除：

- 已取消的四条旧链接缓存；
- 已不再使用的本地 TXT/ZIP 输入；
- 旧评分、历史和分片模块；
- 旧版本压缩包；
- 清理工作流自身。

等待清理任务显示绿色成功，再运行刷新任务。新建空仓库可以跳过清理。

## 3. 运行自动优选

进入 **Actions → Refresh verified endpoints → Run workflow**。以后任务在每 6 小时的第 17 分钟自动执行。

工作流单次上限 350 分钟，并设置并发锁，防止两次刷新同时修改快照。任务会自动提交：

- `output/`；
- 两条指定源的缓存；
- `data/previous-top100.json`；
- `data/previous-official-ips.txt`。

## 4. 检查结果

订阅地址：

```text
https://raw.githubusercontent.com/<用户名>/<仓库名>/main/output/nodes.txt
```

在 `output/health.json` 查看：

- `rounds[].tcp_one_pass_success`：每批 TCP 一次成功数；
- `rounds[].tcp_duration_seconds`：全量 TCP 一次测试耗时；
- `counts.strict_tcp_qualified`：累计严格 TCP 候选；
- `counts.three_metric_qualified`：TCP、TLS、HTTPS TTFB 三项全部合格的数量；
- `metric_batches`：TLS、HTTPS TTFB 各一次及三项综合平均延迟记录；
- `speed_batches[].speed_at_least_minimum`：达到 1Mbps 的数量；
- `speed_batches[].speed_duration_seconds`：256KiB 单次下载测速耗时；
- `rolling_attempts`：本轮 500 加旧 TOP100 的补位复测记录；
- `network_baseline`：Google、Cloudflare、GitHub 单次基线；
- `published`：本轮是否真正覆盖订阅。

若 `published: false`，说明严格条件下未达到 500，原订阅仍被保留。下一次六小时任务会使用新的官方样本继续优选。
