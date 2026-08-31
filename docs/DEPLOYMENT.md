# GitHub 部署指南

## 1. 覆盖上传

解压 `Noode-CG-V3-Rolling300.zip`，将解压后 `Noode-CG` 文件夹里的全部内容上传到 GitHub 仓库根目录。根目录应直接出现：

```text
.github/
core/
data/
output/
config.yaml
main.py
requirements.txt
```

不要只上传 ZIP，也不要形成 `仓库/Noode-CG/Noode-CG/...`。

## 2. 一次性清理旧文件

网页覆盖上传不会删除旧版文件。进入 **Actions → Cleanup obsolete files → Run workflow**。该工作流会删除旧模块、旧测试、废弃数据和旧压缩包，提交后再删除自身。

等待它显示绿色成功后，再运行 **Refresh verified endpoints**。新建空仓库可跳过清理。

## 3. 自动优选

刷新任务每 6 小时自动运行，单次最大 350 分钟。同一仓库不会并发运行两份刷新任务。每次任务会：

1. 全量刷新 6 个链接源；
2. 排除上次官方样本并抽取新的 50,000 个官方 IP；
3. 必要时继续追加互不重复的 50,000 批次，直到得到 3,000 个平均延迟不超过 300ms 的地址或接近运行时限；
4. 测速并选本轮 TOP300；
5. 加入上轮 TOP100，全部重新测试后发布最终 TOP300；
6. 自动提交 `output/`、源缓存、TOP100 快照和官方样本快照。

如果接近 GitHub 托管任务时限仍未凑齐 3,000，程序会保留旧订阅，不会伪造或发布未验证地址。下一次六小时任务会换一批官方候选继续运行。

## 4. 订阅地址

```text
https://raw.githubusercontent.com/<用户名>/<仓库名>/main/output/nodes.txt
```

检查 `output/health.json`：

- `status: ok`：所有关键数量门槛通过；
- `published: true`：本轮 300 已覆盖订阅；
- `counts.official_previous_excluded`：为避免相邻运行重复而排除的上轮官方 IP 数；
- `counts.official_sampled_this_run`：本轮抽取的官方 IP 总数；
- `counts.latency_eligible`：平均延迟不超过 300ms 的累计数量；
- `counts.previous_loaded / previous_reverified / previous_in_final`：旧 TOP100 的复测情况；
- `rounds`：每一批 50,000 的 TCP、TLS、HTTP 和合格数量。

## 5. 常见问题

Actions 无法推送时，进入 **Settings → Actions → General → Workflow permissions**，允许工作流读写仓库内容。

目标域名必须在 `config.yaml` 中正确配置并绑定到你的 Worker。GitHub Runner 的网络位置与本地三网不同，GitHub 排名不能等同于电信、联通、移动本地结果。
