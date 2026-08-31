# GitHub 部署指南

## 覆盖上传

解压 `Noode-CG-V10-Fast200JP10.zip`，把其中 `Noode-CG` 文件夹里的全部内容上传到 GitHub 仓库根目录。不要直接上传外层 ZIP。

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

旧仓库进入 **Actions → Cleanup obsolete files → Run workflow**。它会删除旧模块、旧本地输入、旧远程缓存、旧官方 CIDR 文件和旧版本 ZIP，并在提交后删除清理工作流自身。

## 第一次刷新

进入 **Actions → Refresh verified endpoints → Run workflow**。初始 `output/nodes.txt` 为空，第一次成功运行后出现 200 条地址。

以后任务在每 8 小时的第 17 分钟自动执行。工作流只提交输出、上一轮 TOP100 和上一轮官方抽样快照。

## 结果检查

订阅地址：

```text
https://raw.githubusercontent.com/<用户名>/<仓库名>/main/output/nodes.txt
```

在 `output/health.json` 查看：

- `rounds[].prefilter_tcp_three_pass_success_under_1000ms`：三次初筛 TCPing 成功数量；
- `rounds[].prefilter_shortlisted`：进入严格阶段的数量，应为 5,000；
- `rounds[].quality_tcp_three_pass_success_under_300ms`：严格 TCP 三次合格数；
- `metric_batches[].tls_three_pass_success`：TLS 三次合格数；
- `metric_batches[].https_ttfb_three_pass_success`：TTFB 三次合格数；
- `speed_batches[].current_speed_qualified_total`：跨轮累计下载合格数；
- `rolling_attempts[0].previous_tested_this_attempt`：上一轮 TOP100 的唯一一次复测数；
- `gates.final_top200` 与 `gates.final_country:JP`：200 条和 JP10 发布门槛；
- `published`：本轮是否覆盖订阅。

若 `published: false`，说明 200 条、JP10、5,000 初筛或 Runner 基线没有全部满足，旧的非空订阅会保留。
