from __future__ import annotations

import asyncio
import gzip
import ipaddress
import json
import os
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .colo_detect import load_locations
from .config import resolve_path
from .exporter import publish_outputs
from .fetcher import collect_official_batch, collect_source_candidates
from .io_utils import atomic_write_bytes, atomic_write_json
from .models import NodeResult
from .pipeline import (
    _final_country_allowed,
    _final_ordinary_quality_allowed,
    _rank_source_country_tcp_speed,
    _source_country_tcp_speed_checks,
    _speed_checks,
    _three_metric_checks,
)
from .ranking import rank_final, rank_tcp
from .rolling import load_previous_top, save_previous_top
from .tcp_scan import scan_tcp

HANDOFF_SCHEMA = 1


def _handoff_node(node: NodeResult) -> dict[str, Any]:
    """Serialize only fields needed by the next local selection pass.

    Per-attempt probe traces are useful while a stage is running but make the
    Windows-to-GitHub result payload exceed the workflow control-channel
    limit.  Aggregate measurements retain everything needed for filtering,
    ranking, display, and subsequent accumulation.
    """
    return {
        "ip": node.ip,
        "port": node.port,
        "country_hint": node.country_hint,
        "sources": node.sources,
        "tcp_ok": node.tcp_ok,
        "tcp_latency_ms": node.tcp_latency_ms,
        "tcp_jitter_ms": node.tcp_jitter_ms,
        "tcp_loss_rate": node.tcp_loss_rate,
        "tls_ok": node.tls_ok,
        "tls_latency_ms": node.tls_latency_ms,
        "tls_jitter_ms": node.tls_jitter_ms,
        "tls_version": node.tls_version,
        "http_ok": node.http_ok,
        "http_status": node.http_status,
        "http_latency_ms": node.http_latency_ms,
        "http_jitter_ms": node.http_jitter_ms,
        "average_latency_ms": node.average_latency_ms,
        "overall_jitter_ms": node.overall_jitter_ms,
        "colo": node.colo,
        "colo_country": node.colo_country,
        "country": node.country,
        "region": node.region,
        "city": node.city,
        "speed_mbps": node.speed_mbps,
        "score": node.score,
    }


def _unique_by_ip(records: Iterable[NodeResult]) -> list[NodeResult]:
    unique: list[NodeResult] = []
    seen: set[str] = set()
    for node in records:
        if node.ip in seen:
            continue
        unique.append(node)
        seen.add(node.ip)
    return unique


def _fresh(records: Iterable[NodeResult], source: str) -> list[NodeResult]:
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


def _load_previous(config: dict[str, Any]) -> tuple[list[NodeResult], list[str]]:
    output = resolve_path(config, config["paths"]["output"])
    rolling = config.get("rolling", {})
    snapshot_value = rolling.get("snapshot_path")
    snapshot = resolve_path(config, snapshot_value) if snapshot_value else None
    return load_previous_top(
        output,
        snapshot,
        int(rolling.get("previous_limit", 100)),
    )


def _load_published_nodes(config: dict[str, Any]) -> list[NodeResult]:
    """Load the currently published ranking before this run overwrites it."""
    path = resolve_path(config, config["paths"]["output"]) / "nodes.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    return _nodes_from_payload(payload)


def _nodes_from_payload(value: Any) -> list[NodeResult]:
    if not isinstance(value, list):
        return []
    return _unique_by_ip(
        NodeResult.from_dict(item)
        for item in value
        if isinstance(item, dict)
    )


def _write_handoff(
    path: Path,
    nodes: list[NodeResult],
    report: dict[str, Any],
    *,
    state: dict[str, Any] | None = None,
) -> None:
    payload = {
        "schema": HANDOFF_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "report": report,
        "nodes": [_handoff_node(node) for node in nodes],
    }
    if state:
        payload["state"] = state
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    atomic_write_bytes(path, gzip.compress(encoded, compresslevel=9, mtime=0))


def _load_ip_set(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    payload = gzip.decompress(path.read_bytes()) if path.suffix.lower() == ".gz" else path.read_bytes()
    return {line.strip() for line in payload.decode("utf-8").splitlines() if line.strip()}


def _write_ip_set(path: Path, values: set[str]) -> None:
    text = "\n".join(sorted(values)) + ("\n" if values else "")
    payload = text.encode("utf-8")
    if path.suffix.lower() == ".gz":
        payload = gzip.compress(payload, compresslevel=9, mtime=0)
    atomic_write_bytes(path, payload)


def _rank_with_colo_diversity(
    records: Iterable[NodeResult],
    *,
    count: int,
    max_per_colo: int,
    latency_speed_first: bool = False,
) -> list[NodeResult]:
    prepared = list(records)
    ordered = rank_final(prepared, count=len(prepared))
    if latency_speed_first:
        ordered = sorted(
            ordered,
            key=lambda node: (
                node.average_latency_ms if node.average_latency_ms is not None else float("inf"),
                -(node.speed_mbps if node.speed_mbps is not None else -1.0),
                node.tcp_loss_rate,
                node.overall_jitter_ms if node.overall_jitter_ms is not None else float("inf"),
                node.ip,
            ),
        )
    if max_per_colo <= 0:
        return ordered[:count]
    selected: list[NodeResult] = []
    selected_ips: set[str] = set()
    colo_counts: dict[str, int] = {}
    for node in ordered:
        colo = (node.colo or "UNKNOWN").upper()
        if colo != "UNKNOWN" and colo_counts.get(colo, 0) >= max_per_colo:
            continue
        selected.append(node)
        selected_ips.add(node.ip)
        colo_counts[colo] = colo_counts.get(colo, 0) + 1
        if len(selected) >= count:
            return selected
    # The cap is deliberately soft: quality/quantity wins when too few colos
    # are reachable from the subscriber's actual network.
    for node in ordered:
        if node.ip in selected_ips:
            continue
        selected.append(node)
        if len(selected) >= count:
            break
    return selected


def _select_cloud_pool(
    passed: Iterable[NodeResult],
    *,
    linked_jp: Iterable[NodeResult],
    linked_ipv6: Iterable[NodeResult] = (),
    target: int,
    source_priority: list[str],
) -> list[NodeResult]:
    """Keep locally measured link candidates before filling the normal pool.

    A GitHub-hosted runner is a poor place to reject Japan candidates intended
    for a user's local route.  It also commonly has no usable IPv6 route, so
    IPv6 records that actually occur in the fixed links are passed through for
    the Windows runner to measure.  No synthetic official IPv6 pool is added.
    """
    reserved = _unique_by_ip([*linked_jp, *linked_ipv6])
    if len(reserved) >= target:
        return reserved[:target]
    reserved_ips = {node.ip for node in reserved}
    normal = rank_tcp(
        (node for node in passed if node.ip not in reserved_ips),
        count=target - len(reserved),
        source_priority=source_priority,
    )
    return [*reserved, *normal]


def load_cloud_handoff(path: str | Path) -> tuple[list[NodeResult], dict[str, Any]]:
    source = Path(path)
    if not source.is_file():
        raise ValueError(f"云端交接池不存在: {source}")
    try:
        payload = json.loads(gzip.decompress(source.read_bytes()).decode("utf-8"))
    except (OSError, ValueError, gzip.BadGzipFile) as exc:
        raise ValueError(f"云端交接池损坏: {exc}") from exc
    if payload.get("schema") != HANDOFF_SCHEMA or not isinstance(payload.get("nodes"), list):
        raise ValueError("云端交接池格式不受支持")
    return _nodes_from_payload(payload["nodes"]), payload


def prepare_cloud_handoff(config: dict[str, Any]) -> dict[str, Any]:
    """Cloud stage: reduce full feeds and official ranges to a fresh TOP pool."""
    started = datetime.now(UTC)
    handoff = config["handoff"]
    target = int(handoff.get("target", 5000))
    max_rounds = int(handoff.get("max_official_rounds", 5))
    pool_path = resolve_path(config, handoff["pool_path"])
    health_path = resolve_path(config, handoff["health_path"])
    pipeline = config["pipeline"]
    source_priority = [str(value) for value in pipeline.get("source_priority", [])]

    previous, warnings = _load_previous(config)
    previous_ips = {node.ip for node in previous}
    attempted_value = handoff.get("attempted_path")
    attempted_path = resolve_path(config, attempted_value) if attempted_value else None
    loaded_attempted_ips = _load_ip_set(attempted_path) if attempted_path else set()
    official_value = config.get("rolling", {}).get("official_snapshot_path")
    official_path = resolve_path(config, official_value) if official_value else None
    loaded_prior_official_ips = _load_ip_set(official_path) if official_path else set()
    continuation = (
        os.getenv("NOODE_CONTINUATION", "").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    # Only an explicitly dispatched replenishment belongs to the same cycle.
    # A scheduled/manual fresh run may reuse addresses from an older cycle.
    attempted_ips = loaded_attempted_ips if continuation else set()
    prior_official_ips = loaded_prior_official_ips if continuation else set()
    sources, source_warnings = collect_source_candidates(config)
    warnings.extend(source_warnings)
    source_country = str(
        pipeline.get("jp_source_requirement", {}).get("country", "JP")
    ).upper()
    linked_jp = [
        node
        for node in sources
        if (node.country_hint or node.country).upper() == source_country
        and node.ip not in previous_ips
        and node.ip not in attempted_ips
    ]
    linked_ipv6 = [
        node
        for node in sources
        if ipaddress.ip_address(node.ip).version == 6
        and node.ip not in previous_ips
        and node.ip not in attempted_ips
    ]
    locally_reserved_ips = {node.ip for node in [*linked_jp, *linked_ipv6]}
    cloud_test_sources = [
        node for node in sources if node.ip not in locally_reserved_ips
    ]
    source_ips = {node.ip for node in sources}
    tested_ips: set[str] = set(attempted_ips)
    official_ips: set[str] = set()
    passed_by_ip: dict[str, NodeResult] = {}
    rounds: list[dict[str, Any]] = []

    for round_index in range(max_rounds):
        official, official_warnings = collect_official_batch(
            config,
            exclude_ips=(
                previous_ips | source_ips | attempted_ips | prior_official_ips
                | tested_ips | official_ips
            ),
            round_index=round_index,
        )
        warnings.extend(official_warnings)
        official_ips.update(node.ip for node in official)
        raw = [*cloud_test_sources, *official] if round_index == 0 else official
        batch = [
            node
            for node in _unique_by_ip(raw)
            if node.ip not in previous_ips and node.ip not in tested_ips
        ]
        if not batch:
            rounds.append({
                "round": round_index + 1,
                "input": 0,
                "tcp_qualified": 0,
                "qualified_total": len(passed_by_ip),
                "shortlisted": len(_select_cloud_pool(
                    passed_by_ip.values(),
                    linked_jp=linked_jp,
                    linked_ipv6=linked_ipv6,
                    target=target,
                    source_priority=source_priority,
                )),
            })
            warning = (
                f"云端第 {round_index + 1} 轮没有新的唯一候选，提前结束补池"
            )
            warnings.append(warning)
            print(warning, flush=True)
            break
        tested_ips.update(node.ip for node in batch)
        passed = asyncio.run(scan_tcp(_fresh(batch, "cloud-prefilter"), pipeline["prefilter_tcp"]))
        for node in passed:
            existing = passed_by_ip.get(node.ip)
            if existing is None or (
                node.tcp_latency_ms is not None
                and (existing.tcp_latency_ms is None or node.tcp_latency_ms < existing.tcp_latency_ms)
            ):
                passed_by_ip[node.ip] = node
        shortlist = _select_cloud_pool(
            passed_by_ip.values(),
            linked_jp=linked_jp,
            linked_ipv6=linked_ipv6,
            target=target,
            source_priority=source_priority,
        )
        rounds.append({
            "round": round_index + 1,
            "input": len(batch),
            "tcp_qualified": len(passed),
            "qualified_total": len(passed_by_ip),
            "shortlisted": len(shortlist),
        })
        print(
            f"云端第 {round_index + 1} 轮: 输入={len(batch)} "
            f"TCP合格累计={len(passed_by_ip)} 交接池={len(shortlist)}/{target}",
            flush=True,
        )
        if len(shortlist) >= target:
            break

    selected = _select_cloud_pool(
        passed_by_ip.values(),
        linked_jp=linked_jp,
        linked_ipv6=linked_ipv6,
        target=target,
        source_priority=source_priority,
    )
    status = "ok" if len(selected) >= target else "degraded"
    report = {
        "status": status,
        "stage": "cloud-prepare",
        "started_at": started.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "target": target,
        "selected": len(selected),
        "previous_excluded": len(previous_ips),
        "attempted_excluded": len(attempted_ips),
        "prior_official_excluded": len(prior_official_ips),
        "continuation": continuation,
        "stale_attempted_ignored": (
            len(loaded_attempted_ips) if not continuation else 0
        ),
        "stale_official_ignored": (
            len(loaded_prior_official_ips) if not continuation else 0
        ),
        "source_candidates": len(sources),
        "linked_jp_reserved_for_local": len(linked_jp),
        "linked_ipv6_reserved_for_local": len(linked_ipv6),
        "official_sampled": len(official_ips),
        "tested_unique": len(tested_ips),
        "rounds": rounds,
        "warnings": warnings,
    }
    if status == "ok":
        accumulator_value = handoff.get("accumulator_path")
        accumulator_path = (
            resolve_path(config, accumulator_value) if accumulator_value else None
        )
        accumulated: list[NodeResult] = []
        if accumulator_path and accumulator_path.is_file():
            accumulated, _ = load_cloud_handoff(accumulator_path)
        _write_handoff(
            pool_path,
            selected,
            report,
            state={
                "previous_top100": [node.to_dict() for node in previous],
                "accumulated": [node.to_dict() for node in accumulated],
                "attempted_ips": sorted(attempted_ips),
            },
        )
    else:
        report["warnings"].append("新交接池不足目标数量，保留上一版云端交接文件")
    atomic_write_json(health_path, report)
    if official_path:
        snapshot = prior_official_ips | official_ips if continuation else official_ips
        _write_ip_set(official_path, snapshot)
    return report


def run_local_selection(config: dict[str, Any]) -> dict[str, Any]:
    """Windows self-hosted stage: remeasure cloud TOP5000 + previous TOP100."""
    started = datetime.now(UTC)
    handoff_path = resolve_path(config, config["handoff"]["pool_path"])
    force_marker_path = handoff_path.parent / "force-rerank.json"
    stop_marker_path = handoff_path.parent / "stop-after-current.json"
    if stop_marker_path.is_file():
        output_options = config["output"]
        total_target = int(output_options["top_nodes"])
        source_rule = config["pipeline"].get("jp_source_requirement", {})
        source_target = int(source_rule.get("count", 10))
        report = {
            "status": "stopped",
            "stage": "local-self-hosted-selection",
            "started_at": started.isoformat(),
            "generated_at": datetime.now(UTC).isoformat(),
            "vantage": "windows-self-hosted-local-network",
            "counts": {
                "general_target": total_target,
                "final_target": total_target + source_target,
                "final_selected": 0,
            },
            "warnings": ["已按本地停止请求结束本轮；保留上一版订阅且不再自动补池"],
            "needs_more": False,
            "stopped_by_user": True,
            "local_rules": config.get("_local_rules", {}),
        }
        stop_marker_path.unlink(missing_ok=True)
        return publish_outputs(
            resolve_path(config, config["paths"]["output"]),
            [],
            report,
            output_options,
        )
    force_rerank = force_marker_path.is_file()
    cloud_nodes, cloud_payload = load_cloud_handoff(handoff_path)
    cloud_report = cloud_payload.get("report", {})
    continuation = bool(
        cloud_report.get("continuation", False)
        if isinstance(cloud_report, dict)
        else False
    )
    local_previous, warnings = _load_previous(config)
    published_previous = _load_published_nodes(config)
    embedded_state = cloud_payload.get("state", {})
    if not isinstance(embedded_state, dict):
        embedded_state = {}
    embedded_previous = _nodes_from_payload(embedded_state.get("previous_top100"))
    previous_limit = int(config.get("rolling", {}).get("previous_limit", 100))
    previous = _unique_by_ip([*local_previous, *embedded_previous])[:previous_limit]
    handoff = config["handoff"]
    accumulator_value = handoff.get("accumulator_path")
    accumulator_path = resolve_path(config, accumulator_value) if accumulator_value else None
    live_value = handoff.get("live_results_path")
    live_path = resolve_path(config, live_value) if live_value else None
    if live_path and not continuation:
        live_path.unlink(missing_ok=True)
    attempted_value = handoff.get("attempted_path")
    attempted_path = resolve_path(config, attempted_value) if attempted_value else None
    accumulated: list[NodeResult] = []
    accumulator_payload: dict[str, Any] = {}
    if accumulator_path and accumulator_path.is_file():
        accumulated, accumulator_payload = load_cloud_handoff(accumulator_path)
    embedded_accumulated = _nodes_from_payload(embedded_state.get("accumulated"))
    accumulated = _unique_by_ip([*accumulated, *embedded_accumulated])
    attempted_ips = _load_ip_set(attempted_path) if attempted_path else set()
    embedded_attempted = embedded_state.get("attempted_ips")
    if isinstance(embedded_attempted, list):
        attempted_ips.update(
            str(value).strip()
            for value in embedded_attempted
            if isinstance(value, str) and value.strip()
        )
    # The cloud-side previous-official snapshot already retains every sampled
    # official address (and is committed between continuation rounds).  Sending
    # those addresses back again in the Runner output duplicated megabytes of
    # state and eventually exceeded the workflow control-channel limit.  Keep
    # the complete fixed-link snapshot instead: it prevents those mandatory
    # sources from being fetched/tested again during the same replenishment
    # cycle while the cloud snapshot handles official-address uniqueness.
    retained_source_ips: set[str] | None = None
    source_config = config.get("sources", {})
    if isinstance(source_config, dict) and (
        source_config.get("remote") or source_config.get("local")
    ):
        source_nodes, source_warnings = collect_source_candidates(config)
        warnings.extend(source_warnings)
        if source_nodes:
            retained_source_ips = {node.ip for node in source_nodes}
    incoming = _unique_by_ip([
        *_fresh(cloud_nodes, "cloud-handoff"),
        *_fresh(previous, "previous-top100"),
    ])
    accumulated_ips = {node.ip for node in accumulated}
    new_candidates = [
        node for node in incoming
        if node.ip not in attempted_ips
        and node.ip not in accumulated_ips
    ]
    combined = _unique_by_ip([*accumulated, *new_candidates])

    pipeline = config["pipeline"]
    output_options = config["output"]
    total_target = int(output_options["top_nodes"])
    minimum_general = min(
        total_target,
        int(output_options.get("minimum_publish", total_target)),
    )
    source_rule = pipeline.get("jp_source_requirement", {})
    source_country = str(source_rule.get("country", "JP")).upper()
    source_target = int(source_rule.get("count", 10))
    # output.top_nodes is the ordinary-node target. The JP lane is appended and
    # is intentionally exempt from the user's ordinary quality thresholds.
    general_target = total_target
    publish_target = total_target + source_target
    minimum_publish_target = minimum_general + source_target
    if general_target <= 0 or minimum_general <= 0:
        raise ValueError("最终总数必须大于 JP 保留数量")

    probe_paths = [
        resolve_path(config, path)
        for path in config.get("vantage", {}).get("probe_files", [])
    ]
    user_agent = str(config["project"].get("user_agent", "Noode-CG/local"))
    locations = load_locations(resolve_path(config, config["paths"]["locations"]))

    accumulated_jp = [
        node for node in accumulated
        if (node.country_hint or node.country).upper() == source_country
    ]
    jp_candidates = [
        node for node in new_candidates
        if (node.country_hint or node.country).upper() == source_country
    ]
    jp_new_selected, jp_counts = _source_country_tcp_speed_checks(
        jp_candidates,
        pipeline=pipeline,
        rule=source_rule,
        user_agent=user_agent,
        probe_paths=probe_paths,
        count=source_target,
    )
    jp_selected = _rank_source_country_tcp_speed(
        [*accumulated_jp, *jp_new_selected],
        count=source_target,
    )

    excluded = {
        str(value).upper()
        for value in pipeline.get("location_filter", {}).get("excluded_countries", ["CN"])
    }
    accumulated_general = [
        node for node in accumulated
        if (node.country_hint or node.country).upper() != source_country
    ]
    general_candidates = [
        node for node in new_candidates
        if (node.country_hint or node.country).upper() != source_country
        and (node.country_hint or node.country).upper() not in excluded
    ]
    speed_batch_size = int(pipeline.get("speed_batch_size", 400))
    speed_qualified: dict[str, NodeResult] = {
        node.ip: node for node in accumulated_general
    }
    speed_processed: set[str] = set()
    speed_batches: list[dict[str, Any]] = []
    def current_eligible_general() -> list[NodeResult]:
        return [
            node for node in speed_qualified.values()
            if _final_country_allowed(
                node,
                pipeline=pipeline,
                source_country=source_country,
            )
            and _final_ordinary_quality_allowed(
                node,
                pipeline=pipeline,
                source_country=source_country,
            )
        ]

    def write_live_preview() -> None:
        if not live_path:
            return
        ordinary_preview = current_eligible_general()
        preview = _unique_by_ip([*ordinary_preview, *jp_selected[:source_target]])
        _write_handoff(
            live_path,
            preview,
            {
                "status": "running",
                "stage": "local-self-hosted-selection",
                "live_preview": True,
                "generated_at": datetime.now(UTC).isoformat(),
                "counts": {
                    "ordinary_qualified": len(ordinary_preview),
                    "ordinary_minimum": minimum_general,
                    "ordinary_maximum": general_target,
                    "jp_selected": len(jp_selected[:source_target]),
                },
                "local_rules": config.get("_local_rules", {}),
            },
        )

    def accept_live_speed_result(node: NodeResult) -> None:
        speed_qualified[node.ip] = node
        write_live_preview()

    # Show retained valid nodes and the JP lane immediately, then append each
    # newly speed-qualified ordinary node as soon as its download probe ends.
    write_live_preview()
    tcp_valid = asyncio.run(scan_tcp(general_candidates, pipeline["quality_tcp"]))
    metric_valid, metric_counts = _three_metric_checks(
        tcp_valid,
        domain=str(config["project"]["target_domain"]),
        pipeline=pipeline,
        user_agent=user_agent,
        locations=locations,
    )
    ordered_metrics = rank_final(metric_valid, count=len(metric_valid))
    while force_rerank or len(current_eligible_general()) < minimum_general:
        remaining = [node for node in ordered_metrics if node.ip not in speed_processed]
        if not remaining:
            break
        chunk = remaining[:speed_batch_size]
        qualified, counts = _speed_checks(
            chunk,
            pipeline=pipeline,
            user_agent=user_agent,
            probe_paths=probe_paths,
            on_qualified=accept_live_speed_result,
        )
        speed_processed.update(node.ip for node in chunk)
        for node in qualified:
            speed_qualified[node.ip] = node
        speed_batches.append({
            **counts,
            "input": len(chunk),
            "qualified_total": len(speed_qualified),
        })

    max_per_colo = int(handoff.get("max_per_colo", 50))
    eligible_general = current_eligible_general()
    current_general = _rank_with_colo_diversity(
        eligible_general,
        count=general_target,
        max_per_colo=max_per_colo,
        latency_speed_first=force_rerank,
    )
    current_general = [
        node
        for node in current_general
        if _final_country_allowed(node, pipeline=pipeline, source_country=source_country)
        and _final_ordinary_quality_allowed(
            node,
            pipeline=pipeline,
            source_country=source_country,
        )
    ]
    replacement_ips = {node.ip for node in current_general}
    previous_general = [
        node
        for node in published_previous
        if (node.country or node.country_hint).upper() != source_country
        and node.ip not in replacement_ips
        and _final_country_allowed(node, pipeline=pipeline, source_country=source_country)
    ]
    merged_general = _unique_by_ip([*current_general, *previous_general])[:general_target]
    # New qualified nodes replace the same number of entries from the tail of
    # the already-published ranking. JP remains an independent appended lane.
    selected = _unique_by_ip([*merged_general, *jp_selected[:source_target]])
    ordinary_replacements = len(current_general)
    ordinary_selected = len(merged_general)
    jp_selected_count = len(selected) - ordinary_selected
    publish_ready = (
        ordinary_replacements >= minimum_general
        and jp_selected_count >= source_target
    )
    if not publish_ready:
        warnings.append(
            f"本地复测只得到普通节点 {ordinary_replacements}/{minimum_general} 条、"
            f"日本节点 {jp_selected_count}/{source_target} 条，保留上一轮历史订阅"
        )
        selected = []

    attempted_ips.update(node.ip for node in new_candidates)
    qualified_accumulator = _unique_by_ip([
        *jp_selected,
        *eligible_general,
    ])

    previous_cycle_round = int(
        accumulator_payload.get("report", {}).get("cycle_round", 0)
        if isinstance(accumulator_payload.get("report"), dict)
        else 0
    )
    cycle_round = previous_cycle_round + 1
    max_cycle_rounds = int(handoff.get("max_replenishment_rounds", 30))
    needs_more = not publish_ready and cycle_round < max_cycle_rounds
    if not publish_ready and not needs_more:
        warnings.append(
            f"已达到最多 {max_cycle_rounds} 轮本地补池限制，继续保留历史订阅"
        )
    report = {
        "status": "ok" if publish_ready else "degraded",
        "stage": "local-self-hosted-selection",
        "started_at": started.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "vantage": "windows-self-hosted-local-network",
        "handoff_generated_at": cloud_payload.get("generated_at"),
        "cycle_round": cycle_round,
        "max_replenishment_rounds": max_cycle_rounds,
        "counts": {
            "cloud_pool": len(cloud_nodes),
            "previous_loaded": len(previous),
            "combined_unique": len(combined),
            "new_unique_tested": len(new_candidates),
            "accumulated_loaded": len(accumulated),
            "jp_candidates": len(jp_candidates),
            "jp_selected": len(jp_selected),
            "general_candidates": len(general_candidates),
            "tcp_qualified": len(tcp_valid),
            "metric_qualified": len(metric_valid),
            "speed_tested": len(speed_processed),
            "speed_qualified": len(speed_qualified),
            "ordinary_current_rules_qualified": len(eligible_general),
            "qualified_accumulated": len(qualified_accumulator),
            "fixed_source_ips_retained": len(retained_source_ips or ()),
            "general_target": general_target,
            "general_minimum": minimum_general,
            "ordinary_selected": ordinary_selected,
            "ordinary_replacements": ordinary_replacements,
            "previous_ordinary_retained": max(0, len(merged_general) - ordinary_replacements),
            "final_target": publish_target,
            "minimum_publish_target": minimum_publish_target,
            "final_selected": len(selected),
        },
        "jp_measurements": jp_counts,
        "metric_measurements": metric_counts,
        "speed_batches": speed_batches,
        "warnings": warnings,
        "needs_more": needs_more,
        "forced_rerank": force_rerank,
        "local_rules": config.get("_local_rules", {}),
    }
    if not publish_ready:
        if accumulator_path:
            _write_handoff(accumulator_path, qualified_accumulator, report)
        if attempted_path:
            _write_ip_set(
                attempted_path,
                retained_source_ips if retained_source_ips is not None else attempted_ips,
            )
    output_dir = resolve_path(config, config["paths"]["output"])
    final_report = publish_outputs(output_dir, selected, report, output_options)
    if final_report["published"]:
        rolling = config.get("rolling", {})
        snapshot_value = rolling.get("snapshot_path")
        if snapshot_value:
            save_previous_top(
                resolve_path(config, snapshot_value),
                selected[: int(rolling.get("previous_limit", 100))],
            )
        if accumulator_path:
            accumulator_path.unlink(missing_ok=True)
        if attempted_path:
            attempted_path.unlink(missing_ok=True)
        if force_rerank:
            force_marker_path.unlink(missing_ok=True)
    print(
        f"本地完成: 输入={len(combined)} 最终={len(selected)} "
        f"最低发布={minimum_publish_target} 最大={publish_target} "
        f"published={final_report['published']}",
        flush=True,
    )
    return final_report
