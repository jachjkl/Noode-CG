from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?}")


class ConfigError(ValueError):
    pass


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return ENV_PATTERN.sub(lambda match: os.getenv(match.group(1), match.group(2) or ""), value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path).resolve()
    if not config_path.is_file():
        raise ConfigError(f"配置文件不存在: {config_path}")
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ConfigError("配置文件根节点必须是对象")
    data = _expand_env(data)
    data["_config_path"] = str(config_path)
    data["_base_dir"] = str(config_path.parent)
    validate_config(data)
    return data


def resolve_path(config: dict[str, Any], value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = Path(config["_base_dir"]) / path
    return path.resolve()


def _positive_number(value: Any, name: str, *, allow_zero: bool = False) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigError(f"{name} 必须是数字")
    if value < 0 or (value == 0 and not allow_zero):
        raise ConfigError(f"{name} 必须{'大于等于 0' if allow_zero else '大于 0'}")


def validate_config(config: dict[str, Any]) -> None:
    for section in ("project", "paths", "sources", "pipeline", "output"):
        if section not in config or not isinstance(config[section], dict):
            raise ConfigError(f"缺少配置段: {section}")

    domain = str(config["project"].get("target_domain", "")).strip()
    if not domain or "://" in domain or "/" in domain:
        raise ConfigError("project.target_domain 必须是纯域名，不能包含协议或路径")

    pipeline = config["pipeline"]
    for name in (
        "latency_shortlist",
        "maximum_average_latency_ms",
        "current_selection",
        "max_runtime_seconds",
        "minimum_round_budget_seconds",
        "postprocess_reserve_seconds",
    ):
        _positive_number(pipeline.get(name), f"pipeline.{name}")
    for stage in ("tcp", "tls", "http"):
        block = pipeline.get(stage)
        if not isinstance(block, dict):
            raise ConfigError(f"缺少 pipeline.{stage}")
        _positive_number(block.get("concurrency"), f"pipeline.{stage}.concurrency")
        _positive_number(block.get("timeout_seconds"), f"pipeline.{stage}.timeout_seconds")

    ranges = config["sources"].get("cloudflare_ranges", {})
    _positive_number(ranges.get("official_batch_size"), "sources.cloudflare_ranges.official_batch_size")

    for stage in ("rolling_retest",):
        block = pipeline.get(stage)
        if not isinstance(block, dict):
            raise ConfigError(f"缺少 pipeline.{stage}")
        _positive_number(block.get("concurrency"), f"pipeline.{stage}.concurrency")
        _positive_number(block.get("timeout_seconds"), f"pipeline.{stage}.timeout_seconds")
        _positive_number(block.get("attempts"), f"pipeline.{stage}.attempts")

    top_nodes = config["output"].get("top_nodes")
    _positive_number(top_nodes, "output.top_nodes")
    if int(config["pipeline"]["current_selection"]) != int(top_nodes):
        raise ConfigError("pipeline.current_selection 必须等于 output.top_nodes")
