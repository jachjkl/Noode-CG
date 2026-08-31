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
    stop_on_failure = bool(options.get("stop_on_failure", False))
    stop_when_impossible = bool(options.get("stop_when_average_impossible", False))
    maximum_raw = options.get("maximum_average_latency_ms")
    maximum_latency = float(maximum_raw) if maximum_raw is not None else None

    async def worker(node: NodeResult) -> NodeResult:
        measurements: list[float] = []
        last_error: BaseException | None = None
        early_reason = ""
        for _ in range(attempts):
            try:
                measurements.append(await _probe_once(node, timeout))
            except Exception as exc:
                last_error = exc
                if require_all and stop_on_failure:
                    early_reason = "必需测试失败，提前终止剩余尝试"
                    break
            if (
                stop_when_impossible
                and maximum_latency is not None
                and sum(measurements) > maximum_latency * attempts
            ):
                early_reason = "即使剩余延迟为 0，测试平均值也会超限"
                break
        node.tcp_loss_rate = round(1 - len(measurements) / attempts, 4)
        if measurements:
            node.tcp_latency_ms = round(statistics.fmean(measurements), 3)
            node.tcp_jitter_ms = round(statistics.pstdev(measurements), 3) if len(measurements) > 1 else 0.0
        all_succeeded = len(measurements) == attempts
        average_within_limit = maximum_latency is None or (
            node.tcp_latency_ms is not None and node.tcp_latency_ms <= maximum_latency
        )
        node.tcp_ok = bool(measurements) and (all_succeeded or not require_all) and average_within_limit
        node.probe_results["tcp"] = {
            "attempts": attempts,
            "successes": len(measurements),
            "latencies_ms": [round(value, 3) for value in measurements],
            "average_ms": node.tcp_latency_ms,
            "maximum_average_latency_ms": maximum_latency,
            "early_rejected": bool(early_reason),
            "strict_passed": node.tcp_ok,
        }
        if early_reason:
            node.add_error("tcp", early_reason)
        if not node.tcp_ok and last_error:
            node.add_error("tcp", last_error)
        elif not node.tcp_ok and not average_within_limit:
            node.add_error("tcp", f"测试平均延迟 {node.tcp_latency_ms:g}ms > {maximum_latency:g}ms")
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
