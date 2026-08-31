from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .colo_detect import enrich_locations, load_locations
from .config import resolve_path
from .exporter import publish_outputs
from .fetcher import collect_official_batch, collect_source_candidates
from .http_check import check_http
from .io_utils import atomic_write_text, write_checkpoint
from .isp_test import merge_probe_files
from .network_baseline import measure_network_baseline
from .parser import deduplicate
from .rolling import load_previous_top, prepare_retest_candidates, save_previous_top
from .selection_v3 import filter_by_average_latency, rank_final, select_latency_shortlist
from .speed_test import test_speed
from .tcp_scan import scan_tcp
from .tls_check import check_tls


def _gate(name: str, actual: int, minimum: int) -> dict[str, Any]:
    return {"name": name, "actual": actual, "minimum": minimum, "passed": actual >= minimum}


def _load_ip_snapshot(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def _save_ip_snapshot(path: Path, values: set[str]) -> None:
    atomic_write_text(path, "\n".join(sorted(values)) + ("\n" if values else ""))


def _run_entry_checks(
    records: list,
    *,
    domain: str,
    pipeline: dict[str, Any],
    user_agent: str,
    locations: dict,
) -> tuple[list, dict[str, int]]:
    tcp_alive = asyncio.run(scan_tcp(records, pipeline["tcp"]))
    tls_valid = asyncio.run(check_tls(tcp_alive, domain, pipeline["tls"]))
    http_valid = asyncio.run(
        check_http(
            tls_valid,
            domain,
            pipeline["http"],
            pipeline.get("websocket", {}),
            user_agent=user_agent,
        )
    )
    enrich_locations(http_valid, locations)
    eligible = filter_by_average_latency(
        http_valid,
        maximum_ms=float(pipeline["maximum_average_latency_ms"]),
    )
    return eligible, {
        "input": len(records),
        "tcp_alive": len(tcp_alive),
        "tls_valid": len(tls_valid),
        "http_valid": len(http_valid),
        "average_latency_eligible": len(eligible),
    }


def run_pipeline(config: dict[str, Any]) -> dict[str, Any]:
    started = datetime.now(UTC)
    monotonic_started = time.monotonic()
    pipeline = config["pipeline"]
    domain = str(config["project"]["target_domain"])
    user_agent = str(config["project"].get("user_agent", "Noode-CG/3.0"))
    checkpoint_dir = resolve_path(config, config["paths"]["checkpoints"])
    output_dir = resolve_path(config, config["paths"]["output"])
    rolling = config.get("rolling", {})
    top_snapshot = resolve_path(config, rolling.get("snapshot_path", "data/previous-top100.json"))
    official_snapshot = resolve_path(
        config,
        rolling.get("official_snapshot_path", "data/previous-official-ips.txt"),
    )
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "status": "running",
        "started_at": started.isoformat(),
        "target_domain": domain,
        "vantage": config.get("vantage", {}).get("name", "unknown"),
        "counts": {},
        "gates": [],
        "warnings": [],
        "rounds": [],
    }

    report["network_baseline"] = measure_network_baseline(
        config.get("network_baseline", {}),
        user_agent=user_agent,
    )

    previous, previous_warnings = load_previous_top(
        output_dir,
        top_snapshot,
        int(rolling.get("previous_limit", 100)),
    )
    save_previous_top(top_snapshot, previous)
    report["warnings"].extend(previous_warnings)
    report["counts"]["previous_loaded"] = len(previous)

    source_candidates, source_warnings = collect_source_candidates(config)
    report["warnings"].extend(source_warnings)
    report["counts"]["link_source_endpoints"] = len(source_candidates)
    report["counts"]["link_source_unique_ips"] = len({node.ip for node in source_candidates})
    source_counts: dict[str, int] = {}
    for configured in config["sources"].get("remote", []):
        entry = {"url": configured} if isinstance(configured, str) else configured
        label = str(entry.get("name") or entry.get("url") or "remote")
        count = sum(label in node.sources for node in source_candidates)
        source_counts[label] = count
        if entry.get("required", False):
            report["gates"].append(_gate(f"required_source:{label}", count, int(entry.get("min_records", 1))))
    report["counts"]["remote_sources"] = source_counts

    prior_official_ips = _load_ip_snapshot(official_snapshot)
    excluded_official_ips = {node.ip for node in source_candidates} | prior_official_ips
    current_official_ips: set[str] = set()
    qualified: dict[str, Any] = {}
    target = int(pipeline["latency_shortlist"])
    max_runtime = float(pipeline["max_runtime_seconds"])
    minimum_round_budget = float(pipeline["minimum_round_budget_seconds"])
    postprocess_reserve = float(pipeline["postprocess_reserve_seconds"])
    round_index = 0
    locations = load_locations(resolve_path(config, config["paths"]["locations"]))

    while len(qualified) < target:
        elapsed = time.monotonic() - monotonic_started
        if round_index > 0 and max_runtime - elapsed < minimum_round_budget + postprocess_reserve:
            report["warnings"].append("剩余运行时间不足以安全完成下一轮，已停止追加官方候选")
            break

        official, official_warnings = collect_official_batch(
            config,
            exclude_ips=excluded_official_ips | current_official_ips,
            round_index=round_index,
        )
        report["warnings"].extend(official_warnings)
        wanted = int(config["sources"]["cloudflare_ranges"]["official_batch_size"])
        if len({node.ip for node in official}) < wanted:
            report["warnings"].append("官方段无法再提供完整的 50,000 个不重复候选，停止追加")
            break
        current_official_ips.update(node.ip for node in official)
        batch = deduplicate([*source_candidates, *official]) if round_index == 0 else official
        print(
            f"第 {round_index + 1} 轮: 链接源={len(source_candidates) if round_index == 0 else 0}, "
            f"官方新 IP={len(official)}, 合计={len(batch)}",
            flush=True,
        )
        eligible, round_counts = _run_entry_checks(
            batch,
            domain=domain,
            pipeline=pipeline,
            user_agent=user_agent,
            locations=locations,
        )
        for node in eligible:
            existing = qualified.get(node.key)
            if existing is None or (node.average_latency_ms or 999999) < (existing.average_latency_ms or 999999):
                qualified[node.key] = node
        round_counts.update(
            {
                "round": round_index + 1,
                "official_unique_ips": len({node.ip for node in official}),
                "qualified_total": len(qualified),
            }
        )
        report["rounds"].append(round_counts)
        print(
            f"第 {round_index + 1} 轮完成: <= {pipeline['maximum_average_latency_ms']}ms "
            f"新增={len(eligible)}, 累计={len(qualified)}/{target}",
            flush=True,
        )
        round_index += 1

    _save_ip_snapshot(official_snapshot, current_official_ips)
    report["counts"]["official_previous_excluded"] = len(prior_official_ips)
    report["counts"]["official_sampled_this_run"] = len(current_official_ips)
    report["counts"]["official_rounds"] = round_index
    report["counts"]["latency_eligible"] = len(qualified)
    report["gates"].append(_gate("latency_eligible", len(qualified), target))
    sampling_seed = config["sources"].get("cloudflare_ranges", {}).get("_resolved_sampling_seed")
    if sampling_seed is not None:
        report["counts"]["cloudflare_sampling_seed"] = sampling_seed

    selected: list = []
    remaining_for_postprocess = max_runtime - (time.monotonic() - monotonic_started)
    if len(qualified) >= target and remaining_for_postprocess >= postprocess_reserve:
        shortlist = select_latency_shortlist(qualified.values(), count=target)
        write_checkpoint(checkpoint_dir / "01-latency-top3000.json", shortlist)
        probe_paths = [resolve_path(config, path) for path in config.get("vantage", {}).get("probe_files", [])]
        merge_probe_files(shortlist, probe_paths)
        speed_tested = asyncio.run(test_speed(shortlist, pipeline["speed"], user_agent=user_agent))
        speed_complete = [node for node in speed_tested if node.speed_mbps is not None]
        report["counts"]["shortlist"] = len(shortlist)
        report["counts"]["shortlist_speed_complete"] = len(speed_complete)

        current_count = int(pipeline["current_selection"])
        current = rank_final(speed_complete, count=current_count)
        report["counts"]["current_top300"] = len(current)
        report["gates"].append(_gate("current_top300", len(current), current_count))
        write_checkpoint(checkpoint_dir / "02-current-top300.json", current)

        retest = prepare_retest_candidates(current, previous)
        report["counts"]["rolling_retest_candidates"] = len(retest)
        retest_tcp = asyncio.run(scan_tcp(retest, pipeline["rolling_retest"]))
        retest_tls = asyncio.run(check_tls(retest_tcp, domain, pipeline["tls"]))
        retest_http = asyncio.run(
            check_http(
                retest_tls,
                domain,
                pipeline["http"],
                pipeline.get("websocket", {}),
                user_agent=user_agent,
            )
        )
        enrich_locations(retest_http, locations)
        retest_latency = filter_by_average_latency(
            retest_http,
            maximum_ms=float(pipeline["maximum_average_latency_ms"]),
        )
        retest_speed_options = dict(pipeline["speed"])
        retest_speed_options["candidates"] = len(retest_latency)
        retest_speed = asyncio.run(test_speed(retest_latency, retest_speed_options, user_agent=user_agent))
        final_candidates = [node for node in retest_speed if node.speed_mbps is not None]
        selected = rank_final(final_candidates, count=int(config["output"]["top_nodes"]))
        previous_keys = {node.key for node in previous}
        report["counts"]["previous_reverified"] = sum(node.key in previous_keys for node in final_candidates)
        report["counts"]["previous_in_final"] = sum(node.key in previous_keys for node in selected)
        report["counts"]["final_speed_complete"] = len(final_candidates)
        write_checkpoint(checkpoint_dir / "03-final-top300.json", selected)
    elif len(qualified) >= target:
        report["warnings"].append("已获得 3,000 个合格项，但剩余时间不足以完成下载及滚动复测，保留旧订阅")

    report["gates"].append(_gate("final_top300", len(selected), int(config["output"]["top_nodes"])))

    gates_passed = all(item["passed"] for item in report["gates"])
    report["status"] = "ok" if gates_passed else "degraded"
    report["duration_seconds"] = round((datetime.now(UTC) - started).total_seconds(), 3)
    final_report = publish_outputs(output_dir, selected, report, config["output"])
    print(
        f"完成: status={final_report['status']} selected={len(selected)} published={final_report['published']}",
        flush=True,
    )
    return final_report
