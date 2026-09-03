from __future__ import annotations

import asyncio
import gzip
import json
import os
import threading
import time
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
from .ranking import rank_final
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
    candidates = [resolve_path(config, config["paths"]["output"]) / "nodes.json"]
    local_root = os.environ.get("NOODE_LOCAL_ROOT", "").strip()
    if local_root:
        candidates.append(Path(local_root) / "dashboard-cache" / "nodes.json")
    for path in candidates:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        nodes = _nodes_from_payload(payload)
        if nodes:
            return nodes
    return []


def _nodes_from_payload(value: Any) -> list[NodeResult]:
    if not isinstance(value, list):
        return []
    def restore(item: dict[str, Any]) -> NodeResult:
        normalized = dict(item)
        # Published nodes use UI-friendly public names while handoff nodes use
        # the internal model names. Preserve old ranking metrics during merge.
        if "tcp_loss_rate" not in normalized and "loss_rate" in normalized:
            normalized["tcp_loss_rate"] = normalized["loss_rate"]
        if "overall_jitter_ms" not in normalized and "jitter_ms" in normalized:
            normalized["overall_jitter_ms"] = normalized["jitter_ms"]
        return NodeResult.from_dict(normalized)
    return _unique_by_ip(
        restore(item)
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


class LiveTestRecorder:
    """Persist a compact, best-effort stream of the local test decisions.

    The selection pipeline must remain independent from the dashboard.  The
    recorder therefore serializes writes, throttles them, and swallows file
    errors.  A partially unavailable UI file can never make a network test
    fail or change which nodes are published.
    """

    WRITE_INTERVAL_SECONDS = 0.25

    def __init__(self, path: Path, *, load_existing: bool = False) -> None:
        self.path = Path(path)
        self.lock = threading.RLock()
        self.records: dict[str, dict[str, Any]] = {}
        self.stage = ""
        self.status = "running"
        self.started_at = datetime.now(UTC).isoformat()
        self.last_write = 0.0
        self.write_error = ""
        if load_existing:
            self._load_existing()

    def _load_existing(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(gzip.decompress(self.path.read_bytes()).decode("utf-8"))
        except (OSError, gzip.BadGzipFile, UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        report = payload.get("report")
        if isinstance(report, dict):
            self.started_at = str(report.get("started_at") or self.started_at)
        tests = payload.get("tests")
        if isinstance(tests, list):
            for item in tests:
                if not isinstance(item, dict) or not item.get("key"):
                    continue
                self.records[str(item["key"])] = dict(item)

    @staticmethod
    def _snapshot(node: NodeResult, lane: str) -> dict[str, Any]:
        country = str(node.country or node.country_hint or "").upper()
        display = f"[{node.ip}]" if ":" in node.ip else node.ip
        return {
            "key": node.key,
            "ip": node.ip,
            "port": node.port,
            "ip_port": f"{display}:{node.port}",
            "lane": lane,
            "country": country,
            "country_hint": str(node.country_hint or "").upper(),
            "colo_country": str(node.colo_country or "").upper(),
            "region": node.region or "",
            "city": node.city or "",
            "colo": node.colo or "",
            "tcp_latency_ms": node.tcp_latency_ms,
            "tcp_jitter_ms": node.tcp_jitter_ms,
            "tcp_loss_rate": node.tcp_loss_rate,
            "tls_latency_ms": node.tls_latency_ms,
            "tls_jitter_ms": node.tls_jitter_ms,
            "http_latency_ms": node.http_latency_ms,
            "http_jitter_ms": node.http_jitter_ms,
            "average_latency_ms": node.average_latency_ms,
            "overall_jitter_ms": node.overall_jitter_ms,
            "speed_mbps": node.speed_mbps,
            "score": node.score,
        }

    @staticmethod
    def _reason(node: NodeResult, fallback: str = "") -> str:
        if node.errors:
            return str(node.errors[-1])[:240]
        return fallback

    def _report_locked(self) -> dict[str, Any]:
        values = list(self.records.values())
        counts = {
            "total": len(values),
            "queued": sum(item.get("status") == "queued" for item in values),
            "testing": sum(item.get("status") == "testing" for item in values),
            "passed": sum(item.get("status") == "passed" for item in values),
            "eliminated": sum(item.get("status") == "eliminated" for item in values),
            "retained": sum(item.get("status") == "retained" for item in values),
        }
        counts["processed"] = counts["passed"] + counts["eliminated"] + counts["retained"]
        report: dict[str, Any] = {
            "status": self.status,
            "stage": self.stage,
            "started_at": self.started_at,
            "generated_at": datetime.now(UTC).isoformat(),
            **counts,
        }
        if self.write_error:
            report["write_error"] = self.write_error
        return report

    def _write_locked(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last_write < self.WRITE_INTERVAL_SECONDS:
            return
        payload = {
            "schema": 1,
            "report": self._report_locked(),
            "tests": list(self.records.values()),
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            atomic_write_bytes(self.path, gzip.compress(encoded, compresslevel=6, mtime=0))
            self.last_write = now
            self.write_error = ""
        except OSError as exc:
            self.write_error = str(exc)[:240]

    def seed(self, nodes: Iterable[NodeResult], lane: str) -> None:
        with self.lock:
            for node in nodes:
                key = node.key
                if key in self.records:
                    continue
                record = self._snapshot(node, lane)
                record.update({
                    "status": "queued",
                    "stage": "等待测试",
                    "reason": "",
                    "updated_at": datetime.now(UTC).isoformat(),
                })
                self.records[key] = record
            self._write_locked(force=True)

    def mark_eliminated(self, node: NodeResult, stage: str, reason: str) -> None:
        self.update(node, stage, "eliminated", reason)

    def start_stage(self, nodes: Iterable[NodeResult], stage: str) -> None:
        with self.lock:
            self.stage = stage
            for node in nodes:
                record = self.records.get(node.key)
                if record is None:
                    continue
                record.update(self._snapshot(node, str(record.get("lane") or "ordinary")))
                record.update({
                    "status": "testing",
                    "stage": stage,
                    "reason": "",
                    "updated_at": datetime.now(UTC).isoformat(),
                })
            self._write_locked(force=True)

    def update(self, node: NodeResult, stage: str, status: str, reason: str = "") -> None:
        with self.lock:
            record = self.records.get(node.key)
            if record is None:
                record = self._snapshot(node, "ordinary")
                self.records[node.key] = record
            else:
                record.update(self._snapshot(node, str(record.get("lane") or "ordinary")))
            record.update({
                "status": status,
                "stage": stage,
                "reason": (reason or self._reason(node))[:240],
                "updated_at": datetime.now(UTC).isoformat(),
            })
            self.stage = stage
            self._write_locked()

    def finish(self, status: str = "completed", **extra: Any) -> None:
        with self.lock:
            self.status = status
            self._write_locked(force=True)
            if extra:
                try:
                    payload = json.loads(gzip.decompress(self.path.read_bytes()).decode("utf-8"))
                    if isinstance(payload, dict) and isinstance(payload.get("report"), dict):
                        payload["report"].update(extra)
                        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                        atomic_write_bytes(self.path, gzip.compress(encoded, compresslevel=6, mtime=0))
                except (OSError, gzip.BadGzipFile, UnicodeDecodeError, json.JSONDecodeError):
                    pass

    def synchronize_results(
        self,
        nodes: Iterable[NodeResult],
        *,
        exempt_country: str,
    ) -> None:
        """Make pass counters match the live qualified-results panel."""
        selected = {node.key: node for node in nodes}
        exempt = exempt_country.upper()
        with self.lock:
            for key, node in selected.items():
                country = str(node.country or node.country_hint or "").upper()
                lane = "jp" if country == exempt else "ordinary"
                record = self.records.get(key) or self._snapshot(node, lane)
                record.update(self._snapshot(node, lane))
                record.update({
                    "status": "retained" if lane == "jp" else "passed",
                    "stage": "日本节点豁免保留" if lane == "jp" else "本轮实时优选合格",
                    "reason": "JP 豁免普通节点门槛" if lane == "jp" else "",
                    "updated_at": datetime.now(UTC).isoformat(),
                })
                self.records[key] = record
            self._write_locked(force=True)


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
    """Cloud stage: hand raw candidates to the Windows runner without probing.

    A fresh cycle contains every unique endpoint from the configured links plus
    one official Cloudflare batch. Replenishment cycles contain only a new,
    disjoint official batch; all quality gates are deliberately local.
    """
    started = datetime.now(UTC)
    handoff = config["handoff"]
    target = int(handoff.get("target", 10000))
    pool_path = resolve_path(config, handoff["pool_path"])
    health_path = resolve_path(config, handoff["health_path"])
    previous, warnings = _load_previous(config)
    previous_ips = {node.ip for node in previous}
    attempted_value = handoff.get("attempted_path")
    attempted_path = resolve_path(config, attempted_value) if attempted_value else None
    loaded_attempted_ips = _load_ip_set(attempted_path) if attempted_path else set()
    accumulator_value = handoff.get("accumulator_path")
    accumulator_path = (
        resolve_path(config, accumulator_value) if accumulator_value else None
    )
    official_value = config.get("rolling", {}).get("official_snapshot_path")
    official_path = resolve_path(config, official_value) if official_value else None
    loaded_prior_official_ips = _load_ip_set(official_path) if official_path else set()
    continuation = (
        os.getenv("NOODE_CONTINUATION", "").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    publish_only = (
        os.getenv("NOODE_PUBLISH_ONLY", "").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    published_nodes = _load_published_nodes(config)
    # Attempted addresses are session-local, while the official snapshot is
    # deliberately carried across dashboard sessions so the next software
    # launch receives a different official batch.
    attempted_ips = loaded_attempted_ips if continuation else set()
    prior_official_ips = loaded_prior_official_ips
    if not continuation:
        # A manual Start begins a genuinely new dashboard session. Do not let
        # unfinished state committed by an older run leak into its live panel.
        if accumulator_path:
            accumulator_path.unlink(missing_ok=True)
        if attempted_path:
            attempted_path.unlink(missing_ok=True)
    sources: list[NodeResult] = []
    if not continuation and not publish_only:
        sources, source_warnings = collect_source_candidates(config)
        warnings.extend(source_warnings)
    source_ips = {node.ip for node in sources}
    official: list[NodeResult] = []
    if not publish_only:
        official, official_warnings = collect_official_batch(
            config,
            exclude_ips=(
                previous_ips | source_ips | attempted_ips | prior_official_ips
            ),
            round_index=0,
        )
        warnings.extend(official_warnings)
        official = _unique_by_ip(official)[:target]
    official_ips = {node.ip for node in official}
    selected = _unique_by_ip([*sources, *official])
    status = "ok" if publish_only or len(official) >= target else "degraded"
    print(
        f"云端候选生成完成: 官方唯一IP={len(official)}/{target} "
        f"链接全量IP={len(sources)} 交接合计={len(selected)}；未进行网络初筛",
        flush=True,
    )
    report = {
        "status": status,
        "stage": "cloud-prepare",
        "started_at": started.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "target": target,
        "selected": len(selected),
        "official_target": target,
        "previous_excluded": len(previous_ips),
        "attempted_excluded": len(attempted_ips),
        "prior_official_excluded": len(prior_official_ips),
        "continuation": continuation,
        "publish_only": publish_only,
        "stale_attempted_ignored": (
            len(loaded_attempted_ips) if not continuation else 0
        ),
        "stale_official_ignored": 0,
        "source_candidates": len(sources),
        "links_included": not continuation and not publish_only,
        "cloud_prefilter_skipped": True,
        "official_sampled": len(official_ips),
        "tested_unique": 0,
        "rounds": [{
            "round": 1,
            "official_unique": len(official),
            "link_unique": len(sources),
            "handoff_total": len(selected),
        }],
        "warnings": warnings,
    }
    if status == "ok":
        accumulated: list[NodeResult] = []
        if continuation and accumulator_path and accumulator_path.is_file():
            accumulated, _ = load_cloud_handoff(accumulator_path)
        _write_handoff(
            pool_path,
            selected,
            report,
            state={
                "previous_top100": [node.to_dict() for node in previous],
                "published_nodes": [node.to_dict() for node in published_nodes],
                "accumulated": [node.to_dict() for node in accumulated],
                "attempted_ips": sorted(attempted_ips),
            },
        )
    else:
        report["warnings"].append("官方候选不足 10000 个，保留上一版云端交接文件")
    atomic_write_json(health_path, report)
    if official_path and not publish_only:
        snapshot = prior_official_ips | official_ips if continuation else official_ips
        _write_ip_set(official_path, snapshot)
    return report


def run_local_selection(config: dict[str, Any]) -> dict[str, Any]:
    """Windows self-hosted stage: test raw cloud candidates + previous TOP100."""
    started = datetime.now(UTC)
    handoff_path = resolve_path(config, config["handoff"]["pool_path"])
    force_marker_path = handoff_path.parent / "force-rerank.json"
    stop_marker_path = handoff_path.parent / "stop-after-current.json"
    force_rerank = force_marker_path.is_file()
    cloud_nodes, cloud_payload = load_cloud_handoff(handoff_path)
    cloud_report = cloud_payload.get("report", {})
    continuation = bool(
        cloud_report.get("continuation", False)
        if isinstance(cloud_report, dict)
        else False
    )
    local_previous, warnings = _load_previous(config)
    embedded_state = cloud_payload.get("state", {})
    if not isinstance(embedded_state, dict):
        embedded_state = {}
    embedded_published = _nodes_from_payload(embedded_state.get("published_nodes"))
    published_previous = _unique_by_ip([
        *embedded_published,
        *_load_published_nodes(config),
    ])
    embedded_previous = _nodes_from_payload(embedded_state.get("previous_top100"))
    previous_limit = int(config.get("rolling", {}).get("previous_limit", 100))
    previous = _unique_by_ip([*local_previous, *embedded_previous])[:previous_limit]
    handoff = config["handoff"]
    accumulator_value = handoff.get("accumulator_path")
    accumulator_path = resolve_path(config, accumulator_value) if accumulator_value else None
    live_value = handoff.get("live_results_path")
    live_path = resolve_path(config, live_value) if live_value else None
    session_live_nodes: list[NodeResult] = []
    if live_path and live_path.is_file() and continuation:
        session_live_nodes, _ = load_cloud_handoff(live_path)
    elif live_path and not continuation:
        live_path.unlink(missing_ok=True)
    live_tests_value = handoff.get("live_tests_path")
    live_tests_path = resolve_path(config, live_tests_value) if live_tests_value else None
    if live_tests_path and not continuation:
        live_tests_path.unlink(missing_ok=True)
    live_tests = (
        LiveTestRecorder(live_tests_path, load_existing=continuation)
        if live_tests_path
        else None
    )
    attempted_value = handoff.get("attempted_path")
    attempted_path = resolve_path(config, attempted_value) if attempted_value else None
    accumulated: list[NodeResult] = []
    accumulator_payload: dict[str, Any] = {}
    attempted_ips: set[str] = set()
    if continuation:
        if accumulator_path and accumulator_path.is_file():
            accumulated, accumulator_payload = load_cloud_handoff(accumulator_path)
        embedded_accumulated = _nodes_from_payload(embedded_state.get("accumulated"))
        accumulated = _unique_by_ip([*accumulated, *embedded_accumulated])
        if attempted_path:
            attempted_ips = _load_ip_set(attempted_path)
        embedded_attempted = embedded_state.get("attempted_ips")
        if isinstance(embedded_attempted, list):
            attempted_ips.update(
                str(value).strip()
                for value in embedded_attempted
                if isinstance(value, str) and value.strip()
            )
    else:
        # The recovery step may restore state from a failed older session before
        # the fresh cloud handoff is downloaded.  A new local cycle must never
        # inherit that accumulator or its (potentially huge) attempted-IP set.
        if accumulator_path:
            accumulator_path.unlink(missing_ok=True)
        if attempted_path:
            attempted_path.unlink(missing_ok=True)
    # Fixed links are fetched once by the cloud at the beginning of a local
    # session. Replenishment must not download them again, so the local stage
    # only consumes the handoff and records every address it attempted.
    accumulated_loaded_count = len(accumulated)
    published_ips = {node.ip for node in published_previous}
    exempt_country = str(
        config.get("pipeline", {}).get("jp_source_requirement", {}).get("country", "JP")
    ).upper()
    retained_jp = _unique_by_ip([
        node for node in [*published_previous, *accumulated]
        if (node.country_hint or node.country).upper() == exempt_country
    ])
    # Every cloud-published and locally accumulated node competes again under
    # the current ordinary-node rules before each push. JP remains in its
    # explicitly exempt lane and is not duplicated on continuation rounds.
    retained_retests = _unique_by_ip([
        node for node in [*published_previous, *accumulated]
        if (node.country_hint or node.country).upper() != exempt_country
    ])
    retained_retest_ips = {node.ip for node in retained_retests}
    accumulated = retained_jp
    incoming = _unique_by_ip([
        *_fresh(cloud_nodes, "cloud-handoff"),
        *_fresh(previous, "previous-top100"),
    ])
    accumulated_ips = {node.ip for node in accumulated}
    ordinary_new_candidates = [
        node for node in incoming
        if node.ip not in attempted_ips
        and node.ip not in accumulated_ips
        and node.ip not in retained_retest_ips
    ]
    active_retests = _fresh(retained_retests, "published-and-qualified-retest")
    new_candidates = _unique_by_ip([*active_retests, *ordinary_new_candidates])
    combined = _unique_by_ip([*accumulated, *new_candidates])

    pipeline = config["pipeline"]
    output_options = config["output"]
    total_target = int(output_options["top_nodes"])
    minimum_general = min(total_target, int(output_options.get("minimum_publish", 1)))
    # Automatic multi-round chaining was removed. A new cloud batch is only
    # requested by the explicit Continue button, which avoids overlapping
    # high-cost local probes and keeps each round inspectable.
    continuous_three_rounds = False
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

    def latest_reason(node: NodeResult, fallback: str) -> str:
        return str(node.errors[-1])[:240] if node.errors else fallback

    def jp_tcp_result(node: NodeResult) -> None:
        if live_tests is None:
            return
        live_tests.update(
            node,
            "TCPing",
            "retained",
            "" if node.tcp_latency_ms is not None else latest_reason(node, "TCPing 未测得延迟；JP 继续参与排名"),
        )

    def jp_speed_result(node: NodeResult) -> None:
        if live_tests is None:
            return
        if node.speed_mbps is not None:
            live_tests.update(node, "下载测速", "retained", "JP 豁免普通节点规则")
        else:
            live_tests.update(
                node,
                "下载测速",
                "retained",
                latest_reason(node, "未测得下载速度；JP 豁免普通节点规则"),
            )

    def jp_stage(stage_records: list[NodeResult], stage: str) -> None:
        if live_tests is not None:
            live_tests.start_stage(stage_records, stage)

    if live_tests is not None:
        # Seed both lanes before the JP-only probe starts.  Otherwise a long
        # JP download phase makes the dashboard appear to contain only JP and
        # hides the ordinary candidates that are already waiting to be tested.
        live_tests.seed(jp_candidates, "jp")
        live_tests.seed(
            [
                node for node in new_candidates
                if (node.country_hint or node.country).upper() != source_country
            ],
            "ordinary",
        )
    jp_new_selected, jp_counts = _source_country_tcp_speed_checks(
        jp_candidates,
        pipeline=pipeline,
        rule=source_rule,
        user_agent=user_agent,
        probe_paths=probe_paths,
        count=source_target,
        on_tcp_result=jp_tcp_result if live_tests is not None else None,
        on_speed_result=jp_speed_result if live_tests is not None else None,
        on_stage=jp_stage if live_tests is not None else None,
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
    if live_tests is not None:
        ordinary_candidates = [
            node for node in new_candidates
            if (node.country_hint or node.country).upper() != source_country
        ]
        live_tests.seed(ordinary_candidates, "ordinary")
        included_keys = {node.key for node in general_candidates}
        for node in ordinary_candidates:
            if node.key not in included_keys:
                country = (node.country_hint or node.country or "unknown").upper()
                live_tests.mark_eliminated(
                    node,
                    "地区筛选",
                    f"命中排除地区：{country}",
                )
    speed_batch_size = int(pipeline.get("speed_batch_size", 400))
    max_per_colo = int(handoff.get("max_per_colo", 50))
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

    last_live_write = 0.0

    def current_live_preview() -> list[NodeResult]:
        ordinary_preview = _rank_with_colo_diversity(
            current_eligible_general(),
            count=general_target,
            max_per_colo=max_per_colo,
            latency_speed_first=force_rerank,
        )
        preview_by_ip = {node.ip: node for node in session_live_nodes}
        for node in [*ordinary_preview, *jp_selected[:source_target]]:
            preview_by_ip[node.ip] = node
        return list(preview_by_ip.values())

    def write_live_preview(force: bool = False) -> None:
        nonlocal last_live_write
        if not live_path:
            return
        now = time.monotonic()
        if not force and now - last_live_write < 0.25:
            return
        preview = current_live_preview()
        ordinary_count = sum(
            (node.country_hint or node.country).upper() != source_country
            for node in preview
        )
        jp_count = len(preview) - ordinary_count
        _write_handoff(
            live_path,
            preview,
            {
                "status": "running",
                "stage": "local-self-hosted-selection",
                "live_preview": True,
                "generated_at": datetime.now(UTC).isoformat(),
                "counts": {
                    "ordinary_qualified": ordinary_count,
                    "ordinary_minimum": minimum_general,
                    "ordinary_maximum": general_target,
                    "jp_selected": jp_count,
                },
                "local_rules": config.get("_local_rules", {}),
            },
        )
        last_live_write = now

    def accept_live_speed_result(node: NodeResult) -> None:
        speed_qualified[node.ip] = node
        write_live_preview()

    def ordinary_tcp_result(node: NodeResult) -> None:
        if live_tests is None:
            return
        live_tests.update(
            node,
            "TCPing",
            "testing" if node.tcp_ok else "eliminated",
            "" if node.tcp_ok else latest_reason(node, "TCPing 未通过"),
        )

    def ordinary_metric_result(node: NodeResult, stage: str, passed: bool) -> None:
        if live_tests is None:
            return
        live_tests.update(
            node,
            stage,
            "testing" if passed else "eliminated",
            "" if passed else latest_reason(node, f"{stage} 未通过"),
        )

    def ordinary_stage(stage_records: list[NodeResult], stage: str) -> None:
        if live_tests is not None:
            live_tests.start_stage(stage_records, stage)

    def ordinary_speed_result(node: NodeResult) -> None:
        if live_tests is None:
            return
        minimum_mbps = float(pipeline["speed"].get("minimum_mbps", 0))
        passed = node.speed_mbps is not None and node.speed_mbps >= minimum_mbps
        live_tests.update(
            node,
            "下载测速",
            "passed" if passed else "eliminated",
            "" if passed else latest_reason(node, f"下载速度低于 {minimum_mbps:g} Mbps"),
        )

    # Show retained valid nodes and the JP lane immediately. Ordinary nodes are
    # tested end-to-end in bounded batches. Every socket/probe owned by a batch
    # is closed by its stage before the next group starts.
    write_live_preview(force=True)
    metric_kwargs: dict[str, Any] = {}
    if live_tests is not None:
        metric_kwargs["on_result"] = ordinary_metric_result
        metric_kwargs["on_stage"] = ordinary_stage
    probe_batch_size = max(1, int(pipeline.get("local_probe_batch_size", 100)))
    tcp_enabled = bool(pipeline.get("quality_tcp", {}).get("enabled", True))
    tcp_valid: list[NodeResult] = []
    metric_valid: list[NodeResult] = []
    metric_counts: dict[str, Any] = {
        "enabled_metrics": [],
        "tls_three_pass_success": 0 if pipeline.get("tls", {}).get("enabled", True) else None,
        "https_ttfb_three_pass_success": 0 if pipeline.get("http", {}).get("enabled", True) else None,
        "foreign_combined_latency_qualified": 0,
        "tls_and_https_duration_seconds": 0.0,
        "probe_batch_size": probe_batch_size,
        "probe_batches": 0,
    }
    total_batches = (len(general_candidates) + probe_batch_size - 1) // probe_batch_size
    progress_every = max(1, total_batches // 20)
    scan_stopped_at_batch: int | None = None
    ordinary_probe_input_tested = 0
    for offset in range(0, len(general_candidates), probe_batch_size):
        chunk = general_candidates[offset : offset + probe_batch_size]
        ordinary_probe_input_tested += len(chunk)
        batch_number = offset // probe_batch_size + 1
        if tcp_enabled:
            if live_tests is not None:
                live_tests.start_stage(chunk, "TCPing")
                batch_tcp = asyncio.run(scan_tcp(
                    chunk,
                    pipeline["quality_tcp"],
                    on_result=ordinary_tcp_result,
                ))
            else:
                batch_tcp = asyncio.run(scan_tcp(chunk, pipeline["quality_tcp"]))
        else:
            batch_tcp = list(chunk)
        tcp_valid.extend(batch_tcp)
        batch_metrics, batch_counts = _three_metric_checks(
            batch_tcp,
            domain=str(config["project"]["target_domain"]),
            pipeline=pipeline,
            user_agent=user_agent,
            locations=locations,
            **metric_kwargs,
        )
        metric_valid.extend(batch_metrics)
        metric_counts["enabled_metrics"] = batch_counts.get("enabled_metrics", [])
        for name in (
            "tls_three_pass_success",
            "https_ttfb_three_pass_success",
            "foreign_combined_latency_qualified",
            "tls_and_https_duration_seconds",
        ):
            value = batch_counts.get(name)
            if value is not None and metric_counts.get(name) is not None:
                metric_counts[name] += value
        metric_counts["probe_batches"] = batch_number

        for speed_offset in range(0, len(batch_metrics), max(1, min(speed_batch_size, probe_batch_size))):
            speed_chunk = rank_final(
                batch_metrics[speed_offset : speed_offset + max(1, min(speed_batch_size, probe_batch_size))],
                count=max(1, min(speed_batch_size, probe_batch_size)),
            )
            if not speed_chunk:
                continue
            if live_tests is not None:
                live_tests.start_stage(speed_chunk, "下载测速")
            speed_kwargs: dict[str, Any] = {}
            if live_tests is not None:
                speed_kwargs["on_result"] = ordinary_speed_result
            qualified, counts = _speed_checks(
                speed_chunk,
                pipeline=pipeline,
                user_agent=user_agent,
                probe_paths=probe_paths,
                on_qualified=accept_live_speed_result,
                **speed_kwargs,
            )
            speed_processed.update(node.ip for node in speed_chunk)
            for node in qualified:
                speed_qualified[node.ip] = node
            speed_batches.append({
                **counts,
                "input": len(speed_chunk),
                "qualified_total": len(speed_qualified),
                "probe_batch": batch_number,
            })
        write_live_preview(force=True)
        if batch_number % progress_every == 0 or batch_number == total_batches:
            print(
                f"[LOCAL-BATCH] {batch_number}/{total_batches} "
                f"input={len(chunk)} tcp={len(batch_tcp)} "
                f"metrics={len(batch_metrics)} qualified={len(current_eligible_general())}",
                flush=True,
            )
        # A stop request is cooperative: finish the current 100-IP group so
        # its sockets are closed and measured winners are retained, then move
        # directly to the mandatory pre-publish competition pass. Without a
        # stop request every cloud-pushed candidate is always tested.
        if stop_marker_path.is_file():
            scan_stopped_at_batch = batch_number
            break

    eligible_general = current_eligible_general()

    # Mandatory pre-publish competition. Re-measure every ordinary IP that
    # qualified locally together with all currently published ordinary IPs,
    # then apply the current local rules and keep the best 300. JP is kept in
    # its independent exempt lane and never enters these ordinary gates.
    published_ordinary = [
        node for node in published_previous
        if (node.country_hint or node.country).upper() != source_country
    ]
    competition_candidates = _fresh(
        _unique_by_ip([*eligible_general, *published_ordinary]),
        "pre-publish-competition-retest",
    )
    competition_qualified: dict[str, NodeResult] = {}
    competition_tcp_tested = 0
    competition_metric_tested = 0
    competition_speed_tested = 0
    competition_batches = 0

    def competition_tcp_result(node: NodeResult) -> None:
        if live_tests is not None:
            live_tests.update(
                node,
                "发布前竞赛复测 · TCPing",
                "testing" if node.tcp_ok else "eliminated",
                "" if node.tcp_ok else latest_reason(node, "TCPing 未通过"),
            )

    def competition_metric_result(node: NodeResult, stage: str, passed: bool) -> None:
        if live_tests is not None:
            live_tests.update(
                node,
                f"发布前竞赛复测 · {stage}",
                "testing" if passed else "eliminated",
                "" if passed else latest_reason(node, f"{stage} 未通过"),
            )

    def competition_stage(stage_records: list[NodeResult], stage: str) -> None:
        if live_tests is not None:
            live_tests.start_stage(stage_records, f"发布前竞赛复测 · {stage}")

    def competition_speed_result(node: NodeResult) -> None:
        if live_tests is None:
            return
        minimum_mbps = float(pipeline["speed"].get("minimum_mbps", 0))
        passed = node.speed_mbps is not None and node.speed_mbps >= minimum_mbps
        live_tests.update(
            node,
            "发布前竞赛复测 · 下载测速",
            "testing" if passed else "eliminated",
            "" if passed else latest_reason(node, f"下载速度低于 {minimum_mbps:g} Mbps"),
        )

    if live_tests is not None:
        live_tests.seed(competition_candidates, "ordinary")
    for offset in range(0, len(competition_candidates), probe_batch_size):
        chunk = competition_candidates[offset : offset + probe_batch_size]
        competition_batches += 1
        if tcp_enabled:
            if live_tests is not None:
                live_tests.start_stage(chunk, "发布前竞赛复测 · TCPing")
                batch_tcp = asyncio.run(scan_tcp(
                    chunk,
                    pipeline["quality_tcp"],
                    on_result=competition_tcp_result,
                ))
            else:
                batch_tcp = asyncio.run(scan_tcp(chunk, pipeline["quality_tcp"]))
        else:
            batch_tcp = list(chunk)
        competition_tcp_tested += len(chunk)
        competition_metric_kwargs: dict[str, Any] = {}
        if live_tests is not None:
            competition_metric_kwargs["on_result"] = competition_metric_result
            competition_metric_kwargs["on_stage"] = competition_stage
        batch_metrics, _batch_counts = _three_metric_checks(
            batch_tcp,
            domain=str(config["project"]["target_domain"]),
            pipeline=pipeline,
            user_agent=user_agent,
            locations=locations,
            **competition_metric_kwargs,
        )
        competition_metric_tested += len(batch_tcp)
        for speed_offset in range(0, len(batch_metrics), max(1, min(speed_batch_size, probe_batch_size))):
            speed_chunk = rank_final(
                batch_metrics[speed_offset : speed_offset + max(1, min(speed_batch_size, probe_batch_size))],
                count=max(1, min(speed_batch_size, probe_batch_size)),
            )
            if not speed_chunk:
                continue
            if live_tests is not None:
                live_tests.start_stage(speed_chunk, "发布前竞赛复测 · 下载测速")
            competition_speed_kwargs: dict[str, Any] = {}
            if live_tests is not None:
                competition_speed_kwargs["on_result"] = competition_speed_result
            qualified, _counts = _speed_checks(
                speed_chunk,
                pipeline=pipeline,
                user_agent=user_agent,
                probe_paths=probe_paths,
                **competition_speed_kwargs,
            )
            competition_speed_tested += len(speed_chunk)
            for node in qualified:
                competition_qualified[node.ip] = node
        print(
            f"[PUBLISH-RETEST] {competition_batches}/"
            f"{max(1, (len(competition_candidates) + probe_batch_size - 1) // probe_batch_size)} "
            f"input={len(chunk)} qualified={len(competition_qualified)}",
            flush=True,
        )

    competition_eligible = [
        node for node in competition_qualified.values()
        if _final_country_allowed(node, pipeline=pipeline, source_country=source_country)
        and _final_ordinary_quality_allowed(
            node,
            pipeline=pipeline,
            source_country=source_country,
        )
    ]
    if live_tests is not None:
        live_tests.start_stage([], "发布前竞赛复测完成 · 等待推送")
    # A full published ordinary pool is a last-good safety net. Every old node
    # is still re-tested above, but transient batch pressure must never turn a
    # healthy cloud TOP300 into a 17-node file. Fresh passing measurements win
    # by IP; compatible last-good measurements fill only missing slots. If the
    # current local rules make even that impossible, publishing is skipped and
    # the existing cloud output is preserved intact.
    published_pool_was_full = len(published_ordinary) >= general_target
    fallback_by_ip = {
        node.ip: node
        for node in published_ordinary
        if published_pool_was_full
        and _final_country_allowed(node, pipeline=pipeline, source_country=source_country)
        and _final_ordinary_quality_allowed(
            node,
            pipeline=pipeline,
            source_country=source_country,
        )
    }
    competition_by_ip = dict(fallback_by_ip)
    competition_by_ip.update({node.ip: node for node in competition_eligible})
    current_general = _rank_with_colo_diversity(
        competition_by_ip.values(),
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
    merged_general = current_general
    freshly_qualified_ips = {node.ip for node in competition_eligible}
    fallback_retained = sum(
        node.ip in fallback_by_ip and node.ip not in freshly_qualified_ips
        for node in merged_general
    )
    selected = _unique_by_ip([*merged_general, *jp_selected[:source_target]])
    ordinary_replacements = sum(
        1 for node in merged_general if node.ip not in published_ips
    )
    ordinary_selected = len(merged_general)
    jp_selected_count = len(selected) - ordinary_selected
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
    max_cycle_rounds = 1
    target_reached = ordinary_selected >= general_target
    stop_after_current = stop_marker_path.is_file()
    required_ordinary = general_target if published_pool_was_full else minimum_general
    has_minimum_ordinary = ordinary_selected >= required_ordinary
    publish_ready = (
        jp_selected_count >= source_target
        and has_minimum_ordinary
    )
    needs_more = (
        continuous_three_rounds
        and not target_reached
        and cycle_round < max_cycle_rounds
        and jp_selected_count >= source_target
        and not stop_after_current
        and not bool(cloud_report.get("publish_only", False))
    )
    if stop_after_current:
        warnings.append("已收到停止请求：已在当前100-IP批次结束后停止扫描，并完成发布前竞赛复测")
    if published_pool_was_full and fallback_retained:
        warnings.append(
            f"发布前复测出现瞬时失败；为防止云端TOP{general_target}缩水，"
            f"按当前规则保留 {fallback_retained} 条上次合格测量"
        )
    if not publish_ready:
        if needs_more:
            warnings.append(
                f"连续优选第 {cycle_round}/{max_cycle_rounds} 轮累计得到普通节点 "
                f"{ordinary_replacements}/{general_target} 条，继续申请新的10000个官方候选"
            )
        else:
            warnings.append(
                f"本地复测得到普通节点 {ordinary_replacements}/{general_target} 条、"
                f"日本节点 {jp_selected_count}/{source_target} 条，本轮没有可发布结果"
            )
        selected = []
    if not publish_ready and not needs_more:
        warnings.append(
            f"已达到最多 {max_cycle_rounds} 轮本地补池限制"
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
            "accumulated_loaded": accumulated_loaded_count,
            "jp_candidates": len(jp_candidates),
            "jp_selected": len(jp_selected),
            "general_candidates": len(general_candidates),
            "ordinary_probe_input_tested": ordinary_probe_input_tested,
            "tcp_qualified": len(tcp_valid),
            "metric_qualified": len(metric_valid),
            "speed_tested": len(speed_processed),
            "speed_qualified": len(speed_qualified),
            "ordinary_current_rules_qualified": len(eligible_general),
            "prepublish_competition_input": len(competition_candidates),
            "prepublish_competition_qualified": len(competition_eligible),
            "prepublish_fallback_retained": fallback_retained,
            "qualified_accumulated": len(qualified_accumulator),
            "fixed_source_ips_retained": 0,
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
        "publish_retest": {
            "status": "completed",
            "input": len(competition_candidates),
            "tcp_tested": competition_tcp_tested,
            "metric_tested": competition_metric_tested,
            "speed_tested": competition_speed_tested,
            "qualified": len(competition_eligible),
            "selected": len(merged_general),
            "batches": competition_batches,
        },
        "speed_batches": speed_batches,
        "warnings": warnings,
        "needs_more": needs_more,
        "stopped_after_current": stop_after_current,
        "scan_stopped_at_batch": scan_stopped_at_batch,
        "forced_rerank": force_rerank,
        "continuous_three_rounds": continuous_three_rounds,
        "local_rules": config.get("_local_rules", {}),
    }
    # Keep the current session's qualified and attempted state after every
    # round, including successful publishes. It is cleared only when the next
    # dashboard session starts.
    if accumulator_path:
        _write_handoff(accumulator_path, qualified_accumulator, report)
    if attempted_path:
        _write_ip_set(attempted_path, attempted_ips)
    output_dir = resolve_path(config, config["paths"]["output"])
    publish_options = dict(output_options)
    if publish_ready:
        publish_options["minimum_publish"] = 1
    final_report = publish_outputs(output_dir, selected, report, publish_options)
    write_live_preview(force=True)
    if live_tests is not None:
        live_tests.synchronize_results(
            current_live_preview(),
            exempt_country=source_country,
        )
        live_tests.finish(
            "completed" if final_report.get("published") else "degraded",
            selection_status=final_report.get("status", ""),
            published=bool(final_report.get("published")),
            qualified_for_publish=len(selected),
        )
    if final_report["published"]:
        rolling = config.get("rolling", {})
        snapshot_value = rolling.get("snapshot_path")
        if snapshot_value:
            save_previous_top(
                resolve_path(config, snapshot_value),
                selected[: int(rolling.get("previous_limit", 100))],
            )
        if force_rerank:
            force_marker_path.unlink(missing_ok=True)
    print(
        f"本地完成: 输入={len(combined)} 最终={len(selected)} "
        f"最低发布={minimum_publish_target} 最大={publish_target} "
        f"published={final_report['published']}",
        flush=True,
    )
    return final_report
