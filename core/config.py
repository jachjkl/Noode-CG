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
    for section in ("project", "paths", "sources", "pipeline", "score", "output"):
        if section not in config or not isinstance(config[section], dict):
            raise ConfigError(f"缺少配置段: {section}")

    domain = str(config["project"].get("target_domain", "")).strip()
    if not domain or "://" in domain or "/" in domain:
        raise ConfigError("project.target_domain 必须是纯域名，不能包含协议或路径")

    pipeline = config["pipeline"]
    for name in (
        "min_pool",
        "min_tcp_alive",
        "min_tls_valid",
        "min_http_valid",
        "min_stable_valid",
        "min_speed_qualified",
    ):
        _positive_number(pipeline.get(name), f"pipeline.{name}", allow_zero=name != "min_pool")
    for stage in ("tcp", "tls", "http"):
        block = pipeline.get(stage)
        if not isinstance(block, dict):
            raise ConfigError(f"缺少 pipeline.{stage}")
        _positive_number(block.get("concurrency"), f"pipeline.{stage}.concurrency")
        _positive_number(block.get("timeout_seconds"), f"pipeline.{stage}.timeout_seconds")

    scan = pipeline.get("scan")
    if not isinstance(scan, dict):
        raise ConfigError("缺少 pipeline.scan")
    for name in ("limit", "shards", "window_hours"):
        _positive_number(scan.get(name), f"pipeline.scan.{name}")

    stability = pipeline.get("stability", {})
    if stability.get("enabled", True):
        _positive_number(stability.get("candidates"), "pipeline.stability.candidates")
        for stage in ("tcp", "http"):
            block = stability.get(stage)
            if not isinstance(block, dict):
                raise ConfigError(f"缺少 pipeline.stability.{stage}")
            _positive_number(block.get("concurrency"), f"pipeline.stability.{stage}.concurrency")
            _positive_number(block.get("timeout_seconds"), f"pipeline.stability.{stage}.timeout_seconds")
            _positive_number(block.get("attempts"), f"pipeline.stability.{stage}.attempts")
            ratio = float(block.get("minimum_success_ratio", 0.0))
            if not 0 <= ratio <= 1:
                raise ConfigError(f"pipeline.stability.{stage}.minimum_success_ratio 必须在 0 到 1 之间")

    speed = pipeline.get("speed", {})
    if speed.get("enabled", True):
        for name in ("concurrency", "timeout_seconds", "bytes_per_test", "candidates", "batch_size"):
            _positive_number(speed.get(name), f"pipeline.speed.{name}")
        completion = float(speed.get("minimum_completion_ratio", 0.0))
        if not 0 <= completion <= 1:
            raise ConfigError("pipeline.speed.minimum_completion_ratio 必须在 0 到 1 之间")

    weights = config["score"].get("weights", {})
    if not weights or abs(sum(float(v) for v in weights.values()) - 1.0) > 0.0001:
        raise ConfigError("score.weights 权重之和必须等于 1")

    top_nodes = config["output"].get("top_nodes")
    _positive_number(top_nodes, "output.top_nodes")

    platform = config.get("platform_compatibility", {})
    if not isinstance(platform, dict):
        raise ConfigError("platform_compatibility 必须是对象")
    if platform.get("required", False):
        _positive_number(platform.get("minimum_nodes"), "platform_compatibility.minimum_nodes")
        _positive_number(platform.get("minimum_vantages"), "platform_compatibility.minimum_vantages")
        if not platform.get("required_platforms"):
            raise ConfigError("严格平台验证必须配置 required_platforms")
