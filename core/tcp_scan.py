from __future__ import annotations

import asyncio
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
    require_all = bool(options.get("require_all_attempts", False))
    maximum_raw = options.get("maximum_attempt_latency_ms")
    maximum_latency = float(maximum_raw) if maximum_raw is not None else None

    async def worker(node: NodeResult) -> NodeResult:
        measurements: list[float] = []
        last_error: BaseException | None = None
        for _ in range(attempts):
            try:
                measurements.append(await _probe_once(node, timeout))
            except Exception as exc:
                last_error = exc
        node.tcp_loss_rate = round(1 - len(measurements) / attempts, 4)
        if measurements:
            node.tcp_latency_ms = round(statistics.median(measurements), 3)
            node.tcp_jitter_ms = round(statistics.pstdev(measurements), 3) if len(measurements) > 1 else 0.0
        all_succeeded = len(measurements) == attempts
        all_within_limit = maximum_latency is None or all(value <= maximum_latency for value in measurements)
        node.tcp_ok = bool(measurements) and (all_succeeded or not require_all) and all_within_limit
        node.probe_results["tcp"] = {
            "attempts": attempts,
            "successes": len(measurements),
            "latencies_ms": [round(value, 3) for value in measurements],
            "maximum_attempt_latency_ms": maximum_latency,
            "strict_passed": node.tcp_ok,
        }
        if not node.tcp_ok and last_error:
            node.add_error("tcp", last_error)
        elif not node.tcp_ok and not all_within_limit:
            node.add_error("tcp", f"至少一次延迟超过 {maximum_latency:g}ms")
        elif not node.tcp_ok:
            node.add_error("tcp", f"只成功 {len(measurements)}/{attempts} 次")
        return node

    tested = await run_worker_pool(
        records,
        worker,
        int(options.get("concurrency", 800)),
        progress_every=max(1000, len(records) // 20),
        progress_label="TCP",
    )
    return [node for node in tested if node.tcp_ok]
