from __future__ import annotations

import asyncio
import gzip
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .colo_detect import enrich_locations, load_locations
from .config import resolve_path
from .exporter import publish_outputs
from .fetcher import collect_official_batch, collect_source_candidates
from .http_check import check_http
from .io_utils import atomic_write_bytes, atomic_write_text, write_checkpoint
from .isp_test import merge_probe_files
from .models import NodeResult
from .network_baseline import measure_network_baseline
from .parser import deduplicate
from .ranking import calculate_average_latency, rank_final, rank_tcp
from .rolling import load_previous_top, prepare_retest_candidates, save_previous_top
from .speed_test import meets_minimum_speed, test_speed
from .tcp_scan import scan_tcp
from .tls_check import check_tls


def _gate(name: str, actual: int, minimum: int) -> dict[str, Any]:
    return {"name": name, "actual": actual, "minimum": minimum, "passed": actual >= minimum}


def _load_ip_snapshot(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    if path.suffix.lower() == ".gz":
        text = gzip.decompress(path.read_bytes()).decode("utf-8")
    else:
        text = path.read_text(encoding="utf-8")
    return {line.strip() for line in text.splitlines() if line.strip()}


def _save_ip_snapshot(path: Path, values: set[str]) -> None:
    text = "\n".join(sorted(values)) + ("\n" if values else "")
    if path.suffix.lower() == ".gz":
        atomic_write_bytes(path, gzip.compress(text.encode("utf-8"), compresslevel=9, mtime=0))
    else:
        atomic_write_text(path, text)


def _three_metric_checks(
    records: list[NodeResult], *, domain: str, pipeline: dict[str, Any],
    user_agent: str, locations: dict[str, Any],
    on_result: Callable[[NodeResult, str, bool], None] | None = None,
    on_stage: Callable[[list[NodeResult], str], None] | None = None,
) -> tuple[list[NodeResult], dict[str, Any]]:
    started = time.monotonic()

    def emit(node: NodeResult, stage: str, passed: bool) -> None:
        if on_result is None:
            return
        try:
            on_result(node, stage, passed)
        except Exception:
            # UI telemetry is best-effort and must never change selection.
            pass

    def emit_stage(stage_records: list[NodeResult], stage: str) -> None:
        if on_stage is None:
            return
        try:
            on_stage(stage_records, stage)
        except Exception:
            # UI telemetry is best-effort and must never change selection.
            pass

    emit_stage(records, "TLS")
    if on_result is None:
        tls_valid = asyncio.run(check_tls(records, domain, pipeline["tls"]))
    else:
        tls_valid = asyncio.run(check_tls(
            records,
            domain,
            pipeline["tls"],
            on_result=lambda node: emit(node, "TLS", node.tls_ok),
        ))
    emit_stage(tls_valid, "HTTPS TTFB")
    if on_result is None:
        http_valid = asyncio.run(check_http(
            tls_valid, domain, pipeline["http"], pipeline.get("websocket", {}),
            user_agent=user_agent,
        ))
    else:
        http_valid = asyncio.run(check_http(
            tls_valid,
            domain,
            pipeline["http"],
            pipeline.get("websocket", {}),
            user_agent=user_agent,
            on_result=lambda node: emit(node, "HTTPS TTFB", node.http_ok),
        ))
    enrich_locations(http_valid, locations)
    maximum_combined = float(pipeline["maximum_combined_latency_ms"])
    legacy_component = float(pipeline["maximum_component_latency_ms"])
    component_limits = (
        float(pipeline.get("quality_tcp", {}).get("maximum_average_latency_ms", legacy_component)),
        float(pipeline.get("tls", {}).get("maximum_average_latency_ms", legacy_component)),
        float(pipeline.get("http", {}).get("maximum_average_ttfb_ms", legacy_component)),
    )
    maximum_jitter = float(pipeline["maximum_jitter_ms"])
    location_filter = pipeline.get("location_filter", {})
    excluded_countries = {
        str(value).upper() for value in location_filter.get("excluded_countries", ["CN"])
    }
    require_country = bool(location_filter.get("require_known_endpoint_country", True))
    require_colo_country = bool(location_filter.get("require_known_colo_country", True))
    combined_valid: list[NodeResult] = []
    for node in http_valid:
        calculate_average_latency(node)
        country_valid = bool(node.country) or not require_country
        colo_country_valid = bool(node.colo_country) or not require_colo_country
        foreign_valid = (
            country_valid
            and colo_country_valid
            and node.country not in excluded_countries
            and node.colo_country not in excluded_countries
        )
        components = (node.tcp_latency_ms, node.tls_latency_ms, node.http_latency_ms)
        component_valid = all(
            value is not None and value <= limit
            for value, limit in zip(components, component_limits, strict=True)
        )
        jitter_valid = (
            node.overall_jitter_ms is not None
            and node.overall_jitter_ms <= maximum_jitter
        )
        latency_valid = (
            node.average_latency_ms is not None
            and node.average_latency_ms <= maximum_combined
        )
        combined_passed = foreign_valid and component_valid and jitter_valid and latency_valid
        if combined_passed:
            combined_valid.append(node)
        elif not foreign_valid:
            node.add_error(
                "location",
                f"已排除端点/机房: {node.country or 'unknown'}/{node.colo_country or 'unknown'}",
            )
        elif not component_valid:
            node.add_error(
                "latency",
                f"单项延迟超过自定义上限 {component_limits}: {components}",
            )
        elif not jitter_valid:
            node.add_error(
                "jitter",
                f"最大抖动 {node.overall_jitter_ms}ms > {maximum_jitter:g}ms",
            )
        else:
            node.add_error(
                "latency",
                f"TCP/TLS/TTFB 综合平均 {node.average_latency_ms}ms > {maximum_combined:g}ms",
            )
        emit(node, "综合规则", combined_passed)
    return combined_valid, {
        "tls_three_pass_success": len(tls_valid),
        "https_ttfb_three_pass_success": len(http_valid),
        "foreign_combined_latency_qualified": len(combined_valid),
        "tls_and_https_duration_seconds": round(time.monotonic() - started, 3),
    }


def _speed_checks(
    records: list[NodeResult], *, pipeline: dict[str, Any], user_agent: str,
    probe_paths: list[Path], on_qualified: Callable[[NodeResult], None] | None = None,
    on_result: Callable[[NodeResult], None] | None = None,
) -> tuple[list[NodeResult], dict[str, Any]]:
    merge_probe_files(records, probe_paths)
    options = dict(pipeline["speed"])
    options["candidates"] = len(records)
    full_started = time.monotonic()
    minimum_mbps = float(options["minimum_mbps"])

    def report_result(node: NodeResult) -> None:
        if on_result is not None:
            try:
                on_result(node)
            except Exception:
                # UI telemetry is best-effort and must never change selection.
                pass
        if on_qualified is not None and meets_minimum_speed(
            node, minimum_mbps=minimum_mbps
        ):
            on_qualified(node)

    tested = asyncio.run(test_speed(
        records,
        options,
        user_agent=user_agent,
        on_result=report_result,
    ))
    full_seconds = time.monotonic() - full_started
    qualified = [node for node in tested if meets_minimum_speed(
        node, minimum_mbps=minimum_mbps
    )]
    return qualified, {
        "speed_tested_once": len(tested),
        "speed_duration_seconds": round(full_seconds, 3),
        "speed_at_least_minimum": len(qualified),
    }


def _rank_source_country_tcp_speed(
    records: Iterable[NodeResult], *, count: int
) -> list[NodeResult]:
    """Rank the link-provided JP lane using only TCPing and download results.

    Failed measurements stay in the ordering instead of being filtered out, so
    the lane can still select the best available ten from the two source feeds.
    Multiple ports for the same IP compete and only the best endpoint survives.
    """
    ordered = sorted(
        records,
        key=lambda node: (
            node.tcp_latency_ms is None or node.speed_mbps is None,
            node.tcp_loss_rate,
            -(node.speed_mbps if node.speed_mbps is not None else -1.0),
            node.tcp_latency_ms if node.tcp_latency_ms is not None else 999999.0,
            node.tcp_jitter_ms if node.tcp_jitter_ms is not None else 999999.0,
            node.ip,
            node.port,
        ),
    )
    selected: list[NodeResult] = []
    seen: set[str] = set()
    for node in ordered:
        if node.ip in seen:
            continue
        selected.append(node)
        seen.add(node.ip)
        if len(selected) >= count:
            break
    return selected


def _source_country_tcp_speed_checks(
    records: list[NodeResult], *, pipeline: dict[str, Any], rule: dict[str, Any],
    user_agent: str, probe_paths: list[Path], count: int,
    on_tcp_result: Callable[[NodeResult], None] | None = None,
    on_speed_result: Callable[[NodeResult], None] | None = None,
    on_stage: Callable[[list[NodeResult], str], None] | None = None,
) -> tuple[list[NodeResult], dict[str, Any]]:
    """Measure the special JP lane once without TLS or HTTPS-TTFB gates."""
    started = time.monotonic()
    tcp_options = dict(pipeline["prefilter_tcp"])
    tcp_options.update({
        "attempts": int(rule.get("tcp_attempts", 3)),
        "require_all_attempts": False,
        "stop_on_failure": False,
        "stop_when_average_impossible": False,
        "return_all": True,
    })
    tcp_options.pop("maximum_average_latency_ms", None)
    tcp_options.pop("maximum_jitter_ms", None)
    if on_stage is not None:
        try:
            on_stage(records, "TCPing")
        except Exception:
            pass
    if on_tcp_result is None:
        tcp_tested = asyncio.run(scan_tcp(records, tcp_options))
    else:
        tcp_tested = asyncio.run(scan_tcp(
            records,
            tcp_options,
            on_result=on_tcp_result,
        ))

    merge_probe_files(tcp_tested, probe_paths)
    speed_options = dict(pipeline["speed"])
    speed_options["candidates"] = len(tcp_tested)
    if on_stage is not None:
        try:
            on_stage(tcp_tested, "下载测速")
        except Exception:
            pass
    speed_kwargs: dict[str, Any] = {}
    if on_speed_result is not None:
        speed_kwargs["on_result"] = on_speed_result
    speed_tested = asyncio.run(test_speed(
        tcp_tested,
        speed_options,
        user_agent=user_agent,
        probe_name="jp_source_speed",
        **speed_kwargs,
    ))
    selected = _rank_source_country_tcp_speed(speed_tested, count=count)
    return selected, {
        "input_endpoints": len(records),
        "input_unique_ips": len({node.ip for node in records}),
        "tcping_attempts_per_endpoint": int(tcp_options["attempts"]),
        "tcping_measured": sum(node.tcp_latency_ms is not None for node in tcp_tested),
        "download_tested_once": len(speed_tested),
        "download_measured": sum(node.speed_mbps is not None for node in speed_tested),
        "selected_unique_ips": len(selected),
        "duration_seconds": round(time.monotonic() - started, 3),
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


def _country_count(records: Iterable[NodeResult], country: str) -> int:
    normalized = country.upper()
    return sum((node.country or node.country_hint).upper() == normalized for node in records)


def _country_quotas_met(records: list[NodeResult], minimums: dict[str, int]) -> bool:
    return all(_country_count(records, country) >= minimum for country, minimum in minimums.items())


def _country_of(node: NodeResult) -> str:
    return str(node.country or node.country_hint or "").upper()


def _final_loss_allowed(
    node: NodeResult, *, pipeline: dict[str, Any], source_country: str
) -> bool:
    """Apply the hard packet-loss gate to ordinary nodes, not the JP lane."""
    if _country_of(node) == source_country.upper():
        return True
    maximum = float(pipeline.get("maximum_loss_rate", 0.30))
    return node.tcp_loss_rate <= maximum


def _final_ordinary_quality_allowed(
    node: NodeResult, *, pipeline: dict[str, Any], source_country: str
) -> bool:
    """Reapply the active local rules to fresh and accumulated ordinary nodes."""
    if _country_of(node) == source_country.upper():
        return True

    component_default = float(pipeline.get("maximum_component_latency_ms", 300.0))
    tcp_limit = float(
        pipeline.get("quality_tcp", {}).get("maximum_average_latency_ms", component_default)
    )
    tls_limit = float(
        pipeline.get("tls", {}).get("maximum_average_latency_ms", component_default)
    )
    http_limit = float(
        pipeline.get("http", {}).get("maximum_average_ttfb_ms", component_default)
    )
    average_limit = float(pipeline.get("maximum_combined_latency_ms", 300.0))
    jitter_limit = float(pipeline.get("maximum_jitter_ms", 500.0))
    loss_limit = float(pipeline.get("maximum_loss_rate", 0.30))
    speed_limit = float(pipeline.get("speed", {}).get("minimum_mbps", 3.0))

    return (
        node.tcp_latency_ms is not None
        and node.tcp_latency_ms <= tcp_limit
        and node.tls_latency_ms is not None
        and node.tls_latency_ms <= tls_limit
        and node.http_latency_ms is not None
        and node.http_latency_ms <= http_limit
        and node.average_latency_ms is not None
        and node.average_latency_ms <= average_limit
        and node.overall_jitter_ms is not None
        and node.overall_jitter_ms <= jitter_limit
        and node.tcp_loss_rate <= loss_limit
        and node.speed_mbps is not None
        and node.speed_mbps >= speed_limit
    )


def _final_country_allowed(
    node: NodeResult, *, pipeline: dict[str, Any], source_country: str
) -> bool:
    """Apply a final country guard independently of earlier probe stages."""
    location_filter = pipeline.get("location_filter", {})
    excluded = {
        str(value).upper()
        for value in location_filter.get("excluded_countries", ["CN"])
    }
    country = _country_of(node)
    hint = str(node.country_hint or "").upper()
    colo_country = str(node.colo_country or "").upper()
    if country in excluded or hint in excluded or colo_country in excluded:
        return False
    # The JP source lane deliberately skips HTTPS trace, so it has no colo.
    if country == source_country.upper() and hint == source_country.upper():
        return True
    if location_filter.get("require_known_endpoint_country", True) and not country:
        return False
    if location_filter.get("require_known_colo_country", True) and not colo_country:
        return False
    return True


def _has_explicit_excluded_country(node: NodeResult, pipeline: dict[str, Any]) -> bool:
    excluded = {
        str(value).upper()
        for value in pipeline.get("location_filter", {}).get("excluded_countries", ["CN"])
    }
    return any(
        value in excluded
        for value in (
            _country_of(node),
            str(node.country_hint or "").upper(),
            str(node.colo_country or "").upper(),
        )
        if value
    )


def _competition_candidates(
    current: Iterable[NodeResult],
    previous: Iterable[NodeResult],
    source_country_quality: Iterable[NodeResult],
    *,
    source_country: str,
    source_country_count: int,
) -> list[NodeResult]:
    """Keep the required country lane sourced only from the configured links."""
    normalized = source_country.upper()
    preferred = rank_final(source_country_quality, count=source_country_count)
    preferred_ips = {item.ip for item in preferred}
    general = [
        node
        for node in [*current, *previous]
        if _country_of(node) != normalized and node.ip not in preferred_ips
    ]
    return [*preferred, *general]


def _fresh_endpoint_candidates(
    records: Iterable[NodeResult], *, source: str
) -> list[NodeResult]:
    fresh: list[NodeResult] = []
    for original in records:
        node = NodeResult(
            ip=original.ip,
            port=original.port,
            country_hint=original.country_hint or original.country,
        )
        for label in original.sources:
            node.add_source(label)
        node.add_source(source)
        fresh.append(node)
    return fresh


def run_pipeline(config: dict[str, Any]) -> dict[str, Any]:
    started = datetime.now(UTC)
    monotonic_started = time.monotonic()
    pipeline = config["pipeline"]
    source_priority = [
        str(value)
        for value in pipeline.get("source_priority", [])
        if str(value).strip()
    ]
    domain = str(config["project"]["target_domain"])
    user_agent = str(config["project"].get("user_agent", "Noode-CG/11.2"))
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
    local_counts: dict[str, int] = {}
    for configured in config["sources"].get("local", []):
        entry = {"path": configured} if isinstance(configured, str) else configured
        label = str(entry.get("name") or entry.get("path") or "local")
        local_counts[label] = sum(label in node.sources for node in source_candidates)
    report["counts"]["local_sources"] = local_counts
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
    previous_ips = {node.ip for node in previous}
    excluded_official_ips = (
        {node.ip for node in source_candidates} | prior_official_ips | previous_ips
    )
    current_official_ips: set[str] = set()
    tested_current_ips: set[str] = set()
    metric_qualified: dict[str, NodeResult] = {}
    speed_processed_ips: set[str] = set()
    current_quality: dict[str, NodeResult] = {}
    previous_quality: dict[str, NodeResult] = {}
    source_country_quality: dict[str, NodeResult] = {}
    selected: list[NodeResult] = []
    round_index = speed_batch_index = 0
    prefilter_target = int(pipeline["prefilter_shortlist"])
    final_target = int(pipeline["current_selection"])
    speed_batch_size = int(pipeline["speed_batch_size"])
    country_minimums = {
        str(country).upper(): int(minimum)
        for country, minimum in pipeline.get("country_minimums", {}).items()
    }
    source_country_rule = pipeline.get("jp_source_requirement", {})
    source_country = str(source_country_rule.get("country", "JP")).upper()
    source_country_target = int(
        source_country_rule.get("count", country_minimums.get(source_country, 0))
    )
    speed_country_reserve = {
        str(country).upper(): int(minimum)
        for country, minimum in pipeline.get("speed_country_reserve", {}).items()
    }
    max_runtime = float(pipeline["max_runtime_seconds"])
    reserve = float(pipeline["postprocess_reserve_seconds"])
    minimum_round_budget = float(pipeline["minimum_round_budget_seconds"])
    max_official_rounds = int(pipeline.get("max_official_rounds", 5))
    locations = load_locations(resolve_path(config, config["paths"]["locations"]))
    probe_paths = [resolve_path(config, path) for path in config.get("vantage", {}).get("probe_files", [])]
    prefilter_country_reserve = {
        str(country).upper(): int(minimum)
        for country, minimum in pipeline.get("prefilter_country_reserve", {}).items()
    }
    prefilter_shortlisted_max = 0
    final_country_rejected_ips: set[str] = set()
    final_loss_rejected_ips: set[str] = set()

    def final_country_eligible(records: Iterable[NodeResult]) -> list[NodeResult]:
        accepted: list[NodeResult] = []
        for node in records:
            country_allowed = _final_country_allowed(
                node,
                pipeline=pipeline,
                source_country=source_country,
            )
            loss_allowed = _final_loss_allowed(
                node,
                pipeline=pipeline,
                source_country=source_country,
            )
            if country_allowed and loss_allowed:
                accepted.append(node)
            else:
                if not country_allowed:
                    final_country_rejected_ips.add(node.ip)
                if not loss_allowed:
                    final_loss_rejected_ips.add(node.ip)
        return accepted

    # The required JP lane is sourced only from the two configured link feeds.
    # It is a separate ranking lane: measure TCPing three times and download
    # once, skip TLS/HTTPS-TTFB, then retain the best ten unique IPs even when
    # they do not meet the normal pool's strict quality thresholds.
    source_country_candidates = [
        node for node in source_candidates if _country_of(node) == source_country
    ]
    tested_current_ips.update(node.ip for node in source_country_candidates)
    source_country_measured = _fresh_endpoint_candidates(
        source_country_candidates,
        source=f"{source_country.lower()}-source-tcp-speed",
    )
    source_country_selected, source_country_counts = _source_country_tcp_speed_checks(
        source_country_measured,
        pipeline=pipeline,
        rule=source_country_rule,
        user_agent=user_agent,
        probe_paths=probe_paths,
        count=source_country_target,
    )
    source_country_quality = {node.ip: node for node in source_country_selected}
    speed_processed_ips.update(node.ip for node in source_country_candidates)

    report["source_country_lane"] = {
        "country": source_country,
        "required": source_country_target,
        "link_candidates": len(source_country_candidates),
        "link_unique_ips": len({node.ip for node in source_country_candidates}),
        "test_count": 1,
        "tcping_only_plus_download": True,
        "tls_skipped": True,
        "https_ttfb_skipped": True,
        "measurements": source_country_counts,
        "qualified": len(source_country_quality),
        "selected": len(source_country_quality),
    }

    report["counts"].update({
        "jp_source_candidates": len(source_country_candidates),
        "jp_source_qualified": len(source_country_quality),
        "jp_source_selected": len(source_country_quality),
        "jp_source_test_attempts": 1,
    })
    if len(source_country_quality) < source_country_target:
        report["warnings"].append(
            f"两个指定链接中只有 {len(source_country_quality)}/{source_country_target} 个不同的 "
            f"{source_country} 候选；无法满足固定数量，停止官方池补测并保留旧订阅"
        )

    # The previous TOP100 has its own lane: one strict TCP/TLS/TTFB/download
    # recheck, then it competes with current results without being tested again.
    # Explicit old CN labels are discarded immediately. Unknown old records
    # still receive their one promised re-test, which can establish a valid
    # endpoint/colo country before final competition.
    previous_general = [
        node
        for node in previous
        if _country_of(node) != source_country
        and not _has_explicit_excluded_country(node, pipeline)
    ]
    if previous_general:
        previous_retest = prepare_retest_candidates([], previous_general)
        previous_tcp = asyncio.run(scan_tcp(previous_retest, pipeline["quality_tcp"]))
        previous_metrics, metric_counts = _three_metric_checks(
            previous_tcp,
            domain=domain,
            pipeline=pipeline,
            user_agent=user_agent,
            locations=locations,
        )
        previous_passed, speed_counts = _speed_checks(
            previous_metrics,
            pipeline=pipeline,
            user_agent=user_agent,
            probe_paths=probe_paths,
        )
        _keep_best_by_ip(previous_quality, previous_passed)
        report["rolling_attempts"].append({
            **metric_counts,
            **speed_counts,
            "attempt": 1,
            "input": len(previous_retest),
            "tcp_five_probe_success": len(previous_tcp),
            "previous_tested_this_attempt": len(previous_retest),
            "previous_reverified": len(previous_quality),
        })

    while len(source_country_quality) >= source_country_target:
        combined = final_country_eligible(_competition_candidates(
            current_quality.values(),
            previous_quality.values(),
            source_country_quality.values(),
            source_country=source_country,
            source_country_count=source_country_target,
        ))
        candidate_selection = rank_final(
            combined,
            count=final_target,
            minimum_by_country=country_minimums,
            source_priority=source_priority,
        )
        if (
            round_index > 0
            and
            len(candidate_selection) >= final_target
            and _country_quotas_met(candidate_selection, country_minimums)
        ):
            selected = candidate_selection
            break

        if round_index >= max_official_rounds:
            report["warnings"].append(
                f"已达到最多 {max_official_rounds} 轮官方候选补测限制，保留旧订阅"
            )
            break

        remaining = max_runtime - (time.monotonic() - monotonic_started)
        if remaining < minimum_round_budget + reserve:
            report["warnings"].append(
                "剩余时间不足以完成新的官方 50,000 候选批次，保留上一版订阅"
            )
            break

        official, warnings = collect_official_batch(
            config,
            exclude_ips=excluded_official_ips | current_official_ips | tested_current_ips,
            round_index=round_index,
        )
        report["warnings"].extend(warnings)
        wanted = int(config["sources"]["cloudflare_ranges"]["official_batch_size"])
        if len({node.ip for node in official}) < wanted:
            report["warnings"].append("官方段无法再提供完整的 50,000 个不重复候选")
            break
        current_official_ips.update(node.ip for node in official)
        raw_batch = deduplicate([*source_candidates, *official]) if round_index == 0 else official
        batch = [
            node
            for node in raw_batch
            if node.ip not in previous_ips and node.ip not in tested_current_ips
        ]
        tested_current_ips.update(node.ip for node in batch)

        prefilter_started = time.monotonic()
        prefilter_passed = asyncio.run(scan_tcp(batch, pipeline["prefilter_tcp"]))
        shortlist = rank_tcp(
            prefilter_passed,
            count=prefilter_target,
            minimum_by_country=prefilter_country_reserve,
            source_priority=source_priority,
        )
        prefilter_seconds = time.monotonic() - prefilter_started
        prefilter_shortlisted_max = max(prefilter_shortlisted_max, len(shortlist))

        quality_tcp_started = time.monotonic()
        quality_tcp = asyncio.run(scan_tcp(shortlist, pipeline["quality_tcp"]))
        quality_tcp_seconds = time.monotonic() - quality_tcp_started
        metrics, metric_counts = _three_metric_checks(
            quality_tcp,
            domain=domain,
            pipeline=pipeline,
            user_agent=user_agent,
            locations=locations,
        )
        _keep_best_by_ip(metric_qualified, metrics)
        metric_counts.update({
            "batch": round_index + 1,
            "input": len(shortlist),
            "quality_tcp_five_probe_success": len(quality_tcp),
            "three_metric_qualified_total": len(metric_qualified),
        })
        report["metric_batches"].append(metric_counts)
        write_checkpoint(
            checkpoint_dir / "02-three-metric-qualified.json",
            rank_final(metric_qualified.values(), count=len(metric_qualified)),
        )

        untested_speed = [node for node in metrics if node.ip not in speed_processed_ips]
        while untested_speed:
            if max_runtime - (time.monotonic() - monotonic_started) < reserve:
                report["warnings"].append("已为输出阶段保留时间，停止新的下载测速")
                untested_speed = []
                break
            chunk = rank_final(
                untested_speed,
                count=min(speed_batch_size, len(untested_speed)),
                minimum_by_country=speed_country_reserve,
                source_priority=source_priority,
            )
            speed_batch_index += 1
            qualified, counts = _speed_checks(
                chunk,
                pipeline=pipeline,
                user_agent=user_agent,
                probe_paths=probe_paths,
            )
            speed_processed_ips.update(node.ip for node in chunk)
            _keep_best_by_ip(current_quality, qualified)
            counts.update({
                "batch": speed_batch_index,
                "round": round_index + 1,
                "input": len(chunk),
                "current_speed_qualified_total": len(current_quality),
            })
            report["speed_batches"].append(counts)
            combined = final_country_eligible(_competition_candidates(
                current_quality.values(),
                previous_quality.values(),
                source_country_quality.values(),
                source_country=source_country,
                source_country_count=source_country_target,
            ))
            candidate_selection = rank_final(
                combined,
                count=final_target,
                minimum_by_country=country_minimums,
                source_priority=source_priority,
            )
            if (
                len(candidate_selection) >= final_target
                and _country_quotas_met(candidate_selection, country_minimums)
            ):
                selected = candidate_selection
                break
            untested_speed = [
                node for node in untested_speed if node.ip not in speed_processed_ips
            ]

        round_index += 1
        report["rounds"].append({
            "round": round_index,
            "link_endpoints": len(source_candidates) if round_index == 1 else 0,
            "official_unique_ips": len({node.ip for node in official}),
            "input": len(batch),
            "prefilter_tcp_three_pass_success_under_1000ms": len(prefilter_passed),
            "prefilter_shortlisted": len(shortlist),
            "quality_tcp_five_probe_success_under_300ms": len(quality_tcp),
            "three_metric_qualified": len(metrics),
            "current_speed_qualified_total": len(current_quality),
            "prefilter_duration_seconds": round(prefilter_seconds, 3),
            "quality_tcp_duration_seconds": round(quality_tcp_seconds, 3),
        })
        print(
            f"第 {round_index} 轮: 输入={len(batch)}, 1000ms 初筛={len(prefilter_passed)}, "
            f"严格测试={len(shortlist)}, 下载合格累计={len(current_quality)}/{final_target}",
            flush=True,
        )
        if selected:
            break

    snapshot_values = current_official_ips or prior_official_ips
    _save_ip_snapshot(official_snapshot, snapshot_values)
    report["counts"].update({
        "official_previous_excluded": len(prior_official_ips),
        "official_sampled_this_run": len(current_official_ips),
        "official_snapshot_saved": len(snapshot_values),
        "official_rounds": round_index,
        "prefilter_shortlisted_max": prefilter_shortlisted_max,
        "current_unique_tested": len(tested_current_ips),
        "three_metric_qualified": len(metric_qualified),
        "speed_processed": len(speed_processed_ips),
        "speed_at_least_minimum": len(current_quality),
        "previous_retested_once": len(previous_general),
        "previous_reverified": len(previous_quality),
        "previous_in_final": sum(node.ip in previous_ips for node in selected),
        "final_country_candidates_rejected": len(final_country_rejected_ips),
        "final_packet_loss_candidates_rejected": len(final_loss_rejected_ips),
        "final_forbidden_country_count": sum(
            not _final_country_allowed(
                node,
                pipeline=pipeline,
                source_country=source_country,
            )
            for node in selected
        ),
    })
    selected_source_counts = {
        label: sum(label in node.sources for node in selected)
        for label in source_priority
    }
    local_candidate_count = sum(local_counts.values())
    report["selection_vantage"] = {
        "mode": "local-assisted" if local_candidate_count else "cloud-fallback",
        "local_candidates_loaded": local_candidate_count,
        "source_priority": source_priority,
        "selected_source_counts": selected_source_counts,
        "runner_only_selected": sum(
            not any(label in node.sources for label in source_priority)
            for node in selected
        ),
    }
    if not local_candidate_count:
        report["warnings"].append(
            "未加载 data/local-cfdata-candidates.txt；本次只能按链接来源和 GitHub "
            "Runner 线路兜底，不能代表订阅者本机延迟"
        )
    sampling_seed = config["sources"].get("cloudflare_ranges", {}).get("_resolved_sampling_seed")
    if sampling_seed is not None:
        report["counts"]["cloudflare_sampling_seed"] = sampling_seed
    final_gate_name = f"final_top{int(config['output']['top_nodes'])}"
    report["gates"].extend([
        _gate("prefilter_shortlist", prefilter_shortlisted_max, prefilter_target),
        _gate(final_gate_name, len(selected), int(config["output"]["top_nodes"])),
        *[
            _gate(f"final_country:{country}", _country_count(selected, country), minimum)
            for country, minimum in country_minimums.items()
        ],
        _gate(
            "final_no_forbidden_or_unknown_country",
            int(all(
                _final_country_allowed(
                    node,
                    pipeline=pipeline,
                    source_country=source_country,
                )
                for node in selected
            )),
            1,
        ),
        _gate(
            "final_ordinary_packet_loss_within_local_rule",
            int(all(
                _final_loss_allowed(
                    node,
                    pipeline=pipeline,
                    source_country=source_country,
                )
                for node in selected
            )),
            1,
        ),
    ])

    if selected and prefilter_shortlisted_max < prefilter_target:
        report["warnings"].append("未完成一批足量的 5,000 个初筛候选，保留旧订阅")
        selected = []
    if selected and not _country_quotas_met(selected, country_minimums):
        report["warnings"].append("最终地区最低数量未满足，保留旧订阅")
        selected = []
    if selected and not all(
        _final_country_allowed(
            node,
            pipeline=pipeline,
            source_country=source_country,
        )
        for node in selected
    ):
        report["warnings"].append("最终结果仍包含 CN 或未知地区，拒绝发布")
        selected = []
    if selected and not all(
        _final_loss_allowed(
            node,
            pipeline=pipeline,
            source_country=source_country,
        )
        for node in selected
    ):
        maximum_loss_percent = float(pipeline.get("maximum_loss_rate", 0.30)) * 100
        report["warnings"].append(
            f"最终普通节点仍包含丢包率超过自定义上限 {maximum_loss_percent:g}% 的地址，拒绝发布"
        )
        selected = []
    if selected and not baseline_passed:
        report["warnings"].append("Google、Cloudflare、GitHub 三次基线未全部通过，保留旧订阅")
        selected = []
    if selected:
        write_checkpoint(checkpoint_dir / f"04-{final_gate_name}.json", selected)
    report["status"] = "ok" if all(item["passed"] for item in report["gates"]) else "degraded"
    report["duration_seconds"] = round((datetime.now(UTC) - started).total_seconds(), 3)
    final_report = publish_outputs(output_dir, selected, report, config["output"])
    print(
        f"完成: status={final_report['status']} selected={len(selected)} "
        f"published={final_report['published']}", flush=True,
    )
    return final_report
