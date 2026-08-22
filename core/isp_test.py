from __future__ import annotations

from pathlib import Path
from typing import Any

from .io_utils import read_json
from .models import NodeResult


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
                node.probe_results[vantage] = {
                    key: item
                    for key, item in value.items()
                    if key in {"tcp_ok", "latency_ms", "jitter_ms", "loss_rate", "speed_mbps"}
                }
    return records


def probe_summary(node: NodeResult) -> dict[str, Any]:
    probes = list(node.probe_results.values())
    if not probes:
        return {"count": 0, "reachable": 0}
    return {
        "count": len(probes),
        "reachable": sum(1 for probe in probes if probe.get("tcp_ok")),
    }
