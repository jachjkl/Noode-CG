from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from .colo_detect import enrich_locations, load_locations
from .config import resolve_path
from .exporter import publish_outputs
from .fetcher import collect_candidates
from .history import enrich_history, update_history
from .http_check import check_http
from .io_utils import read_json, write_checkpoint
from .isp_test import apply_platform_policy, merge_probe_files
from .scorer import score_nodes, select_diverse
from .sharding import select_scan_batch
from .speed_test import test_speed
from .tcp_scan import scan_tcp
from .tls_check import check_tls


def _gate(name: str, actual: int, minimum: int) -> dict[str, Any]:
    return {"name": name, "actual": actual, "minimum": minimum, "passed": actual >= minimum}


def run_pipeline(config: dict[str, Any]) -> dict[str, Any]:
    started = datetime.now(UTC)
    pipeline = config["pipeline"]
    domain = str(config["project"]["target_domain"])
    checkpoint_dir = resolve_path(config, config["paths"]["checkpoints"])
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "status": "running",
        "started_at": started.isoformat(),
        "target_domain": domain,
        "vantage": config.get("vantage", {}).get("name", "unknown"),
        "counts": {},
        "gates": [],
        "warnings": [],
    }

    candidates, warnings = collect_candidates(config)
    unique_pool = len({node.ip for node in candidates})
    report["warnings"].extend(warnings)
    report["counts"]["pool_endpoints"] = len(candidates)
    report["counts"]["pool_unique_ips"] = unique_pool
    report["gates"].append(_gate("pool_unique_ips", unique_pool, int(pipeline["min_pool"])))
    source_counts: dict[str, int] = {}
    for configured in config["sources"].get("remote", []):
        entry = {"url": configured} if isinstance(configured, str) else configured
        label = str(entry.get("name") or entry.get("url") or "remote")
        count = sum(1 for node in candidates if label in node.sources)
        source_counts[label] = count
        if entry.get("required", False):
            report["gates"].append(_gate(f"required_source:{label}", count, int(entry.get("min_records", 1))))
    report["counts"]["remote_sources"] = source_counts
    required_sources = {
        str(entry.get("name") or entry.get("url") or "remote")
        for configured in config["sources"].get("remote", [])
        for entry in [{"url": configured} if isinstance(configured, str) else configured]
        if entry.get("required", False)
    }
    history_path = resolve_path(config, config["paths"].get("history", "data/stability-history.json"))
    history_state = read_json(history_path, default={})
    history_nodes = history_state.get("nodes", {}) if isinstance(history_state, dict) else {}
    scan_options = dict(pipeline.get("scan", {}))
    scan_options["required_sources"] = sorted(required_sources)
    scan_candidates, scan_metadata = select_scan_batch(
        candidates,
        scan_options,
        history_keys=set(history_nodes) if isinstance(history_nodes, dict) else set(),
    )
    report["scan"] = scan_metadata
    report["counts"]["scan_endpoints"] = len(scan_candidates)
    report["counts"]["scan_unique_ips"] = len({node.ip for node in scan_candidates})
    write_checkpoint(checkpoint_dir / "01-pool.json", scan_candidates)
    print(
        f"逻辑候选池: {unique_pool} 个 IP / 本轮扫描: {len(scan_candidates)} 个端点 "
        f"(分片 {scan_metadata['shard_index'] + 1}/{scan_metadata['shard_count']})",
        flush=True,
    )
    for label, count in source_counts.items():
        print(f"远程源: {label} = {count} 条", flush=True)

    tcp_alive = asyncio.run(scan_tcp(scan_candidates, pipeline["tcp"]))
    report["counts"]["tcp_alive"] = len(tcp_alive)
    report["gates"].append(_gate("tcp_alive", len(tcp_alive), int(pipeline["min_tcp_alive"])))
    write_checkpoint(checkpoint_dir / "02-tcp.json", tcp_alive)
    print(f"TCP 通过: {len(tcp_alive)}", flush=True)

    tls_valid = asyncio.run(check_tls(tcp_alive, domain, pipeline["tls"]))
    report["counts"]["tls_valid"] = len(tls_valid)
    report["gates"].append(_gate("tls_valid", len(tls_valid), int(pipeline["min_tls_valid"])))
    write_checkpoint(checkpoint_dir / "03-tls.json", tls_valid)
    print(f"TLS 通过: {len(tls_valid)}", flush=True)

    http_valid = asyncio.run(
        check_http(
            tls_valid,
            domain,
            pipeline["http"],
            pipeline.get("websocket", {}),
            user_agent=str(config["project"].get("user_agent", "Noode-CG/2.0")),
        )
    )
    locations = load_locations(resolve_path(config, config["paths"]["locations"]))
    enrich_locations(http_valid, locations)
    report["counts"]["http_valid"] = len(http_valid)
    report["gates"].append(_gate("http_valid", len(http_valid), int(pipeline["min_http_valid"])))
    write_checkpoint(checkpoint_dir / "04-http.json", http_valid)
    print(f"HTTP/Cloudflare 通过: {len(http_valid)}", flush=True)

    history_options = pipeline.get("history", {})
    enrich_history(history_path, http_valid, history_options)
    stability = pipeline.get("stability", {})
    stability_limit = min(len(http_valid), int(stability.get("candidates", len(http_valid))))
    stability_assessed = sorted(
        http_valid,
        key=lambda node: (
            -node.history_score,
            node.http_latency_ms or 999999,
            node.tls_latency_ms or 999999,
        ),
    )[:stability_limit]
    report["counts"]["stability_assessed"] = len(stability_assessed)
    if stability.get("enabled", True):
        stable_tcp = asyncio.run(scan_tcp(stability_assessed, stability.get("tcp", {})))
        report["counts"]["stability_tcp_valid"] = len(stable_tcp)
        stable_http = asyncio.run(
            check_http(
                stable_tcp,
                domain,
                stability.get("http", {}),
                pipeline.get("websocket", {}),
                user_agent=str(config["project"].get("user_agent", "Noode-CG/2.1")),
            )
        )
        enrich_locations(stable_http, locations)
    else:
        stable_http = stability_assessed
    report["counts"]["stable_valid"] = len(stable_http)
    report["gates"].append(
        _gate("stable_valid", len(stable_http), int(pipeline.get("min_stable_valid", pipeline["min_http_valid"])))
    )
    write_checkpoint(checkpoint_dir / "05-stable.json", stable_http)
    print(f"重复稳定性复测通过: {len(stable_http)}", flush=True)

    probe_paths = [resolve_path(config, path) for path in config.get("vantage", {}).get("probe_files", [])]
    merge_probe_files(stable_http, probe_paths)
    platform_options = config.get("platform_compatibility", {})
    platform_assessed = apply_platform_policy(stable_http, platform_options)
    report["counts"]["platform_verified"] = sum(node.platform_ok is True for node in stable_http)
    if platform_options.get("required", False):
        report["gates"].append(_gate("platform_verified", len(platform_assessed), int(platform_options.get("minimum_nodes", 50))))
    tested = asyncio.run(
        test_speed(
            platform_assessed,
            pipeline.get("speed", {}),
            user_agent=str(config["project"].get("user_agent", "Noode-CG/2.1")),
        )
    )
    speed_options = pipeline.get("speed", {})
    speed_required = bool(speed_options.get("enabled", True) and speed_options.get("require_for_publish", True))
    speed_assessed = [node for node in tested if node.speed_tested]
    qualified = [node for node in tested if node.speed_ok] if speed_required else tested
    report["counts"]["speed_tested"] = len(speed_assessed)
    report["counts"]["speed_qualified"] = len(qualified)
    if speed_required:
        report["gates"].append(
            _gate(
                "speed_qualified",
                len(qualified),
                int(pipeline.get("min_speed_qualified", pipeline["min_http_valid"])),
            )
        )
    history_assessed = speed_assessed if speed_required else stability_assessed
    history_passed = qualified if speed_required else stable_http
    report["history"] = update_history(history_path, history_assessed, history_passed, history_options)
    ranked = score_nodes(qualified, config["score"])
    selected = select_diverse(ranked, config["output"])
    write_checkpoint(checkpoint_dir / "06-ranked.json", ranked)

    gates_passed = all(item["passed"] for item in report["gates"])
    report["status"] = "ok" if gates_passed else "degraded"
    report["duration_seconds"] = round((datetime.now(UTC) - started).total_seconds(), 3)
    final_report = publish_outputs(
        resolve_path(config, config["paths"]["output"]),
        selected,
        report,
        config["output"],
    )
    print(
        f"完成: status={final_report['status']} selected={len(selected)} published={final_report['published']}",
        flush=True,
    )
    return final_report
