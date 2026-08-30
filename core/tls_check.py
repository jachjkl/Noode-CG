from __future__ import annotations

import asyncio
import ssl
import statistics
import time
from typing import Any

from .async_utils import run_worker_pool
from .models import NodeResult


def make_ssl_context(verify_certificate: bool = True, minimum_version: str = "TLSv1.2") -> ssl.SSLContext:
    if verify_certificate:
        context = ssl.create_default_context()
    else:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    versions = {
        "TLSv1.2": ssl.TLSVersion.TLSv1_2,
        "TLSv1.3": ssl.TLSVersion.TLSv1_3,
    }
    context.minimum_version = versions.get(minimum_version, ssl.TLSVersion.TLSv1_2)
    context.set_alpn_protocols(["http/1.1"])
    return context


async def _probe_once(
    node: NodeResult,
    domain: str,
    context: ssl.SSLContext,
    timeout: float,
) -> tuple[float, str, str]:
    writer = None
    started = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(
                node.ip,
                node.port,
                ssl=context,
                server_hostname=domain,
                ssl_handshake_timeout=timeout,
            ),
            timeout=timeout,
        )
        ssl_object: ssl.SSLObject | None = writer.get_extra_info("ssl_object")
        if ssl_object is None:
            raise ssl.SSLError("TLS 握手未返回 SSL 对象")
        cipher = ssl_object.cipher()
        return (
            (time.perf_counter() - started) * 1000,
            ssl_object.version() or "",
            cipher[0] if cipher else "",
        )
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


async def check_tls(records: list[NodeResult], domain: str, options: dict[str, Any]) -> list[NodeResult]:
    timeout = float(options.get("timeout_seconds", 4.0))
    context = make_ssl_context(
        bool(options.get("verify_certificate", True)),
        str(options.get("minimum_version", "TLSv1.2")),
    )
    attempts = max(1, int(options.get("attempts", 1)))
    require_all = bool(options.get("require_all_attempts", False))
    maximum_latency = float(options.get("maximum_attempt_latency_ms", float("inf")))

    async def worker(node: NodeResult) -> NodeResult:
        latencies: list[float] = []
        successes = 0
        for attempt_index in range(attempts):
            try:
                latency, version, cipher = await _probe_once(node, domain, context, timeout)
                if latency > maximum_latency:
                    node.add_error(
                        "tls",
                        f"第 {attempt_index + 1} 次: {latency:.3f}ms > {maximum_latency:g}ms",
                    )
                    continue
                successes += 1
                latencies.append(latency)
                node.tls_version = version
                node.tls_cipher = cipher
            except Exception as exc:
                node.add_error("tls", f"第 {attempt_index + 1} 次: {exc}")
        node.tls_ok = successes == attempts if require_all else successes > 0
        node.tls_latency_ms = round(statistics.fmean(latencies), 3) if latencies else None
        node.probe_results["tls"] = {
            "attempts": attempts,
            "successes": successes,
            "latencies_ms": [round(value, 3) for value in latencies],
            "average_ms": node.tls_latency_ms,
            "maximum_attempt_latency_ms": maximum_latency,
            "strict_passed": node.tls_ok,
        }
        return node

    tested = await run_worker_pool(
        records,
        worker,
        int(options.get("concurrency", 300)),
        progress_every=max(500, len(records) // 20),
        progress_label="TLS",
    )
    return [node for node in tested if node.tls_ok]
