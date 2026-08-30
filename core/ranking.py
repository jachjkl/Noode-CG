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
    return node.average_latency_ms


def rank_final(records: Iterable[NodeResult], *, count: int) -> list[NodeResult]:
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
            node.ip,
            node.port,
        ),
    )
    selected: list[NodeResult] = []
    selected_ips: set[str] = set()
    for node in ordered:
        if node.ip in selected_ips:
            continue
        selected.append(node)
        selected_ips.add(node.ip)
        if len(selected) >= count:
            break
    return selected
