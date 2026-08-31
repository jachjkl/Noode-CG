# GitHub 部署指南

## 覆盖上传

解压 `Noode-CG-V12.1-Hybrid310-NoCN-TCP5-Speed3.zip`，把其中 `Noode-CG` 文件夹里的全部内容上传到 GitHub 仓库根目录。不要直接上传外层 ZIP。

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

旧仓库进入 **Actions → Cleanup obsolete files → Run workflow**。它会删除旧模块、旧缓存、旧动态订阅和旧 TOP100，防止历史 CN 继续显示，并在提交后删除清理工作流自身。

## 第一次刷新

进入 **Actions → Refresh verified endpoints → Run workflow**。清理后 `output/nodes.txt` 暂时不存在，第一次成功运行后出现 310 条地址。

以后任务在每 8 小时的第 17 分钟自动执行。工作流只提交输出、上一轮 TOP100 和上一轮官方抽样快照。

## 结果检查

订阅地址：

```text
https://raw.githubusercontent.com/<用户名>/<仓库名>/main/output/nodes.txt
```

在 `output/health.json` 查看：

- `rounds[].prefilter_tcp_three_pass_success_under_1000ms`：三次初筛 TCPing 成功数量；
- `source_country_lane.link_candidates`：两个链接中本轮提取到的 JP 候选数；
- `source_country_lane.measurements.tcping_measured`：JP 端点完成 TCPing 测量的数量；
- `source_country_lane.measurements.download_measured`：JP 端点取得下载速度的数量；
- `source_country_lane.selected`：仅按 TCPing 和下载结果排序后选出的不同 JP IP 数，应为 10；
- `source_country_lane.tls_skipped` 与 `https_ttfb_skipped`：应为 `true`；
- `rounds[].prefilter_shortlisted`：进入严格阶段的数量，应为 5,000；
- `rounds[].quality_tcp_five_probe_success_under_300ms`：严格 TCP 五次测试后平均不超过 300ms 的数量；
- `metric_batches[].tls_three_pass_success`：TLS 三次合格数；
- `metric_batches[].https_ttfb_three_pass_success`：TTFB 三次合格数；
- `speed_batches[].current_speed_qualified_total`：跨轮累计下载合格数；
- `rolling_attempts[0].previous_tested_this_attempt`：上一轮 TOP100 的唯一一次复测数；
- `counts.final_country_candidates_rejected`：在最终竞争层被排除的 CN/未知地区数量；
- `counts.final_forbidden_country_count`：最终结果中的禁用地区数量，必须为 0；
- `gates.final_top310`、`gates.final_country:JP` 和 `gates.final_no_forbidden_or_unknown_country`：310 条、JP10 和无 CN 发布门槛；
- `published`：本轮是否覆盖订阅。

若 `published: false`，说明两个链接中不足 10 个不同 JP IP、严格非 JP 结果不足 300、5,000 初筛或 Runner 基线没有全部满足。首次迁移已清除旧动态订阅，因此不会继续显示历史 CN。

## 安装本地开机优选

本地自动推送必须使用一个通过 `git clone` 得到的仓库，而不是浏览器下载的普通文件夹。先在 Git Credential Manager 中完成一次 GitHub 登录，然后执行：

```powershell
git clone https://github.com/jachjkl/Noode-CG.git D:\Noode-CG
powershell -ExecutionPolicy Bypass -File D:\Noode-CG\scripts\install-local-cfdata-task.ps1 `
  -RepositoryPath D:\Noode-CG `
  -CfDataExe "D:\桌面\软件\cfdata-windows-amd64.exe"
```

计划任务在当前用户登录且网络可用时执行；脚本会等待网络、使用 CFData 本地优选、写入 `data/local-cfdata-candidates.txt`，再用现有 Git 凭据提交和推送。推送候选会自动触发 `Refresh verified endpoints`。脚本强制关闭 CFData 自带 GitHub 上传，因此不会读取或提交仓库 Token。

手动试运行：

```powershell
powershell -ExecutionPolicy Bypass -File D:\Noode-CG\scripts\run-local-cfdata.ps1 `
  -RepositoryPath D:\Noode-CG `
  -CfDataExe "D:\桌面\软件\cfdata-windows-amd64.exe"
```
