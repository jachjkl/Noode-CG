from __future__ import annotations

import asyncio
import math
import statistics
import time
from typing import Any

from .async_utils import run_worker_pool
from .models import NodeResult


async def _probe_once(node: NodeResult, timeout: float) -> float:
    started = time.perf_counter()
    writer = None
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(node.ip, node.port),
            timeout=timeout,
        )
        return (time.perf_counter() - started) * 1000
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


async def scan_tcp(records: list[NodeResult], options: dict[str, Any]) -> list[NodeResult]:
    timeout = float(options.get("timeout_seconds", 1.5))
    attempts = max(1, int(options.get("attempts", 1)))
    minimum_success_ratio = max(0.0, min(1.0, float(options.get("minimum_success_ratio", 0.0))))
    maximum_p95_ms = float(options.get("maximum_p95_ms", 0.0))

    async def worker(node: NodeResult) -> NodeResult:
        measurements: list[float] = []
        last_error: BaseException | None = None
        for _ in range(attempts):
            try:
                measurements.append(await _probe_once(node, timeout))
            except Exception as exc:
                last_error = exc
        success_ratio = len(measurements) / attempts
        node.tcp_loss_rate = round(1 - len(measurements) / attempts, 4)
        if measurements:
            node.tcp_latency_ms = round(statistics.median(measurements), 3)
            ordered = sorted(measurements)
            p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
            node.tcp_p95_latency_ms = round(ordered[p95_index], 3)
            node.tcp_jitter_ms = round(statistics.pstdev(measurements), 3) if len(measurements) > 1 else 0.0
        ratio_ok = bool(measurements) and success_ratio >= minimum_success_ratio
        latency_ok = not maximum_p95_ms or (
            node.tcp_p95_latency_ms is not None and node.tcp_p95_latency_ms <= maximum_p95_ms
        )
        node.tcp_ok = ratio_ok and latency_ok
        if not node.tcp_ok and measurements:
            node.add_error(
                "tcp-stability",
                f"success={len(measurements)}/{attempts}, p95={node.tcp_p95_latency_ms}",
            )
        elif not measurements and last_error:
            node.add_error("tcp", last_error)
        return node

    tested = await run_worker_pool(
        records,
        worker,
        int(options.get("concurrency", 800)),
        progress_every=max(1000, len(records) // 20),
        progress_label="TCP",
    )
    return [node for node in tested if node.tcp_ok]
