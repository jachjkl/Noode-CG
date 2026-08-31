from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any

from .io_utils import atomic_write_json, read_json
from .models import NodeResult
from .parser import deduplicate, parse_bytes


def _from_public(value: dict[str, Any]) -> NodeResult | None:
    try:
        ip = str(ipaddress.ip_address(str(value.get("ip", "")).strip().strip("[]")))
        port = int(value.get("port", 443))
        if not 1 <= port <= 65535:
            return None
    except (TypeError, ValueError):
        return None
    return NodeResult(
        ip=ip,
        port=port,
        country_hint=str(value.get("country", "")).upper(),
        country=str(value.get("country", "")).upper(),
        colo=str(value.get("colo", "")).upper(),
        colo_country=str(value.get("colo_country", "")).upper(),
        region=str(value.get("region", "")),
        city=str(value.get("city", "")),
        tcp_latency_ms=value.get("tcp_latency_ms"),
        tls_latency_ms=value.get("tls_latency_ms"),
        http_latency_ms=value.get("http_latency_ms"),
        average_latency_ms=value.get("average_latency_ms"),
        tcp_jitter_ms=value.get("tcp_jitter_ms", value.get("jitter_ms")),
        tls_jitter_ms=value.get("tls_jitter_ms"),
        http_jitter_ms=value.get("http_jitter_ms"),
        overall_jitter_ms=value.get("jitter_ms", value.get("overall_jitter_ms")),
        tcp_loss_rate=float(value.get("loss_rate", value.get("tcp_loss_rate", 1.0))),
        speed_mbps=value.get("speed_mbps"),
        score=float(value.get("score", 0.0)),
    )


def _load_json_records(path: Path) -> list[NodeResult]:
    payload = read_json(path, [])
    if not isinstance(payload, list):
        raise ValueError("TOP100 文件必须是 JSON 数组")
    return [node for item in payload if isinstance(item, dict) and (node := _from_public(item)) is not None]


def load_previous_top(
    output_dir: str | Path,
    snapshot_path: str | Path | None,
    limit: int,
) -> tuple[list[NodeResult], list[str]]:
    output = Path(output_dir)
    warnings: list[str] = []
    candidates = [output / "nodes.json"]
    if snapshot_path is not None:
        candidates.append(Path(snapshot_path))
    for path in candidates:
        if not path.is_file():
            continue
        try:
            records = deduplicate(_load_json_records(path))
            if records:
                return records[: max(0, limit)], warnings
        except (OSError, ValueError) as exc:
            warnings.append(f"读取上一轮 TOP100 失败 {path}: {exc}")

    text_path = output / "nodes.txt"
    if text_path.is_file():
        try:
            records = deduplicate(parse_bytes(text_path.name, text_path.read_bytes(), source="previous-output"))
            return records[: max(0, limit)], warnings
        except OSError as exc:
            warnings.append(f"读取上一轮地址列表失败 {text_path}: {exc}")
    return [], warnings


def save_previous_top(path: str | Path, records: list[NodeResult]) -> None:
    atomic_write_json(path, [record.to_dict() for record in records])


def prepare_retest_candidates(
    current_selected: list[NodeResult],
    previous: list[NodeResult],
) -> list[NodeResult]:
    fresh: list[NodeResult] = []
    seen_ips: set[str] = set()
    for original, source in [
        *((node, "current-top500") for node in current_selected),
        *((node, "previous-top100") for node in previous),
    ]:
        if original.ip in seen_ips:
            continue
        node = NodeResult(ip=original.ip, port=original.port, country_hint=original.country or original.country_hint)
        node.add_source(source)
        fresh.append(node)
        seen_ips.add(node.ip)
    return fresh
