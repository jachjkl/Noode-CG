from __future__ import annotations

from pathlib import Path
from typing import Any

from .io_utils import read_json
from .models import NodeResult

PLATFORM_NAMES = {"x", "telegram", "youtube", "huggingface", "github", "google", "chatgpt", "civitai"}


def merge_probe_files(records: list[NodeResult], probe_paths: list[str | Path]) -> list[NodeResult]:
    by_key = {node.key: node for node in records}
    for configured in probe_paths:
        payload = read_json(configured, default={})
        if isinstance(payload, dict) and isinstance(payload.get("nodes"), list):
            vantage = str(payload.get("vantage") or Path(configured).stem)
            values = payload["nodes"]
        elif isinstance(payload, list):
            vantage = Path(configured).stem
            values = payload
        else:
            continue
        for value in values:
            if not isinstance(value, dict):
                continue
            ip = str(value.get("ip", ""))
            port = int(value.get("port", 443))
            node = by_key.get(f"{ip}|{port}")
            if node:
                safe_probe = {
                    key: item
                    for key, item in value.items()
                    if key in {"tcp_ok", "latency_ms", "jitter_ms", "loss_rate", "speed_mbps"}
                }
                platforms = value.get("platforms")
                if isinstance(platforms, dict):
                    safe_probe["platforms"] = {
                        str(key).strip().lower(): item
                        for key, item in platforms.items()
                        if str(key).strip().lower() in PLATFORM_NAMES and isinstance(item, bool)
                    }
                node.probe_results[vantage] = safe_probe
    return records


def apply_platform_policy(records: list[NodeResult], options: dict[str, Any]) -> list[NodeResult]:
    required = [str(value).strip().lower() for value in options.get("required_platforms", []) if str(value).strip()]
    minimum_vantages = max(1, int(options.get("minimum_vantages", 1)))
    for node in records:
        values_by_platform: dict[str, list[bool]] = {name: [] for name in required}
        passing_vantages = 0
        for probe in node.probe_results.values():
            platforms = probe.get("platforms") if isinstance(probe, dict) else None
            if not isinstance(platforms, dict):
                continue
            normalised = {str(key).strip().lower(): value for key, value in platforms.items() if isinstance(value, bool)}
            for name in required:
                if name in normalised:
                    values_by_platform[name].append(normalised[name])
            if required and all(normalised.get(name) is True for name in required):
                passing_vantages += 1

        node.platform_results = {
            name: (all(values) if values else None) for name, values in values_by_platform.items()
        }
        known = [value for value in node.platform_results.values() if value is not None]
        node.platform_score = sum(value is True for value in known) / len(required) if required else 0.0
        node.platform_ok = passing_vantages >= minimum_vantages if known else None

    if options.get("required", False):
        return [node for node in records if node.platform_ok is True]
    return records


def probe_summary(node: NodeResult) -> dict[str, Any]:
    probes = list(node.probe_results.values())
    if not probes:
        return {"count": 0, "reachable": 0, "latency_ms": None, "loss_rate": None}
    latencies = [float(probe["latency_ms"]) for probe in probes if isinstance(probe.get("latency_ms"), (int, float))]
    losses = [float(probe["loss_rate"]) for probe in probes if isinstance(probe.get("loss_rate"), (int, float))]
    return {
        "count": len(probes),
        "reachable": sum(1 for probe in probes if probe.get("tcp_ok")),
        "latency_ms": sum(latencies) / len(latencies) if latencies else None,
        "loss_rate": sum(losses) / len(losses) if losses else None,
    }
