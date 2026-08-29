from __future__ import annotations

import asyncio
import time
from typing import Any

from .async_utils import run_worker_pool
from .models import NodeResult
from .tls_check import make_ssl_context


def _country(node: NodeResult) -> str:
    return (node.country or node.country_hint or "XX").upper()


def _select_speed_targets(
    records: list[NodeResult],
    count: int,
    options: dict[str, Any],
) -> list[NodeResult]:
    ordered = sorted(records, key=lambda node: (node.http_latency_ms or 999999, node.tcp_latency_ms or 999999))
    minimums = {
        str(country).upper(): max(0, int(value))
        for country, value in options.get("minimum_per_country", {}).items()
    }
    selected: list[NodeResult] = []
    selected_keys: set[str] = set()

    priority_sources = {
        str(source): max(0, int(value)) for source, value in options.get("priority_sources", {}).items()
    }
    for wanted_source, wanted_count in priority_sources.items():
        source_count = 0
        for node in ordered:
            if source_count >= min(wanted_count, count):
                break
            if node.key not in selected_keys and wanted_source in node.sources:
                selected.append(node)
                selected_keys.add(node.key)
                source_count += 1

    for wanted_country, wanted_count in minimums.items():
        for node in ordered:
            if sum(_country(item) == wanted_country for item in selected) >= min(wanted_count, count):
                break
            if node.key not in selected_keys and _country(node) == wanted_country:
                selected.append(node)
                selected_keys.add(node.key)

    for node in ordered:
        if len(selected) >= count:
            break
        if node.key not in selected_keys:
            selected.append(node)
            selected_keys.add(node.key)

    order = {node.key: index for index, node in enumerate(ordered)}
    return sorted(selected[:count], key=lambda node: order[node.key])


def _accepted_speed_mbps(
    received: int,
    wanted: int,
    elapsed: float,
    minimum_completion_ratio: float,
) -> float | None:
    if wanted <= 0 or received / wanted < minimum_completion_ratio:
        return None
    return round(received * 8 / max(elapsed, 0.001) / 1_000_000, 3)


async def test_speed(records: list[NodeResult], options: dict[str, Any], *, user_agent: str) -> list[NodeResult]:
    if not options.get("enabled", True) or not records:
        return records
    count = min(len(records), int(options.get("candidates", 600)))
    targets = _select_speed_targets(records, count, options)
    domain = str(options.get("domain", "speed.cloudflare.com"))
    path = str(options.get("path", "/__down?bytes=262144"))
    wanted = int(options.get("bytes_per_test", 262144))
    timeout = float(options.get("timeout_seconds", 8.0))
    minimum_completion_ratio = float(options.get("minimum_completion_ratio", 0.95))
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
            node.speed_mbps = _accepted_speed_mbps(received, wanted, elapsed, minimum_completion_ratio)
            if node.speed_mbps is None:
                node.add_error("speed", f"测速正文不完整: {received}/{wanted} bytes")
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
