from __future__ import annotations

import asyncio
import ssl
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


async def check_tls(records: list[NodeResult], domain: str, options: dict[str, Any]) -> list[NodeResult]:
    timeout = float(options.get("timeout_seconds", 4.0))
    context = make_ssl_context(
        bool(options.get("verify_certificate", True)),
        str(options.get("minimum_version", "TLSv1.2")),
    )

    async def worker(node: NodeResult) -> NodeResult:
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
            node.tls_ok = ssl_object is not None
            node.tls_latency_ms = round((time.perf_counter() - started) * 1000, 3)
            if ssl_object:
                node.tls_version = ssl_object.version() or ""
                cipher = ssl_object.cipher()
                node.tls_cipher = cipher[0] if cipher else ""
        except Exception as exc:
            node.tls_ok = False
            node.add_error("tls", exc)
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
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
