from __future__ import annotations

import ipaddress
from collections import Counter
from typing import Any

from .isp_test import probe_summary
from .models import NodeResult


def _lower_better(value: float | None, cap: float) -> float:
    if value is None:
        return 0.0
    return max(0.0, min(1.0, 1.0 - float(value) / cap))


def _higher_better(value: float | None, cap: float) -> float:
    if value is None:
        return 0.0
    return max(0.0, min(1.0, float(value) / cap))


def score_nodes(records: list[NodeResult], options: dict[str, Any]) -> list[NodeResult]:
    weights = {key: float(value) for key, value in options["weights"].items()}
    caps = options["caps"]
    priorities = {str(key).upper(): float(value) for key, value in options.get("region_priority", {}).items()}
    default_region = float(options.get("default_region_score", 0.5))

    for node in records:
        latency = node.http_latency_ms or node.tls_latency_ms or node.tcp_latency_ms
        protocol = (float(node.tcp_ok) + float(node.tls_ok) + float(node.http_ok)) / 3
        if node.websocket_ok is not None:
            protocol = (protocol * 3 + float(node.websocket_ok)) / 4
        probe = probe_summary(node)
        if probe["count"]:
            protocol *= probe["reachable"] / probe["count"]
        parts = {
            "latency": _lower_better(latency, float(caps["latency_ms"])),
            "jitter": _lower_better(node.tcp_jitter_ms, float(caps["jitter_ms"])),
            "loss": max(0.0, min(1.0, 1.0 - node.tcp_loss_rate)),
            "speed": _higher_better(node.speed_mbps, float(caps["speed_mbps"])),
            "region": priorities.get((node.country or node.country_hint).upper(), default_region),
            "protocol": protocol,
        }
        node.score = round(sum(parts[key] * weights.get(key, 0.0) for key in parts) * 1000, 3)
    return sorted(records, key=lambda node: (-node.score, node.http_latency_ms or 999999, node.ip, node.port))


def _prefix(node: NodeResult, ipv4_prefix: int, ipv6_prefix: int) -> str:
    address = ipaddress.ip_address(node.ip)
    bits = ipv4_prefix if address.version == 4 else ipv6_prefix
    return str(ipaddress.ip_network(f"{address}/{bits}", strict=False))


def _country(node: NodeResult) -> str:
    return (node.country or node.country_hint or "XX").upper()


def select_diverse(records: list[NodeResult], output: dict[str, Any]) -> list[NodeResult]:
    limit = int(output.get("top_nodes", 300))
    country_limit = int(output.get("max_per_country", limit))
    ipv4_prefix = int(output.get("max_per_ipv4_24", 4))
    ipv6_prefix = int(output.get("max_per_ipv6_48", 4))
    minimums = {
        str(country).upper(): max(0, int(count))
        for country, count in output.get("minimum_per_country", {}).items()
    }
    country_counts: Counter[str] = Counter()
    prefix_counts: Counter[str] = Counter()
    selected: list[NodeResult] = []
    selected_keys: set[str] = set()

    def add(node: NodeResult, *, enforce_caps: bool = True) -> bool:
        if node.key in selected_keys or len(selected) >= limit:
            return False
        country = _country(node)
        prefix = _prefix(node, 24, 48)
        max_prefix = ipv4_prefix if ":" not in node.ip else ipv6_prefix
        if enforce_caps and (country_counts[country] >= country_limit or prefix_counts[prefix] >= max_prefix):
            return False
        selected.append(node)
        selected_keys.add(node.key)
        country_counts[country] += 1
        prefix_counts[prefix] += 1
        return True

    # Reserve only explicitly configured countries. This keeps the original V2
    # ranking/diversity behaviour while preventing the US-hosted runner from
    # crowding every Japanese candidate out of the final list.
    for wanted_country, wanted_count in minimums.items():
        target = min(wanted_count, country_limit, limit)
        for node in records:
            if country_counts[wanted_country] >= target:
                break
            if _country(node) == wanted_country:
                add(node)

    for node in records:
        add(node)
        if len(selected) >= limit:
            break

    # If diversity caps leave fewer than TOP N, fill from the remaining verified
    # records. Quality checks remain mandatory; only diversity is relaxed.
    for node in records:
        add(node, enforce_caps=False)
        if len(selected) >= limit:
            break

    rank = {node.key: index for index, node in enumerate(records)}
    return sorted(selected, key=lambda node: rank.get(node.key, len(records)))
