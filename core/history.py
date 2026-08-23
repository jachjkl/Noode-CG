from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .io_utils import atomic_write_json, read_json
from .models import NodeResult


def _metrics(samples: list[int], neutral_prior_runs: int) -> tuple[float, int, float]:
    runs = len(samples)
    success_rate = sum(samples) / runs if runs else 0.5
    consecutive = 0
    for value in reversed(samples):
        if not value:
            break
        consecutive += 1
    maturity = min(1.0, runs / max(1, neutral_prior_runs))
    history_score = 0.5 + (success_rate - 0.5) * maturity
    return round(success_rate, 4), consecutive, round(history_score, 4)


def enrich_history(path: str | Path, records: list[NodeResult], options: dict[str, Any]) -> None:
    raw = read_json(path, default={})
    nodes = raw.get("nodes", {}) if isinstance(raw, dict) else {}
    if not isinstance(nodes, dict):
        return
    neutral_prior_runs = max(1, int(options.get("neutral_prior_runs", 4)))
    for node in records:
        entry = nodes.get(node.key, {})
        if not isinstance(entry, dict):
            continue
        samples = [1 if value else 0 for value in entry.get("samples", []) if value in (0, 1, False, True)]
        success_rate, consecutive, score = _metrics(samples, neutral_prior_runs)
        node.history_runs = len(samples)
        node.history_success_rate = success_rate
        node.history_consecutive_successes = consecutive
        node.history_score = score


def update_history(
    path: str | Path,
    assessed: list[NodeResult],
    passed: list[NodeResult],
    options: dict[str, Any],
) -> dict[str, Any]:
    destination = Path(path)
    raw = read_json(destination, default={})
    state = raw if isinstance(raw, dict) else {}
    nodes = state.get("nodes")
    if not isinstance(nodes, dict):
        nodes = {}

    window_runs = max(1, int(options.get("window_runs", 28)))
    neutral_prior_runs = max(1, int(options.get("neutral_prior_runs", 4)))
    max_missed_runs = max(1, int(options.get("max_missed_runs", window_runs * 2)))
    assessed_by_key = {node.key: node for node in assessed}
    passed_by_key = {node.key: node for node in passed}
    now = datetime.now(UTC).isoformat()

    for key, value in list(nodes.items()):
        if not isinstance(value, dict):
            nodes.pop(key, None)
            continue
        if key not in assessed_by_key:
            value["missed_runs"] = int(value.get("missed_runs", 0)) + 1
            if value["missed_runs"] > max_missed_runs:
                nodes.pop(key, None)

    for key, node in assessed_by_key.items():
        entry = nodes.setdefault(key, {})
        samples = [1 if value else 0 for value in entry.get("samples", []) if value in (0, 1, False, True)]
        samples.append(1 if key in passed_by_key else 0)
        samples = samples[-window_runs:]
        success_rate, consecutive, score = _metrics(samples, neutral_prior_runs)
        entry.update(
            {
                "ip": node.ip,
                "port": node.port,
                "samples": samples,
                "success_rate": success_rate,
                "consecutive_successes": consecutive,
                "history_score": score,
                "missed_runs": 0,
                "last_assessed_at": now,
            }
        )

    for key, node in passed_by_key.items():
        entry = nodes.get(key, {})
        samples = entry.get("samples", [])
        success_rate, consecutive, score = _metrics(samples, neutral_prior_runs)
        node.history_runs = len(samples)
        node.history_success_rate = success_rate
        node.history_consecutive_successes = consecutive
        node.history_score = score

    result = {
        "version": 1,
        "updated_at": now,
        "window_runs": window_runs,
        "nodes": nodes,
    }
    atomic_write_json(destination, result)
    return {
        "tracked": len(nodes),
        "assessed": len(assessed_by_key),
        "passed": len(passed_by_key),
        "window_runs": window_runs,
    }
