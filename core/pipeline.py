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
from .models import NodeResult
from .network_baseline import measure_network_baseline
from .parser import deduplicate
from .ranking import calculate_average_latency, rank_final
from .rolling import load_previous_top, prepare_retest_candidates, save_previous_top
from .speed_test import meets_minimum_speed, test_speed
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


def _three_metric_checks(
    records: list[NodeResult], *, domain: str, pipeline: dict[str, Any],
    user_agent: str, locations: dict[str, Any],
) -> tuple[list[NodeResult], dict[str, int]]:
    tls_valid = asyncio.run(check_tls(records, domain, pipeline["tls"]))
    http_valid = asyncio.run(check_http(
        tls_valid, domain, pipeline["http"], pipeline.get("websocket", {}),
        user_agent=user_agent,
    ))
    enrich_locations(http_valid, locations)
    for node in http_valid:
        calculate_average_latency(node)
    return http_valid, {
        "tls_three_attempt_average_under_300ms": len(tls_valid),
        "https_ttfb_three_attempt_average_under_300ms": len(http_valid),
    }


def _speed_checks(
    records: list[NodeResult], *, pipeline: dict[str, Any], user_agent: str,
    probe_paths: list[Path],
) -> tuple[list[NodeResult], dict[str, int]]:
    merge_probe_files(records, probe_paths)
    options = dict(pipeline["speed"])
    options["candidates"] = len(records)
    tested = asyncio.run(test_speed(records, options, user_agent=user_agent))
    qualified = [node for node in tested if meets_minimum_speed(
        node, minimum_mbps=float(options["minimum_mbps"])
    )]
    return qualified, {
        "speed_tested": len(tested),
        "speed_at_least_16mbps": len(qualified),
    }


def _keep_best_by_ip(target: dict[str, NodeResult], records: list[NodeResult]) -> None:
    for node in records:
        existing = target.get(node.ip)
        new_latency = node.average_latency_ms if node.average_latency_ms is not None else 999999
        old_latency = (
            existing.average_latency_ms
            if existing is not None and existing.average_latency_ms is not None else 999999
        )
        if existing is None or new_latency < old_latency:
            target[node.ip] = node


def run_pipeline(config: dict[str, Any]) -> dict[str, Any]:
    started = datetime.now(UTC)
    monotonic_started = time.monotonic()
    pipeline = config["pipeline"]
    domain = str(config["project"]["target_domain"])
    user_agent = str(config["project"].get("user_agent", "Noode-CG/6.0"))
    checkpoint_dir = resolve_path(config, config["paths"]["checkpoints"])
    output_dir = resolve_path(config, config["paths"]["output"])
    rolling = config.get("rolling", {})
    top_snapshot = resolve_path(config, rolling.get("snapshot_path", "data/previous-top100.json"))
    official_snapshot = resolve_path(
        config, rolling.get("official_snapshot_path", "data/previous-official-ips.txt")
    )
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "status": "running", "started_at": started.isoformat(), "target_domain": domain,
        "vantage": config.get("vantage", {}).get("name", "unknown"), "counts": {},
        "gates": [], "warnings": [], "rounds": [], "metric_batches": [],
        "speed_batches": [], "rolling_attempts": [],
    }
    report["network_baseline"] = measure_network_baseline(
        config.get("network_baseline", {}), user_agent=user_agent
    )
    baseline_passed = bool(report["network_baseline"].get("all_targets_passed", False))
    report["gates"].append(_gate("network_baseline_three_sites", int(baseline_passed), 1))

    previous, warnings = load_previous_top(
        output_dir, top_snapshot, int(rolling.get("previous_limit", 100))
    )
    save_previous_top(top_snapshot, previous)
    report["warnings"].extend(warnings)
    report["counts"]["previous_loaded"] = len(previous)

    source_candidates, warnings = collect_source_candidates(config)
    report["warnings"].extend(warnings)
    report["counts"]["link_source_endpoints"] = len(source_candidates)
    report["counts"]["link_source_unique_ips"] = len({node.ip for node in source_candidates})
    source_counts: dict[str, int] = {}
    for configured in config["sources"].get("remote", []):
        entry = {"url": configured} if isinstance(configured, str) else configured
        label = str(entry.get("name") or entry.get("url") or "remote")
        count = sum(label in node.sources for node in source_candidates)
        source_counts[label] = count
        report["gates"].append(_gate(
            f"required_source:{label}", count, int(entry.get("min_records", 1))
        ))
    report["counts"]["remote_sources"] = source_counts

    prior_official_ips = _load_ip_snapshot(official_snapshot)
    excluded_official_ips = {node.ip for node in source_candidates} | prior_official_ips
    current_official_ips: set[str] = set()
    strict_tcp: dict[str, NodeResult] = {}
    processed_metric_keys: set[str] = set()
    three_metric: dict[str, NodeResult] = {}
    speed_processed_keys: set[str] = set()
    current_quality: dict[str, NodeResult] = {}
    rolling_failed_current: set[str] = set()
    selected: list[NodeResult] = []
    round_index = metric_batch_index = speed_batch_index = rolling_attempt_index = 0
    metric_target = int(pipeline["three_metric_shortlist"])
    final_target = int(pipeline["current_selection"])
    max_runtime = float(pipeline["max_runtime_seconds"])
    reserve = float(pipeline["postprocess_reserve_seconds"])
    minimum_round_budget = float(pipeline["minimum_round_budget_seconds"])
    locations = load_locations(resolve_path(config, config["paths"]["locations"]))
    probe_paths = [resolve_path(config, path) for path in config.get("vantage", {}).get("probe_files", [])]
    metric_checkpoint_written = False

    while len(selected) < final_target:
        remaining = max_runtime - (time.monotonic() - monotonic_started)
        if remaining <= 0:
            report["warnings"].append("已到内部运行时限，保留上一版订阅")
            break

        available = [node for node in current_quality.values() if node.ip not in rolling_failed_current]
        if len(available) >= final_target and remaining >= minimum_round_budget:
            current = rank_final(available, count=final_target)
            retest = prepare_retest_candidates(current, previous)
            retest_tcp = asyncio.run(scan_tcp(retest, pipeline["rolling_retest"]))
            retest_metrics, metric_counts = _three_metric_checks(
                retest_tcp, domain=domain, pipeline=pipeline,
                user_agent=user_agent, locations=locations,
            )
            retest_quality, speed_counts = _speed_checks(
                retest_metrics, pipeline=pipeline, user_agent=user_agent, probe_paths=probe_paths
            )
            selected = rank_final(retest_quality, count=final_target)
            passed_ips = {node.ip for node in retest_quality}
            failed = {node.ip for node in current if node.ip not in passed_ips}
            rolling_failed_current.update(failed)
            previous_ips = {node.ip for node in previous}
            rolling_attempt_index += 1
            attempt_counts = {
                **metric_counts, **speed_counts, "attempt": rolling_attempt_index,
                "input": len(retest), "tcp_three_attempt_average_under_300ms": len(retest_tcp),
                "current_failed": len(failed),
                "previous_reverified": sum(node.ip in previous_ips for node in retest_quality),
                "final_count": len(selected),
            }
            report["rolling_attempts"].append(attempt_counts)
            if len(selected) >= final_target:
                report["counts"]["previous_reverified"] = attempt_counts["previous_reverified"]
                report["counts"]["previous_in_final"] = sum(node.ip in previous_ips for node in selected)
                write_checkpoint(checkpoint_dir / "04-final-top500.json", selected)
                break
            selected = []
            continue

        untested_speed = [node for node in three_metric.values() if node.key not in speed_processed_keys]
        if len(untested_speed) >= metric_target and remaining >= reserve:
            chunk = sorted(untested_speed, key=lambda node: (
                node.average_latency_ms if node.average_latency_ms is not None else 999999,
                node.ip, node.port,
            ))[:metric_target]
            speed_batch_index += 1
            qualified, counts = _speed_checks(
                chunk, pipeline=pipeline, user_agent=user_agent, probe_paths=probe_paths
            )
            speed_processed_keys.update(node.key for node in chunk)
            _keep_best_by_ip(current_quality, qualified)
            counts.update({"batch": speed_batch_index, "input": len(chunk),
                           "current_speed_qualified_total": len(current_quality)})
            report["speed_batches"].append(counts)
            continue

        unprocessed = [node for node in strict_tcp.values() if node.key not in processed_metric_keys]
        if len(unprocessed) >= metric_target and remaining >= reserve:
            chunk = sorted(unprocessed, key=lambda node: (
                node.tcp_latency_ms if node.tcp_latency_ms is not None else 999999,
                node.ip, node.port,
            ))[:metric_target]
            metric_batch_index += 1
            qualified, counts = _three_metric_checks(
                chunk, domain=domain, pipeline=pipeline,
                user_agent=user_agent, locations=locations,
            )
            processed_metric_keys.update(node.key for node in chunk)
            _keep_best_by_ip(three_metric, qualified)
            counts.update({"batch": metric_batch_index, "input": len(chunk),
                           "three_metric_qualified_total": len(three_metric)})
            report["metric_batches"].append(counts)
            if len(three_metric) >= metric_target and not metric_checkpoint_written:
                write_checkpoint(
                    checkpoint_dir / "02-three-metric-top5000.json",
                    rank_final(list(three_metric.values()), count=metric_target),
                )
                metric_checkpoint_written = True
            continue

        if round_index > 0 and remaining < minimum_round_budget + reserve:
            report["warnings"].append("剩余时间不足以完成下一批官方候选及后续测速，保留上一版订阅")
            break

        official, warnings = collect_official_batch(
            config, exclude_ips=excluded_official_ips | current_official_ips,
            round_index=round_index,
        )
        report["warnings"].extend(warnings)
        wanted = int(config["sources"]["cloudflare_ranges"]["official_batch_size"])
        if len({node.ip for node in official}) < wanted:
            report["warnings"].append("官方段无法再提供完整的 50,000 个不重复候选")
            break
        current_official_ips.update(node.ip for node in official)
        batch = deduplicate([*source_candidates, *official]) if round_index == 0 else official
        strict_passed = asyncio.run(scan_tcp(batch, pipeline["tcp"]))
        for node in strict_passed:
            old = strict_tcp.get(node.ip)
            if old is None or (node.tcp_latency_ms or 999999) < (old.tcp_latency_ms or 999999):
                strict_tcp[node.ip] = node
        round_index += 1
        report["rounds"].append({
            "round": round_index,
            "link_endpoints": len(source_candidates) if round_index == 1 else 0,
            "official_unique_ips": len({node.ip for node in official}),
            "input": len(batch),
            "tcp_three_attempt_average_under_300ms": len(strict_passed),
            "strict_tcp_total": len(strict_tcp),
        })
        print(
            f"第 {round_index} 轮: 输入={len(batch)}, TCP 三次合格={len(strict_passed)}, "
            f"三项累计={len(three_metric)}/{metric_target}", flush=True,
        )

    _save_ip_snapshot(official_snapshot, current_official_ips)
    report["counts"].update({
        "official_previous_excluded": len(prior_official_ips),
        "official_sampled_this_run": len(current_official_ips),
        "official_rounds": round_index,
        "strict_tcp_qualified": len(strict_tcp),
        "metric_processed": len(processed_metric_keys),
        "three_metric_qualified": len(three_metric),
        "speed_processed": len(speed_processed_keys),
        "speed_at_least_16mbps": len(current_quality),
        "rolling_failed_current": len(rolling_failed_current),
    })
    sampling_seed = config["sources"].get("cloudflare_ranges", {}).get("_resolved_sampling_seed")
    if sampling_seed is not None:
        report["counts"]["cloudflare_sampling_seed"] = sampling_seed
    report["gates"].extend([
        _gate("three_metric_qualified", len(three_metric), metric_target),
        _gate("current_speed_qualified", len(current_quality), final_target),
        _gate("final_top500", len(selected), int(config["output"]["top_nodes"])),
    ])

    if selected and not baseline_passed:
        report["warnings"].append("Google、Cloudflare、GitHub 基线未全部通过三次测试，保留旧订阅")
        selected = []
    report["status"] = "ok" if all(item["passed"] for item in report["gates"]) else "degraded"
    report["duration_seconds"] = round((datetime.now(UTC) - started).total_seconds(), 3)
    final_report = publish_outputs(output_dir, selected, report, config["output"])
    print(
        f"完成: status={final_report['status']} selected={len(selected)} "
        f"published={final_report['published']}", flush=True,
    )
    return final_report
