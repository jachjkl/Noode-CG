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
        "prefilter_shortlist",
        "maximum_combined_latency_ms",
        "maximum_component_latency_ms",
        "maximum_jitter_ms",
        "current_selection",
        "speed_batch_size",
        "max_official_rounds",
        "max_runtime_seconds",
        "minimum_round_budget_seconds",
        "postprocess_reserve_seconds",
    ):
        _positive_number(pipeline.get(name), f"pipeline.{name}")
    for stage in ("prefilter_tcp", "quality_tcp", "tls", "http"):
        block = pipeline.get(stage)
        if not isinstance(block, dict):
            raise ConfigError(f"缺少 pipeline.{stage}")
        _positive_number(block.get("concurrency"), f"pipeline.{stage}.concurrency")
        _positive_number(block.get("timeout_seconds"), f"pipeline.{stage}.timeout_seconds")
        _positive_number(block.get("maximum_jitter_ms"), f"pipeline.{stage}.maximum_jitter_ms")
    _positive_number(
        pipeline["prefilter_tcp"].get("attempts"),
        "pipeline.prefilter_tcp.attempts",
    )
    _positive_number(
        pipeline["prefilter_tcp"].get("maximum_average_latency_ms"),
        "pipeline.prefilter_tcp.maximum_average_latency_ms",
    )
    _positive_number(
        pipeline["quality_tcp"].get("attempts"),
        "pipeline.quality_tcp.attempts",
    )
    _positive_number(
        pipeline["quality_tcp"].get("maximum_average_latency_ms"),
        "pipeline.quality_tcp.maximum_average_latency_ms",
    )
    _positive_number(pipeline["tls"].get("attempts"), "pipeline.tls.attempts")
    _positive_number(
        pipeline["tls"].get("maximum_average_latency_ms"),
        "pipeline.tls.maximum_average_latency_ms",
    )
    _positive_number(pipeline["http"].get("attempts"), "pipeline.http.attempts")
    _positive_number(
        pipeline["http"].get("maximum_average_ttfb_ms"),
        "pipeline.http.maximum_average_ttfb_ms",
    )
    for stage in ("prefilter_tcp", "quality_tcp", "tls", "http"):
        if int(pipeline[stage].get("attempts", 0)) != 3:
            raise ConfigError(f"pipeline.{stage}.attempts 必须等于 3")
        if pipeline[stage].get("require_all_attempts") is not True:
            raise ConfigError(f"pipeline.{stage}.require_all_attempts 必须为 true")

    speed = pipeline.get("speed")
    if not isinstance(speed, dict):
        raise ConfigError("缺少 pipeline.speed")
    for name in ("candidates", "concurrency", "timeout_seconds", "minimum_mbps", "bytes_per_test"):
        _positive_number(speed.get(name), f"pipeline.speed.{name}")
    _positive_number(speed.get("maximum_download_seconds"), "pipeline.speed.maximum_download_seconds")
    if int(pipeline["speed_batch_size"]) > int(pipeline["prefilter_shortlist"]):
        raise ConfigError("pipeline.speed_batch_size 不能大于 prefilter_shortlist")
    if float(pipeline["prefilter_tcp"]["maximum_average_latency_ms"]) != 1000:
        raise ConfigError("pipeline.prefilter_tcp.maximum_average_latency_ms 必须等于 1000")
    if float(pipeline["quality_tcp"]["maximum_average_latency_ms"]) != 300:
        raise ConfigError("pipeline.quality_tcp.maximum_average_latency_ms 必须等于 300")

    country_minimums = pipeline.get("country_minimums")
    if not isinstance(country_minimums, dict) or not country_minimums:
        raise ConfigError("pipeline.country_minimums 必须是非空对象")
    for country, minimum in country_minimums.items():
        if len(str(country)) != 2:
            raise ConfigError("pipeline.country_minimums 国家代码必须是两个字母")
        _positive_number(minimum, f"pipeline.country_minimums.{country}")
    if sum(int(value) for value in country_minimums.values()) > int(pipeline["current_selection"]):
        raise ConfigError("pipeline.country_minimums 合计不能超过 current_selection")

    source_country_rule = pipeline.get("jp_source_requirement")
    if not isinstance(source_country_rule, dict):
        raise ConfigError("缺少 pipeline.jp_source_requirement")
    source_country = str(source_country_rule.get("country", ""))
    if len(source_country) != 2:
        raise ConfigError("pipeline.jp_source_requirement.country 必须是两个字母")
    _positive_number(
        source_country_rule.get("count"),
        "pipeline.jp_source_requirement.count",
    )
    _positive_number(
        source_country_rule.get("max_test_rounds"),
        "pipeline.jp_source_requirement.max_test_rounds",
    )

    ranges = config["sources"].get("cloudflare_ranges", {})
    _positive_number(ranges.get("official_batch_size"), "sources.cloudflare_ranges.official_batch_size")

    location_filter = pipeline.get("location_filter")
    if not isinstance(location_filter, dict):
        raise ConfigError("缺少 pipeline.location_filter")

    baseline = config.get("network_baseline")
    if not isinstance(baseline, dict) or int(baseline.get("attempts", 0)) != 3:
        raise ConfigError("network_baseline.attempts 必须等于 3")

    top_nodes = config["output"].get("top_nodes")
    _positive_number(top_nodes, "output.top_nodes")
    if int(config["pipeline"]["current_selection"]) != int(top_nodes):
        raise ConfigError("pipeline.current_selection 必须等于 output.top_nodes")
