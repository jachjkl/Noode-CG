# V13 架构：云端发现，本地决定

## 为什么改成两段式

GitHub 托管 Runner 通常位于海外数据中心。它测出的 TCP、TLS、TTFB 和下载速度只代表 Runner 到 Cloudflare 的路径，不能代表用户家中宽带到同一 Anycast 地址的路径。V13 只让云端做大规模、低成本的候选压缩，最终质量门槛全部由 Windows 自托管 Runner 在本机直连网络上执行。

## 数据流

```text
两个链接全量候选 + Cloudflare 官方随机 50,000
                    |
                    v
        GitHub 云端 TCP 三次初筛
                    |
            新鲜 TOP5000 交接池
                    |
                    v
Windows 本地：TOP5000 + 上次 TOP100 + 已累计合格结果
                    |
        普通严格通道 / JP 专用通道
                    |
       满 300 才发布，否则请求下一批 5000
```

云端交接文件为 `data/handoff/cloud-top5000.json.gz`。本地未补齐时，合格结果保存在 `data/handoff/local-qualified.json.gz`，已尝试 IP 保存在 `data/handoff/local-attempted-ips.txt.gz`。下一轮云端会同时排除本地已尝试集合和官方抽样快照，因此补池不会把同一批地址再交给本地。

## 唯一性边界

- 单个交接池按 IP 唯一，端口差异不会制造重复节点。
- 同一补齐周期的所有官方 50,000 批次互斥。
- 相邻成功周期排除上一周期官方快照。
- 两个指定链接按要求每次全量重新获取，因此链接本身在不同周期出现相同 IP 是正常且不可避免的；本地同一补齐周期仍只测一次。
- 上一轮 TOP100 是唯一有意复测的历史集合，每个周期只加入一次。

## JP 通道

链接中带 JP 标记的候选不经过云端美国视角淘汰，直接在 5000 交接池中保留。本地只执行 TCPing 三次和一次下载，按丢包、速度、TCP 延迟排序取前 10。它们不执行 TLS 或 HTTPS TTFB，也不套用普通通道门槛；测量失败的地址会排在有有效结果的地址之后。若两个链接合计不足 10 个唯一 JP，整轮不发布并保留历史订阅。

## 普通通道

普通地址本地连续执行 TCPing 五次、TLS 三次、HTTPS TTFB 三次和一次下载。只有已知非 CN 接入国家、TCP 平均不超过 300ms、综合延迟不超过 300ms、抖动不超过 500ms、下载至少 3Mbps 的地址进入累计池。

最终排序依次考虑丢包率、下载速度、平均延迟、抖动。Cloudflare 官方 IP 是 Anycast，CIDR 无法静态映射到唯一数据中心；程序使用本地 `/cdn-cgi/trace` 实测的 `colo` 做软上限分散，优先覆盖更多机房，数量不足时再放宽上限，避免因分散规则导致发布不足 300 条。

## 失败保护与继续补池

不足 300 时，`publish_outputs` 只更新健康报告，不覆盖 `nodes.txt`、`nodes.json`、CSV 或兼容 ZIP。工作流读取 `needs_more=true` 后用仓库 `GITHUB_TOKEN` 再次触发同一工作流，并设置 continuation 模式。新云端批次排除之前的官方 IP 和本地已尝试 IP，本地只测试新地址并与累计合格结果合并。

最多补池 30 轮，防止外部网络长期异常造成无限任务。达到上限仍不足时继续保留最后一次成功订阅，下一次六小时周期再重新开始。

## Windows 控制与弱 GitHub 网络

Windows 用户会话中运行隐藏通知器，自托管 Runner 服务只向 `D:\桌面\软件\Noode-CG-Local\notifications` 写入通知请求，因此服务会话不需要直接显示 UI。代理门禁等待用户关闭代理后再开始测速，手动入口只触发云端工作流并保持最小化。

云端把交接池 SHA-256 作为 job output 传给本地 job。本地应用预先安装在 `D:\桌面\软件\Noode-CG-Local\app`，因此 Windows job 不再 checkout 仓库。本地已有交接池哈希错误时，允许从配置好的只读 GitHub 加速前缀重新下载；任何镜像返回只要不匹配可信哈希就被拒绝。

本地把允许发布的九类结果文件压缩、Base64 分块并通过 Runner 作业输出交给 Ubuntu，同时保存到固定 pending 目录。Ubuntu 校验 SHA-256 和 ZIP 成员白名单后负责提交仓库；Windows 全程不执行 GitHub git 读写。只有云端发布成功后的本地清理 job 才删除 pending、交接池、累计池、TOP100 本地副本、检查点、应用日志和 Python 缓存。若云端发布失败，下一次本地 job 先恢复 pending 再继续。
