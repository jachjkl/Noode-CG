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


def filter_by_average_latency(records: Iterable[NodeResult], *, maximum_ms: float) -> list[NodeResult]:
    eligible: list[NodeResult] = []
    for node in records:
        average = calculate_average_latency(node)
        if average is not None and average <= maximum_ms:
            eligible.append(node)
    return sorted(eligible, key=lambda node: (node.average_latency_ms or math.inf, node.ip, node.port))


def select_latency_shortlist(records: Iterable[NodeResult], *, count: int) -> list[NodeResult]:
    prepared: list[NodeResult] = []
    for node in records:
        if node.average_latency_ms is None:
            calculate_average_latency(node)
        if node.average_latency_ms is not None:
            prepared.append(node)
    return sorted(prepared, key=lambda node: (node.average_latency_ms or math.inf, node.ip, node.port))[:count]


def rank_final(records: Iterable[NodeResult], *, count: int) -> list[NodeResult]:
    prepared = list(records)
    for node in prepared:
        if node.average_latency_ms is None:
            calculate_average_latency(node)
    return sorted(
        prepared,
        key=lambda node: (
            node.tcp_loss_rate,
            -(node.speed_mbps if node.speed_mbps is not None else -1.0),
            node.average_latency_ms if node.average_latency_ms is not None else math.inf,
            node.ip,
            node.port,
        ),
    )[:count]
