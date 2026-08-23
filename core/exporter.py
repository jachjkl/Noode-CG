from __future__ import annotations

import csv
import io
import os
import tempfile
import zipfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .io_utils import atomic_write_json, atomic_write_text
from .models import NodeResult


def _public_node(node: NodeResult) -> dict[str, Any]:
    return {
        "ip": node.ip,
        "port": node.port,
        "ip_port": node.ip_port,
        "country": node.endpoint_country,
        "probe_country": node.country or "XX",
        "colo": node.colo,
        "region": node.region,
        "city": node.city,
        "tcp_latency_ms": node.tcp_latency_ms,
        "tcp_p95_latency_ms": node.tcp_p95_latency_ms,
        "tls_latency_ms": node.tls_latency_ms,
        "http_latency_ms": node.http_latency_ms,
        "http_p95_latency_ms": node.http_p95_latency_ms,
        "http_success_rate": node.http_success_rate,
        "jitter_ms": node.tcp_jitter_ms,
        "loss_rate": node.tcp_loss_rate,
        "speed_tested": node.speed_tested,
        "speed_ok": node.speed_ok,
        "speed_mbps": node.speed_mbps,
        "speed_completion_rate": node.speed_completion_rate,
        "speed_test_bytes": node.speed_test_bytes,
        "speed_test_seconds": node.speed_test_seconds,
        "history_runs": node.history_runs,
        "history_success_rate": node.history_success_rate,
        "history_consecutive_successes": node.history_consecutive_successes,
        "history_score": node.history_score,
        "tls_version": node.tls_version,
        "websocket_ok": node.websocket_ok,
        "score": node.score,
        "probe_results": node.probe_results,
        "platform_results": node.platform_results,
        "platform_score": node.platform_score,
        "platform_ok": node.platform_ok,
    }


def _node_line(node: NodeResult) -> str:
    return f"{node.ip_port}#{node.endpoint_country}"


def _write_zip(path: Path, records: list[NodeResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    grouped: dict[tuple[int, str], list[str]] = defaultdict(list)
    by_port: dict[int, list[str]] = defaultdict(list)
    for node in records:
        country = node.endpoint_country
        grouped[(node.port, country)].append(node.ip)
        by_port[node.port].append(node.ip)

    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for port in sorted(by_port):
                archive.writestr(f"{port}/ALL.txt", "\n".join(by_port[port]) + "\n")
            for (port, country), values in sorted(grouped.items()):
                archive.writestr(f"{port}/{country}.txt", "\n".join(values) + "\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _csv_text(records: list[NodeResult]) -> str:
    buffer = io.StringIO(newline="")
    fields = [
        "ip",
        "port",
        "country",
        "colo",
        "region",
        "city",
        "tcp_latency_ms",
        "tcp_p95_latency_ms",
        "tls_latency_ms",
        "http_latency_ms",
        "http_p95_latency_ms",
        "http_success_rate",
        "jitter_ms",
        "loss_rate",
        "speed_tested",
        "speed_ok",
        "speed_mbps",
        "speed_completion_rate",
        "history_runs",
        "history_success_rate",
        "tls_version",
        "score",
    ]
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for node in records:
        public = _public_node(node)
        writer.writerow({key: public.get(key) for key in fields})
    return buffer.getvalue()


def publish_outputs(
    output_dir: str | Path,
    records: list[NodeResult],
    report: dict[str, Any],
    options: dict[str, Any],
) -> dict[str, Any]:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    minimum = int(options.get("minimum_publish", 1))
    preserve = bool(options.get("preserve_last_good", True))
    existing_good = (destination / "nodes.txt").is_file() and bool(
        (destination / "nodes.txt").read_text(encoding="utf-8").strip()
    )
    base_publish = len(records) >= minimum or not preserve or not existing_good
    quality_allowed = not options.get("require_quality_gates", False) or report.get("status") == "ok"
    should_publish = base_publish and quality_allowed
    generated_at = datetime.now(UTC).isoformat()

    report = dict(report)
    report.update(
        {
            "generated_at": generated_at,
            "selected": len(records),
            "minimum_publish": minimum,
            "published": should_publish,
            "preserved_previous_output": not should_publish,
        }
    )

    if should_publish:
        public_nodes = [_public_node(node) for node in records]
        lines = "\n".join(_node_line(node) for node in records)
        atomic_write_text(destination / "nodes.txt", lines + ("\n" if lines else ""))
        atomic_write_json(destination / "nodes.json", public_nodes)
        atomic_write_json(
            destination / "api.json",
            {
                "project": "Noode-CG V2.3",
                "generated_at": generated_at,
                "count": len(records),
                "format": "edgetunnel-address-feed",
                "nodes": public_nodes,
            },
        )
        atomic_write_text(destination / "nodes.csv", _csv_text(records))
        if options.get("write_compatibility_zip", True):
            _write_zip(destination / "ip.zip", records)

    atomic_write_json(destination / "health.json", report)
    return report
