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
    ordered = sorted(records, key=lambda node: (node.http_latency_ms or 999999, node.tcp_latency_ms or 999999))
    targets = ordered[:count]
    domain = str(options.get("domain", "speed.cloudflare.com"))
    path = str(options.get("path", "/__down?bytes=262144"))
    wanted = int(options.get("bytes_per_test", 262144))
    timeout = float(options.get("timeout_seconds", 8.0))
    context = make_ssl_context(True, "TLSv1.2")

    async def worker(node: NodeResult) -> NodeResult:
        writer = None
        started = time.perf_counter()
        received = 0
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    node.ip,
                    node.port,
                    ssl=context,
                    server_hostname=domain,
                    ssl_handshake_timeout=timeout,
                ),
                timeout=timeout,
            )
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {domain}\r\n"
                f"User-Agent: {user_agent}\r\n"
                "Accept: application/octet-stream\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(request.encode("ascii"))
            await asyncio.wait_for(writer.drain(), timeout=timeout)
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=timeout)
            status_line = header.split(b"\r\n", 1)[0].split()
            if len(status_line) < 2 or status_line[1] != b"200":
                raise ValueError(f"测速响应状态异常: {status_line[:2]}")
            while received < wanted:
                chunk = await asyncio.wait_for(reader.read(min(65536, wanted - received)), timeout=timeout)
                if not chunk:
                    break
                received += len(chunk)
            elapsed = max(time.perf_counter() - started, 0.001)
            if received:
                node.speed_mbps = round(received * 8 / elapsed / 1_000_000, 3)
            else:
                raise ValueError("测速响应正文为空")
        except Exception as exc:
            node.speed_mbps = None
            node.add_error("speed", exc)
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
        return node

    await run_worker_pool(
        targets,
        worker,
        int(options.get("concurrency", 8)),
        progress_every=max(25, len(targets) // 10),
        progress_label="SPEED",
    )
    return records
