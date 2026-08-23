from __future__ import annotations

import asyncio
import time
from typing import Any

from .async_utils import run_worker_pool
from .models import NodeResult
from .tls_check import make_ssl_context


async def test_speed(records: list[NodeResult], options: dict[str, Any], *, user_agent: str) -> list[NodeResult]:
    if not options.get("enabled", True) or not records:
        return records
    count = min(len(records), int(options.get("candidates", 600)))
    ordered = sorted(
        records,
        key=lambda node: (
            node.tcp_loss_rate,
            -node.history_score,
            node.http_p95_latency_ms or node.http_latency_ms or 999999,
            node.tcp_p95_latency_ms or node.tcp_latency_ms or 999999,
        ),
    )
    targets = ordered[:count]
    domain = str(options.get("domain", "speed.cloudflare.com"))
    path = str(options.get("path", "/__down?bytes=1048576"))
    wanted = max(1, int(options.get("bytes_per_test", 1024 * 1024)))
    timeout = float(options.get("timeout_seconds", 10.0))
    minimum_mbps = max(0.0, float(options.get("minimum_mbps", 0.0)))
    minimum_completion = max(0.0, min(1.0, float(options.get("minimum_completion_ratio", 0.95))))
    concurrency = int(options.get("concurrency", 2))
    batch_size = max(concurrency, int(options.get("batch_size", 150)))
    target_qualified = max(1, int(options.get("target_qualified", len(targets))))
    context = make_ssl_context(True, "TLSv1.2")

    async def worker(node: NodeResult) -> NodeResult:
        node.speed_tested = True
        try:
            received, elapsed = await _probe_speed(
                node,
                domain=domain,
                path=path,
                wanted=wanted,
                timeout=timeout,
                user_agent=user_agent,
                context=context,
            )
            node.speed_test_bytes = received
            node.speed_test_seconds = round(elapsed, 3)
            node.speed_completion_rate = round(min(1.0, received / wanted), 4)
            node.speed_mbps = round(received * 8 / elapsed / 1_000_000, 3) if received else None
            node.speed_ok = (
                node.speed_completion_rate >= minimum_completion
                and node.speed_mbps is not None
                and node.speed_mbps >= minimum_mbps
            )
            if not node.speed_ok:
                node.add_error(
                    "speed-quality",
                    f"completion={node.speed_completion_rate:.1%}, speed={node.speed_mbps}Mbps",
                )
        except Exception as exc:
            node.speed_ok = False
            node.speed_mbps = None
            node.speed_completion_rate = 0.0
            node.add_error("speed", exc)
        return node

    tested_count = 0
    for start in range(0, len(targets), batch_size):
        batch = targets[start : start + batch_size]
        await run_worker_pool(
            batch,
            worker,
            concurrency,
            progress_every=max(25, len(batch) // 5),
            progress_label="SPEED",
        )
        tested_count += len(batch)
        qualified = sum(1 for node in targets[:tested_count] if node.speed_ok)
        print(f"[SPEED] 达标 {qualified}/{tested_count}", flush=True)
        if qualified >= target_qualified:
            break
    return records


async def _probe_speed(
    node: NodeResult,
    *,
    domain: str,
    path: str,
    wanted: int,
    timeout: float,
    user_agent: str,
    context: Any,
) -> tuple[int, float]:
    async def transfer() -> tuple[int, float]:
        writer = None
        started = time.perf_counter()
        received = 0
        try:
            reader, writer = await asyncio.open_connection(
                node.ip,
                node.port,
                ssl=context,
                server_hostname=domain,
                ssl_handshake_timeout=timeout,
            )
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {domain}\r\n"
                f"User-Agent: {user_agent}\r\n"
                "Accept: application/octet-stream\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(request.encode("ascii"))
            await writer.drain()
            header = await reader.readuntil(b"\r\n\r\n")
            status_line = header.split(b"\r\n", 1)[0].split()
            if len(status_line) < 2 or status_line[1] != b"200":
                raise ValueError(f"测速响应状态异常: {status_line[:2]}")
            while received < wanted:
                chunk = await reader.read(min(65536, wanted - received))
                if not chunk:
                    break
                received += len(chunk)
            return received, max(time.perf_counter() - started, 0.001)
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

    return await asyncio.wait_for(transfer(), timeout=timeout)
