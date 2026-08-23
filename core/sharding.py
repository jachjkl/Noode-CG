from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from typing import Any

from .models import NodeResult


def _stable_order(node: NodeResult) -> bytes:
    return hashlib.sha256(node.key.encode("utf-8")).digest()


def _current_shard(shards: int, window_hours: int) -> int:
    configured = os.getenv("NOODE_SHARD_INDEX")
    if configured is not None:
        return int(configured) % shards
    window = int(datetime.now(UTC).timestamp()) // (window_hours * 3600)
    return window % shards


def select_scan_batch(
    records: list[NodeResult],
    options: dict[str, Any],
    *,
    history_keys: set[str],
    shard_index: int | None = None,
) -> tuple[list[NodeResult], dict[str, Any]]:
    """Select one deterministic rotating scan batch from a larger logical pool."""
    limit = int(options.get("limit", len(records)))
    shards = max(1, int(options.get("shards", 1)))
    window_hours = max(1, int(options.get("window_hours", 6)))
    required_sources = {str(value) for value in options.get("required_sources", [])}
    index = _current_shard(shards, window_hours) if shard_index is None else int(shard_index) % shards

    mandatory: list[NodeResult] = []
    optional: list[NodeResult] = []
    for node in records:
        if node.key in history_keys or required_sources.intersection(node.sources):
            mandatory.append(node)
        else:
            optional.append(node)
    mandatory.sort(key=_stable_order)
    optional.sort(key=_stable_order)
    if len(mandatory) > limit:
        raise ValueError(f"每轮必测地址 {len(mandatory)} 条，超过 scan.limit={limit}")

    partitions = [optional[offset::shards] for offset in range(shards)]
    ordered = partitions[index]
    for distance in range(1, shards):
        ordered.extend(partitions[(index + distance) % shards])

    selected = mandatory + ordered[: max(0, limit - len(mandatory))]
    metadata = {
        "pool": len(records),
        "selected": len(selected),
        "mandatory": len(mandatory),
        "shard_index": index,
        "shard_count": shards,
        "limit": limit,
        "window_hours": window_hours,
    }
    return selected, metadata
