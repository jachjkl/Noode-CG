from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?}")


class ConfigError(ValueError):
    pass


LOCAL_RULE_DEFAULTS: dict[str, float] = {
    "tcp_max_ms": 200.0,
    "tls_max_ms": 200.0,
    "http_ttfb_max_ms": 200.0,
    "average_max_ms": 200.0,
    "jitter_max_ms": 200.0,
    "loss_max_percent": 30.0,
    "speed_min_mbps": 3.0,
}

LOCAL_OPTION_DEFAULTS: dict[str, bool] = {
    "continuous_three_rounds": True,
}


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return ENV_PATTERN.sub(lambda match: os.getenv(match.group(1), match.group(2) or ""), value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def _load_local_rules(config_path: Path) -> tuple[dict[str, float], Path]:
    local_root = os.getenv("NOODE_LOCAL_ROOT", "").strip()
    rules_path = (
        Path(local_root) / "app" / "data" / "local-rules.json"
        if local_root
        else config_path.parent / "data" / "local-rules.json"
    )
    rules = dict(LOCAL_RULE_DEFAULTS)
    if not rules_path.is_file():
        return rules, rules_path
    try:
        payload = json.loads(rules_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"本地自定义规则无法读取: {exc}") from exc
    values = payload.get("ordinary", payload) if isinstance(payload, dict) else None
    if not isinstance(values, dict):
        raise ConfigError("本地自定义规则必须是对象")
    for name, default in LOCAL_RULE_DEFAULTS.items():
        raw = values.get(name, default)
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            raise ConfigError(f"本地自定义规则 {name} 必须是数字")
        rules[name] = float(raw)
    for name in ("tcp_max_ms", "tls_max_ms", "http_ttfb_max_ms", "average_max_ms"):
        if rules[name] <= 0:
            raise ConfigError(f"本地自定义规则 {name} 必须大于 0")
    if rules["jitter_max_ms"] < 0:
        raise ConfigError("本地自定义规则 jitter_max_ms 不能小于 0")
    if not 0 <= rules["loss_max_percent"] <= 100:
        raise ConfigError("本地自定义规则 loss_max_percent 必须在 0 到 100 之间")
    if rules["speed_min_mbps"] < 0:
        raise ConfigError("本地自定义规则 speed_min_mbps 不能小于 0")
    return rules, rules_path


def _apply_local_rules(data: dict[str, Any], config_path: Path) -> None:
    rules, rules_path = _load_local_rules(config_path)
    pipeline = data.get("pipeline")
    if not isinstance(pipeline, dict):
        return
    quality_tcp = pipeline.get("quality_tcp")
    tls = pipeline.get("tls")
    http = pipeline.get("http")
    speed = pipeline.get("speed")
    if not all(isinstance(block, dict) for block in (quality_tcp, tls, http, speed)):
        return
    quality_tcp["maximum_average_latency_ms"] = rules["tcp_max_ms"]
    quality_tcp["maximum_jitter_ms"] = rules["jitter_max_ms"]
    quality_tcp["maximum_loss_rate"] = rules["loss_max_percent"] / 100.0
    tls["maximum_average_latency_ms"] = rules["tls_max_ms"]
    tls["maximum_jitter_ms"] = rules["jitter_max_ms"]
    http["maximum_average_ttfb_ms"] = rules["http_ttfb_max_ms"]
    http["maximum_jitter_ms"] = rules["jitter_max_ms"]
    pipeline["maximum_combined_latency_ms"] = rules["average_max_ms"]
    pipeline["maximum_component_latency_ms"] = max(
        rules["tcp_max_ms"], rules["tls_max_ms"], rules["http_ttfb_max_ms"]
    )
    pipeline["maximum_jitter_ms"] = rules["jitter_max_ms"]
    pipeline["maximum_loss_rate"] = rules["loss_max_percent"] / 100.0
    speed["minimum_mbps"] = rules["speed_min_mbps"]
    data["_local_rules"] = rules
    data["_local_rules_path"] = str(rules_path)


def _apply_local_options(data: dict[str, Any], config_path: Path) -> None:
    local_root = os.getenv("NOODE_LOCAL_ROOT", "").strip()
    options_path = (
        Path(local_root) / "local-options.json"
        if local_root
        else config_path.parent / "data" / "local-options.json"
    )
    options = dict(LOCAL_OPTION_DEFAULTS)
    if options_path.is_file():
        try:
            payload = json.loads(options_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"本地运行选项无法读取: {exc}") from exc
        values = payload.get("selection", payload) if isinstance(payload, dict) else None
        if not isinstance(values, dict):
            raise ConfigError("本地运行选项必须是对象")
        for name, default in LOCAL_OPTION_DEFAULTS.items():
            raw = values.get(name, default)
            if not isinstance(raw, bool):
                raise ConfigError(f"本地运行选项 {name} 必须是布尔值")
            options[name] = raw
    data["_local_options"] = options
    data["_local_options_path"] = str(options_path)


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path).resolve()
    if not config_path.is_file():
        raise ConfigError(f"配置文件不存在: {config_path}")
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ConfigError("配置文件根节点必须是对象")
    data = _expand_env(data)
    _apply_local_rules(data, config_path)
    _apply_local_options(data, config_path)
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
    for section in ("project", "paths", "sources", "pipeline", "handoff", "output"):
        if section not in config or not isinstance(config[section], dict):
            raise ConfigError(f"缺少配置段: {section}")

    domain = str(config["project"].get("target_domain", "")).strip()
    if not domain or "://" in domain or "/" in domain:
        raise ConfigError("project.target_domain 必须是纯域名，不能包含协议或路径")

    pipeline = config["pipeline"]
    source_priority = pipeline.get("source_priority", [])
    if not isinstance(source_priority, list) or any(
        not isinstance(value, str) or not value.strip() for value in source_priority
    ):
        raise ConfigError("pipeline.source_priority 必须是非空字符串列表")
    if len(source_priority) != len(set(source_priority)):
        raise ConfigError("pipeline.source_priority 不能包含重复来源")
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
    _positive_number(
        pipeline.get("maximum_loss_rate"),
        "pipeline.maximum_loss_rate",
        allow_zero=True,
    )
    if float(pipeline["maximum_loss_rate"]) > 1:
        raise ConfigError("pipeline.maximum_loss_rate 不能大于 1")
    for stage in ("prefilter_tcp", "quality_tcp", "tls", "http"):
        block = pipeline.get(stage)
        if not isinstance(block, dict):
            raise ConfigError(f"缺少 pipeline.{stage}")
        _positive_number(block.get("concurrency"), f"pipeline.{stage}.concurrency")
        _positive_number(block.get("timeout_seconds"), f"pipeline.{stage}.timeout_seconds")
        _positive_number(
            block.get("maximum_jitter_ms"),
            f"pipeline.{stage}.maximum_jitter_ms",
            allow_zero=True,
        )
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
    expected_attempts = {"prefilter_tcp": 3, "quality_tcp": 5, "tls": 3, "http": 3}
    for stage, attempts in expected_attempts.items():
        if int(pipeline[stage].get("attempts", 0)) != attempts:
            raise ConfigError(f"pipeline.{stage}.attempts 必须等于 {attempts}")
    for stage in ("prefilter_tcp", "tls", "http"):
        if pipeline[stage].get("require_all_attempts") is not True:
            raise ConfigError(f"pipeline.{stage}.require_all_attempts 必须为 true")
    if pipeline["quality_tcp"].get("require_all_attempts") is not False:
        raise ConfigError("pipeline.quality_tcp.require_all_attempts 必须为 false，以便计算丢包率")
    if pipeline["quality_tcp"].get("stop_on_failure") is not False:
        raise ConfigError("pipeline.quality_tcp.stop_on_failure 必须为 false")
    if pipeline["quality_tcp"].get("stop_when_average_impossible") is not False:
        raise ConfigError("pipeline.quality_tcp.stop_when_average_impossible 必须为 false")

    speed = pipeline.get("speed")
    if not isinstance(speed, dict):
        raise ConfigError("缺少 pipeline.speed")
    for name in ("candidates", "concurrency", "timeout_seconds", "bytes_per_test"):
        _positive_number(speed.get(name), f"pipeline.speed.{name}")
    _positive_number(speed.get("minimum_mbps"), "pipeline.speed.minimum_mbps", allow_zero=True)
    _positive_number(speed.get("maximum_download_seconds"), "pipeline.speed.maximum_download_seconds")
    if int(pipeline["speed_batch_size"]) > int(pipeline["prefilter_shortlist"]):
        raise ConfigError("pipeline.speed_batch_size 不能大于 prefilter_shortlist")
    if float(pipeline["prefilter_tcp"]["maximum_average_latency_ms"]) != 1000:
        raise ConfigError("pipeline.prefilter_tcp.maximum_average_latency_ms 必须等于 1000")

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
        source_country_rule.get("tcp_attempts"),
        "pipeline.jp_source_requirement.tcp_attempts",
    )
    if int(source_country_rule.get("tcp_attempts", 0)) != 3:
        raise ConfigError("pipeline.jp_source_requirement.tcp_attempts 必须等于 3")

    ranges = config["sources"].get("cloudflare_ranges", {})
    _positive_number(ranges.get("official_batch_size"), "sources.cloudflare_ranges.official_batch_size")

    handoff = config["handoff"]
    for name in ("target", "max_official_rounds", "max_per_colo", "max_replenishment_rounds"):
        _positive_number(handoff.get(name), f"handoff.{name}")
    for name in ("pool_path", "health_path", "accumulator_path", "attempted_path"):
        if not str(handoff.get(name, "")).strip():
            raise ConfigError(f"handoff.{name} 不能为空")
    if int(handoff["target"]) != int(ranges["official_batch_size"]):
        raise ConfigError("handoff.target 必须等于官方候选批次数量")
    if int(handoff["max_replenishment_rounds"]) != 3:
        raise ConfigError("handoff.max_replenishment_rounds 必须等于 3")

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
