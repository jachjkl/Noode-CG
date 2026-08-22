# Noode-CG V2

-  获取`IP:端口#国家`、纯 IP、空格分隔、CSV、JSON。
-  获取`端口/国家.txt` 结构的 ZIP；与你提供的 `ip.zip` 兼容。
-  获取网段确定性采样，把候选池扩展到配置规模。
- TCP、受信任证书、SNI、HTTP Host、`/cdn-cgi/trace` 和可选 WebSocket Upgrade 验证。
- 延迟、抖动、丢包、速度、地区、协议完整性评分。
- 国家和 IPv4 `/24` / IPv6 `/48` 去集中，避免 TOP300 被同一小网段占满。
- 结果不足时保留上一版非空订阅，并在 `health.json` 报告失败门槛。
- 生成 `nodes.txt`、`nodes.json`、`nodes.csv`、`api.json`、兼容结构 `ip.zip`。
- 每 6 小时执行的 GitHub Actions、CI、只读 HTTP API 和 Docker 配置。
- Cloudflare Pages Functions 路由，可直接提供 `/api/nodes` 与 `/api/health`。

⚠️ 免责声明
本项目（"Noode-CG"）仅供教育、科学研究及个人安全测试之目的。
使用者在下载或使用本项目代码时，必须严格遵守所在地区的法律法规。
作者jachjkl对任何滥用本项目代码导致的行为或后果均不承担任何责任。
本项目不对因使用代码引起的任何直接或间接损害负责。
建议在测试完成后 24 小时内删除本项目相关部署。
