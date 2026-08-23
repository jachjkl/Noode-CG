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
        p95_values = [value for value in (node.tcp_p95_latency_ms, node.http_p95_latency_ms) if value is not None]
        p95_latency = max(p95_values) if p95_values else latency
        protocol = (float(node.tcp_ok) + float(node.tls_ok) + float(node.http_ok)) / 3
        if node.websocket_ok is not None:
            protocol = (protocol * 3 + float(node.websocket_ok)) / 4
        probe = probe_summary(node)
        if probe["count"]:
            protocol *= probe["reachable"] / probe["count"]
        external = 0.5
        if probe["latency_ms"] is not None or probe["loss_rate"] is not None:
            external_latency = _lower_better(probe["latency_ms"], float(caps["latency_ms"]))
            external_loss = 1.0 - float(probe["loss_rate"] or 0.0)
            external = max(0.0, min(1.0, (external_latency + external_loss) / 2))
        parts = {
            "latency": _lower_better(latency, float(caps["latency_ms"])),
            "p95": _lower_better(p95_latency, float(caps.get("p95_latency_ms", caps["latency_ms"]))),
            "jitter": _lower_better(node.tcp_jitter_ms, float(caps["jitter_ms"])),
            "loss": max(0.0, min(1.0, 1.0 - node.tcp_loss_rate)),
            "speed": _higher_better(node.speed_mbps, float(caps["speed_mbps"])),
            "completion": max(0.0, min(1.0, node.speed_completion_rate)),
            "history": max(0.0, min(1.0, node.history_score)),
            "region": priorities.get(node.endpoint_country, default_region),
            "protocol": protocol,
            "external": external,
        }
        node.score = round(sum(parts[key] * weights.get(key, 0.0) for key in parts) * 1000, 3)
    return sorted(
        records,
        key=lambda node: (
            -node.score,
            node.http_p95_latency_ms or node.http_latency_ms or 999999,
            node.ip,
            node.port,
        ),
    )


def _prefix(node: NodeResult, ipv4_prefix: int, ipv6_prefix: int) -> str:
    address = ipaddress.ip_address(node.ip)
    bits = ipv4_prefix if address.version == 4 else ipv6_prefix
    return str(ipaddress.ip_network(f"{address}/{bits}", strict=False))


def select_diverse(records: list[NodeResult], output: dict[str, Any]) -> list[NodeResult]:
    limit = int(output.get("top_nodes", 300))
    country_limit = int(output.get("max_per_country", limit))
    region_limit = int(output.get("max_per_region", limit))
    colo_limit = int(output.get("max_per_colo", limit))
    official_limit = int(output.get("max_official_generated", limit))
    minimum_per_colo = {str(key).upper(): int(value) for key, value in output.get("minimum_per_colo", {}).items()}
    minimum_per_country = {
        str(key).upper(): int(value) for key, value in output.get("minimum_per_country", {}).items()
    }
    ipv4_prefix = int(output.get("max_per_ipv4_24", 4))
    ipv6_prefix = int(output.get("max_per_ipv6_48", 4))
    country_counts: Counter[str] = Counter()
    region_counts: Counter[str] = Counter()
    colo_counts: Counter[str] = Counter()
    prefix_counts: Counter[str] = Counter()
    selected: list[NodeResult] = []
    selected_keys: set[str] = set()
    official_count = 0

    def add(node: NodeResult, *, enforce_diversity: bool) -> bool:
        nonlocal official_count
        if node.key in selected_keys or len(selected) >= limit:
            return False
        if node.official_only and official_count >= official_limit:
            return False
        country = node.endpoint_country
        region = node.region or "Unknown"
        colo = node.colo or "Unknown"
        prefix = _prefix(node, 24, 48)
        max_prefix = ipv4_prefix if ":" not in node.ip else ipv6_prefix
        if enforce_diversity and (
            country_counts[country] >= country_limit
            or region_counts[region] >= region_limit
            or colo_counts[colo] >= colo_limit
            or prefix_counts[prefix] >= max_prefix
        ):
            return False
        selected.append(node)
        selected_keys.add(node.key)
        country_counts[country] += 1
        region_counts[region] += 1
        colo_counts[colo] += 1
        prefix_counts[prefix] += 1
        if node.official_only:
            official_count += 1
        return True

    # Hard reservations are selected before the global ranking can fill every
    # slot with endpoints that are only fast from a US GitHub runner.
    for colo, minimum in minimum_per_colo.items():
        added = 0
        for node in records:
            if node.colo == colo and add(node, enforce_diversity=True):
                added += 1
                if added >= minimum:
                    break
    for country, minimum in minimum_per_country.items():
        already = country_counts[country]
        for node in records:
            if already >= minimum:
                break
            if node.endpoint_country == country and add(node, enforce_diversity=True):
                already += 1

    for node in records:
        add(node, enforce_diversity=True)
        if len(selected) >= limit:
            return selected

    # Relax only subnet concentration. Country, region and colo caps remain hard;
    # returning fewer good regions is safer than filling the feed with LAX nodes.
    for node in records:
        if node.key in selected_keys or (node.official_only and official_count >= official_limit):
            continue
        country = node.endpoint_country
        region = node.region or "Unknown"
        colo = node.colo or "Unknown"
        if (
            country_counts[country] >= country_limit
            or region_counts[region] >= region_limit
            or colo_counts[colo] >= colo_limit
        ):
            continue
        add(node, enforce_diversity=False)
        if len(selected) >= limit:
            return selected

    # Region is the final cap that may be relaxed; country and colo never are.
    for node in records:
        country = node.endpoint_country
        colo = node.colo or "Unknown"
        if country_counts[country] >= country_limit or colo_counts[colo] >= colo_limit:
            continue
        add(node, enforce_diversity=False)
        if len(selected) >= limit:
            return selected
    return selected
