from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from .colo_detect import enrich_locations, load_locations
from .config import resolve_path
from .exporter import publish_outputs
from .fetcher import collect_candidates
from .http_check import check_http
from .io_utils import write_checkpoint
from .isp_test import merge_probe_files
from .rolling import load_previous_top, merge_with_previous, save_previous_top
from .scorer import score_nodes, select_diverse
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
    output_dir = resolve_path(config, config["paths"]["output"])
    rolling = config.get("rolling", {})
    snapshot_path = resolve_path(config, rolling.get("snapshot_path", "data/previous-top100.json"))
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

    previous, previous_warnings = load_previous_top(
        output_dir,
        snapshot_path,
        int(rolling.get("previous_limit", 100)),
    )
    save_previous_top(snapshot_path, previous)
    report["warnings"].extend(previous_warnings)
    candidates, warnings = collect_candidates(config, priority_records=previous)
    unique_pool = len({node.ip for node in candidates})
    report["warnings"].extend(warnings)
    report["counts"]["pool_endpoints"] = len(candidates)
    report["counts"]["pool_unique_ips"] = unique_pool
    sampling_seed = config["sources"].get("cloudflare_ranges", {}).get("_resolved_sampling_seed")
    if sampling_seed is not None:
        report["counts"]["cloudflare_sampling_seed"] = sampling_seed
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
    write_checkpoint(checkpoint_dir / "01-pool.json", candidates)
    print(f"候选池: {unique_pool} 个 IP / {len(candidates)} 个 IP:端口", flush=True)
    for label, count in source_counts.items():
        print(f"远程源: {label} = {count} 条", flush=True)

    tcp_alive = asyncio.run(scan_tcp(candidates, pipeline["tcp"]))
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

    probe_paths = [resolve_path(config, path) for path in config.get("vantage", {}).get("probe_files", [])]
    merge_probe_files(http_valid, probe_paths)
    tested = asyncio.run(
        test_speed(
            http_valid,
            pipeline.get("speed", {}),
            user_agent=str(config["project"].get("user_agent", "Noode-CG/2.0")),
        )
    )
    ranked = score_nodes(tested, config["score"])
    current_selected = select_diverse(ranked, config["output"])
    selected, rolling_counts = merge_with_previous(current_selected, ranked, previous, config["output"])
    report["counts"].update(rolling_counts)
    report["counts"]["current_top300"] = len(current_selected)
    selected_countries = Counter((node.country or node.country_hint or "XX").upper() for node in selected)
    report["counts"]["selected_countries"] = dict(sorted(selected_countries.items()))
    for country, minimum in config["output"].get("minimum_per_country", {}).items():
        actual = selected_countries[str(country).upper()]
        if actual < int(minimum):
            report["warnings"].append(
                f"{str(country).upper()} 可用节点不足: 需要 {int(minimum)}，实际 {actual}；已保留正常可用输出"
            )
    write_checkpoint(checkpoint_dir / "05-ranked.json", ranked)

    gates_passed = all(item["passed"] for item in report["gates"])
    report["status"] = "ok" if gates_passed else "degraded"
    report["duration_seconds"] = round((datetime.now(UTC) - started).total_seconds(), 3)
    final_report = publish_outputs(
        output_dir,
        selected,
        report,
        config["output"],
    )
    print(
        f"完成: status={final_report['status']} selected={len(selected)} published={final_report['published']}",
        flush=True,
    )
    return final_report
