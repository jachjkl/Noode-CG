from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import ssl
import time
from typing import Any

from .async_utils import run_worker_pool
from .models import NodeResult
from .tls_check import make_ssl_context


def _parse_response(payload: bytes) -> tuple[int, dict[str, str], bytes]:
    header_blob, separator, body = payload.partition(b"\r\n\r\n")
    if not separator:
        raise ValueError("HTTP 响应头不完整")
    lines = header_blob.decode("iso-8859-1", errors="replace").split("\r\n")
    status_fields = lines[0].split()
    if len(status_fields) < 2 or not status_fields[1].isdigit():
        raise ValueError("HTTP 状态行无效")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    return int(status_fields[1]), headers, body


def _parse_trace(body: bytes) -> dict[str, str]:
    text = body.decode("utf-8", errors="replace")
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip().lower()] = value.strip()
    return values


async def _request(
    node: NodeResult,
    *,
    domain: str,
    path: str,
    context: ssl.SSLContext,
    timeout: float,
    user_agent: str,
    extra_headers: dict[str, str] | None = None,
    max_bytes: int = 128 * 1024,
) -> tuple[int, dict[str, str], bytes, float]:
    writer = None
    started = time.perf_counter()
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
        headers = {
            "Host": domain,
            "User-Agent": user_agent,
            "Accept": "*/*",
            "Connection": "close",
        }
        headers.update(extra_headers or {})
        request = f"GET {path} HTTP/1.1\r\n" + "".join(f"{key}: {value}\r\n" for key, value in headers.items()) + "\r\n"
        writer.write(request.encode("ascii", errors="strict"))
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        header_blob = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=timeout)
        status, response_headers, _ = _parse_response(header_blob)
        body = bytearray()
        if status != 101:
            declared = response_headers.get("content-length", "")
            expected = min(int(declared), max_bytes) if declared.isdigit() else None
            while len(body) < max_bytes:
                if expected is not None and len(body) >= expected:
                    break
                chunk = await asyncio.wait_for(
                    reader.read(min(65536, max_bytes - len(body))),
                    timeout=timeout,
                )
                if not chunk:
                    break
                body.extend(chunk)
        latency_ms = (time.perf_counter() - started) * 1000
        return status, response_headers, bytes(body), latency_ms
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


async def check_http(
    records: list[NodeResult],
    domain: str,
    options: dict[str, Any],
    websocket_options: dict[str, Any] | None = None,
    *,
    user_agent: str = "Noode-CG/2.0",
) -> list[NodeResult]:
    timeout = float(options.get("timeout_seconds", 5.0))
    context = make_ssl_context(True, "TLSv1.2")
    accepted = {int(value) for value in options.get("accepted_statuses", [200])}
    require_trace = bool(options.get("require_trace_fields", True))
    path = str(options.get("path", "/cdn-cgi/trace"))
    ws = websocket_options or {}

    async def worker(node: NodeResult) -> NodeResult:
        try:
            status, headers, body, latency = await _request(
                node,
                domain=domain,
                path=path,
                context=context,
                timeout=timeout,
                user_agent=user_agent,
            )
            trace = _parse_trace(body)
            node.http_status = status
            node.http_latency_ms = round(latency, 3)
            node.cf_ray = headers.get("cf-ray", "")
            node.colo = trace.get("colo", "").upper()
            node.country = trace.get("loc", "").upper()
            cloudflare_evidence = bool(node.colo and node.country) or (
                headers.get("server", "").lower() == "cloudflare" and bool(node.cf_ray)
            )
            node.http_ok = status in accepted and cloudflare_evidence
            if require_trace:
                node.http_ok = node.http_ok and bool(node.colo and node.country)
            if not node.http_ok:
                node.add_error("http", f"status={status}, colo={node.colo or '-'}, loc={node.country or '-'}")
        except Exception as exc:
            node.http_ok = False
            node.add_error("http", exc)

        if node.http_ok and ws.get("enabled", False):
            node.websocket_ok = await _check_websocket(
                node,
                domain=domain,
                path=str(ws.get("path", "/")),
                context=context,
                timeout=float(ws.get("timeout_seconds", 5.0)),
                user_agent=user_agent,
            )
            node.http_ok = node.http_ok and node.websocket_ok
        return node

    tested = await run_worker_pool(
        records,
        worker,
        int(options.get("concurrency", 250)),
        progress_every=max(250, len(records) // 20),
        progress_label="HTTP",
    )
    return [node for node in tested if node.http_ok]


async def _check_websocket(
    node: NodeResult,
    *,
    domain: str,
    path: str,
    context: ssl.SSLContext,
    timeout: float,
    user_agent: str,
) -> bool:
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    expected = base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
    ).decode("ascii")
    try:
        status, headers, _, _ = await _request(
            node,
            domain=domain,
            path=path,
            context=context,
            timeout=timeout,
            user_agent=user_agent,
            extra_headers={
                "Upgrade": "websocket",
                "Connection": "Upgrade",
                "Sec-WebSocket-Key": key,
                "Sec-WebSocket-Version": "13",
            },
            max_bytes=32 * 1024,
        )
        valid = status == 101 and headers.get("sec-websocket-accept", "") == expected
        if not valid:
            node.add_error("websocket", f"status={status}")
        return valid
    except Exception as exc:
        node.add_error("websocket", exc)
        return False
