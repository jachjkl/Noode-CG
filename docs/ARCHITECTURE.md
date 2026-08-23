# 架构与判定边界

Noode-CG V2.1 的目标不是找“端口打开的 IP”，而是找从当前运行网络看，能持续完成 Cloudflare TLS、HTTP 和受控下载请求的边缘入口。

```text
必选 zip.cm.edu.kg 源 + 本地种子 + Cloudflare 官方网段
                 |
                 v
        规范化、校验、去重、扩池
                 |
                 v
         高并发 TCP 快速初筛
                 |
                 v
    TLS 握手 + target_domain SNI + 证书验证
                 |
                 v
  Host=target_domain 的 /cdn-cgi/trace 验证
                 |
                 v
   5 次 TCP + 3 次 TLS/HTTP 低并发复测
                 |
                 v
  分批低并发 2 MiB 完整下载与速度门槛
                 |
                 v
     28 次历史窗口、位置补全、多维评分
                 |
                 v
       国家/网段去集中、TOP N、发布保护
```

## 为什么不保证“3 万条一定通过”

`pipeline.min_tcp_alive: 30000` 是质量门槛。网络、运行地点、运营商策略和 Cloudflare 状态都会改变结果。程序会在 `output/health.json` 记录实际数量；不会把未通过 TLS/HTTP 验证的条目补进最终结果。

`https://zip.cm.edu.kg/all.txt` 是必选源：每次运行重试刷新，解析出的所有有效端点都进入 TCP 候选池；在线失败时使用仓库内最后一次成功缓存。只有网络检测失败的端点才会在后续阶段被淘汰。

## 稳定性判定

快速初筛只负责处理 5 万候选。通过 TLS/HTTP 的地址随后连续进行 5 次 TCP 和 3 次完整 Cloudflare trace 请求；默认要求 TCP 至少成功 80%，HTTP 成功 100%，并限制 P95 延迟。测速采用低并发、分批、达标数量足够即停止的方式，借鉴 CFData “先延迟筛选，再低并发准确测速”的思路。

测速读取未达到请求正文的 98% 时，即使已经收到部分数据也标记为失败，避免把容易中途断流的地址当作高速节点。最近 28 次任务的成功/失败保存在 `data/stability-history.json`，新节点从中性分开始，连续稳定后才获得完整历史加分。

## “三网”测量的真实含义

单个 GitHub 托管 Runner 不能代表中国电信、中国联通和中国移动。真正三网数据需要在三个对应网络上的自托管 Runner 分别运行，并把探测 JSON 放入 `data/probes/` 后通过 `vantage.probe_files` 合并。项目不会把国家代码或机房位置冒充运营商测量。

## 输出语义

`output/nodes.txt` 的格式是 `IP:PORT#CC`，它是 edgetunnel 后台的优选地址输入，不是完整的 Clash/V2Ray/sing-box 节点订阅。UUID、WS 路径、Host 和 SNI 应继续由 edgetunnel 生成。

## 断档保护

当合格结果少于 `output.minimum_publish` 且已经存在上一版非空输出时，程序只更新 `health.json`，保留上一版订阅，避免一次网络故障清空线上数据。
