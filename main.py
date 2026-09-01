from __future__ import annotations

import argparse
import json
import sys

from api.server import serve
from core.config import ConfigError, load_config, resolve_path
from core.handoff import prepare_cloud_handoff, run_local_selection
from core.pipeline import run_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="noode-cg",
        description="验证 Cloudflare 边缘端点并生成 edgetunnel 地址订阅",
    )
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("run", help="运行完整优选流水线")
    subparsers.add_parser("prepare-handoff", help="云端生成新的 TOP5000 交接池")
    subparsers.add_parser("local-select", help="本地复测交接池和上一轮 TOP100")
    subparsers.add_parser("validate", help="只验证配置")
    server = subparsers.add_parser("serve", help="启动只读 HTTP API")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8080)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "run"
    try:
        config = load_config(args.config)
        if command == "validate":
            print("配置验证通过")
            return 0
        if command == "serve":
            serve(args.host, args.port, resolve_path(config, config["paths"]["output"]))
            return 0
        if command == "prepare-handoff":
            report = prepare_cloud_handoff(config)
        elif command == "local-select":
            report = run_local_selection(config)
        else:
            report = run_pipeline(config)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if config["pipeline"].get("fail_on_quality_gate", False) and report["status"] != "ok":
            return 2
        return 0
    except (ConfigError, OSError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已取消", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
