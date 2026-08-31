from __future__ import annotations

import math
import statistics
from collections.abc import Iterable

from .models import NodeResult


def calculate_average_latency(node: NodeResult) -> float | None:
    values = (node.tcp_latency_ms, node.tls_latency_ms, node.http_latency_ms)
    if any(value is None for value in values):
        node.average_latency_ms = None
        return None
    node.average_latency_ms = round(statistics.fmean(float(value) for value in values), 3)
    jitters = [
        value
        for value in (node.tcp_jitter_ms, node.tls_jitter_ms, node.http_jitter_ms)
        if value is not None
    ]
    node.overall_jitter_ms = round(max(jitters), 3) if jitters else None
    return node.average_latency_ms


def rank_final(
    records: Iterable[NodeResult],
    *,
    count: int,
    minimum_by_country: dict[str, int] | None = None,
) -> list[NodeResult]:
    prepared = list(records)
    for node in prepared:
        if node.average_latency_ms is None:
            calculate_average_latency(node)
    ordered = sorted(
        prepared,
        key=lambda node: (
            node.tcp_loss_rate,
            -(node.speed_mbps if node.speed_mbps is not None else -1.0),
            node.average_latency_ms if node.average_latency_ms is not None else math.inf,
            node.overall_jitter_ms if node.overall_jitter_ms is not None else math.inf,
            node.ip,
            node.port,
        ),
    )
    unique: list[NodeResult] = []
    selected_ips: set[str] = set()
    for node in ordered:
        if node.ip in selected_ips:
            continue
        unique.append(node)
        selected_ips.add(node.ip)

    required: set[str] = set()
    for country, minimum in (minimum_by_country or {}).items():
        normalized = str(country).upper()
        matches = [
            node
            for node in unique
            if (node.country or node.country_hint).upper() == normalized
        ]
        required.update(node.ip for node in matches[: max(0, int(minimum))])

    chosen: set[str] = set(required)
    for node in unique:
        if len(chosen) >= count:
            break
        chosen.add(node.ip)
    return [node for node in unique if node.ip in chosen][:count]
