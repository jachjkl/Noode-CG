from __future__ import annotations

import asyncio
import ssl
import statistics
import time
from collections.abc import Callable
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


async def check_tls(
    records: list[NodeResult],
    domain: str,
    options: dict[str, Any],
    *,
    on_result: Callable[[NodeResult], None] | None = None,
) -> list[NodeResult]:
    timeout = float(options.get("timeout_seconds", 4.0))
    context = make_ssl_context(
        bool(options.get("verify_certificate", True)),
        str(options.get("minimum_version", "TLSv1.2")),
    )
    attempts = max(1, int(options.get("attempts", 1)))
    require_all = bool(options.get("require_all_attempts", False))
    stop_on_failure = bool(options.get("stop_on_failure", False))
    stop_when_impossible = bool(options.get("stop_when_average_impossible", False))
    maximum_latency = float(options.get("maximum_average_latency_ms", float("inf")))
    maximum_jitter = float(options.get("maximum_jitter_ms", float("inf")))

    async def worker(node: NodeResult) -> NodeResult:
        latencies: list[float] = []
        successes = 0
        early_reason = ""
        for attempt_index in range(attempts):
            try:
                latency, version, cipher = await _probe_once(node, domain, context, timeout)
                successes += 1
                latencies.append(latency)
                node.tls_version = version
                node.tls_cipher = cipher
            except Exception as exc:
                node.add_error("tls", f"第 {attempt_index + 1} 次: {exc}")
                if require_all and stop_on_failure:
                    early_reason = "必需测试失败，提前终止剩余尝试"
                    break
            if stop_when_impossible and sum(latencies) > maximum_latency * attempts:
                early_reason = "即使剩余延迟为 0，测试平均值也会超限"
                break
        node.tls_latency_ms = round(statistics.fmean(latencies), 3) if latencies else None
        node.tls_jitter_ms = (
            round(statistics.pstdev(latencies), 3) if len(latencies) > 1 else 0.0 if latencies else None
        )
        attempts_passed = successes == attempts if require_all else successes > 0
        average_passed = node.tls_latency_ms is not None and node.tls_latency_ms <= maximum_latency
        jitter_passed = node.tls_jitter_ms is not None and node.tls_jitter_ms <= maximum_jitter
        node.tls_ok = attempts_passed and average_passed and jitter_passed
        if attempts_passed and not average_passed:
            node.add_error(
                "tls",
                f"测试平均延迟 {node.tls_latency_ms:g}ms > {maximum_latency:g}ms",
            )
        elif attempts_passed and not jitter_passed:
            node.add_error("tls", f"抖动 {node.tls_jitter_ms:g}ms > {maximum_jitter:g}ms")
        node.probe_results["tls"] = {
            "attempts": attempts,
            "successes": successes,
            "latencies_ms": [round(value, 3) for value in latencies],
            "average_ms": node.tls_latency_ms,
            "jitter_ms": node.tls_jitter_ms,
            "maximum_average_latency_ms": maximum_latency,
            "maximum_jitter_ms": maximum_jitter,
            "early_rejected": bool(early_reason),
            "strict_passed": node.tls_ok,
        }
        if early_reason:
            node.add_error("tls", early_reason)
        if on_result is not None:
            try:
                on_result(node)
            except Exception:
                # A live UI writer must never be able to abort network probing.
                pass
        return node

    tested = await run_worker_pool(
        records,
        worker,
        int(options.get("concurrency", 300)),
        progress_every=max(500, len(records) // 20),
        progress_label="TLS",
    )
    return [node for node in tested if node.tls_ok]
