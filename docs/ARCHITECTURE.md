# 架构与判定边界

Noode-CG V2.3 的目标不是找“端口打开的 IP”，而是找能持续完成 Cloudflare TLS、HTTP 和受控下载请求，同时保留亚洲落地方向的边缘入口。

```text
必选 zip.cm.edu.kg 源 + 本地种子 + Cloudflare 官方网段 + VPS789 可选参考
                 |
                 v
       规范化、校验、去重、扩到 10 万
                 |
                 v
 必选/历史节点常驻 + 官方样本三片轮换（每轮 5 万）
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
 NRT/ICN/HKG/SIN 预留 + 5 次 TCP + 3 次 HTTP 复测
                 |
                 v
  分批低并发 2 MiB 完整下载与速度门槛
                 |
                 v
 28 次历史窗口、匿名平台结果、位置补全、多维评分
                 |
                 v
 JP/KR 与 NRT/ICN 硬门槛、机房去集中、发布保护
```

## 为什么不保证“3 万条一定通过”

`pipeline.min_tcp_alive: 30000` 是质量门槛。网络、运行地点、运营商策略和 Cloudflare 状态都会改变结果。程序会在 `output/health.json` 记录实际数量；不会把未通过 TLS/HTTP 验证的条目补进最终结果。

`https://zip.cm.edu.kg/all.txt` 是必选源：每次运行重试刷新，解析出的所有有效端点都进入 TCP 候选池；在线失败时使用仓库内最后一次成功缓存。只有网络检测失败的端点才会在后续阶段被淘汰。

## 稳定性判定

逻辑候选池是 10 万 IP，快速初筛每轮只处理其中 5 万。`zip.cm.edu.kg` 必选源和历史节点常驻，官方采样分三片轮换，在计划任务正常运行时最多 18 小时覆盖一轮。通过 TLS/HTTP 的地址随后连续进行 5 次 TCP 和 3 次完整 Cloudflare trace 请求；默认要求 TCP 至少成功 80%，HTTP 成功 100%，并限制 P95 延迟。测速采用低并发、分批、达标数量足够即停止的方式，借鉴 CFData “先延迟筛选，再低并发准确测速”的思路。

测速读取未达到请求正文的 98% 时，即使已经收到部分数据也标记为失败，避免把容易中途断流的地址当作高速节点。最近 28 次任务的成功/失败保存在 `data/stability-history.json`，新节点从中性分开始，连续稳定后才获得完整历史加分。

## 为什么不能只按 GitHub 延迟排序

GitHub 托管 Runner 通常位于美国。Cloudflare 官方 Anycast IP 会从 Runner 进入美国机房，因此对 GitHub 很快，却可能从中国网络绕到 LAX。V2.3 将来源反代节点和实际 `colo` 结合：精测阶段预留 NRT、ICN、HKG、SIN，测速阶段再次预留 NRT/ICN；最终至少选择 10 个 NRT 和 10 个 ICN，并限制纯官方随机样本数量。`/cdn-cgi/trace` 的 `loc` 表示探测客户端位置，不能当成节点落地国家；输出国家改用机房国家，缺失时才回退来源标签。

## “三网”测量的真实含义

单个 GitHub 托管 Runner 不能代表中国电信、中国联通和中国移动。真正三网数据需要在三个对应网络上的自托管 Runner 分别运行，并把探测 JSON 放入 `data/probes/` 后通过 `vantage.probe_files` 合并。项目不会把国家代码或机房位置冒充运营商测量。

VPS789 的公开接口可提供三网延迟和丢包的外部参考，但其样本少、可能过期，也不是从你的 Worker 链路测得。因此只在记录不超过 `max_age_days` 时参与小权重评分；无论接口成功与否，最终节点仍必须通过本项目自己的 TLS、HTTP、稳定性和完整下载测试。

## 平台兼容性的边界

Cloudflare 优选 IP 是入口，X、Telegram、YouTube 等目标平台位于 Worker 出口之后。仅凭入口 IP 无法证明整条代理链可访问目标平台。项目允许合并匿名本地结果，并在 `platform_compatibility.required: true` 时严格过滤，但探针文件只需保存平台布尔值，不需要保存 UUID、路径、域名或订阅。没有真实链路结果时默认保持建议模式，不声称平台已验证。ChatGPT 还会受 Worker 出口地区和 IP 信誉影响；无密钥模式只能用 NRT/ICN 落地、Worker 重复请求和下载完整率降低风险，不能作绝对保证。

## 输出语义

`output/nodes.txt` 的格式是 `IP:PORT#CC`，它是 edgetunnel 后台的优选地址输入，不是完整的 Clash/V2Ray/sing-box 节点订阅。UUID、WS 路径、Host 和 SNI 应继续由 edgetunnel 生成。

## 断档保护

当合格结果少于 `output.minimum_publish`、NRT/ICN 或 JP/KR 配额不足、或者其他质量门槛失败时，程序只更新 `health.json`，保留上一版订阅，避免一次网络故障或美国方向偏置覆盖可用结果。
