from __future__ import annotations

from typing import Any

from .models import NodeResult


def reserve_candidates(records: list[NodeResult], limit: int, options: dict[str, Any]) -> list[NodeResult]:
    """Reserve measured colos before filling an ordered precision-test batch."""
    minimum_per_colo = {
        str(key).upper(): max(0, int(value)) for key, value in options.get("minimum_per_colo", {}).items()
    }
    max_official = max(0, int(options.get("max_official_generated", limit)))
    max_per_colo = max(1, int(options.get("max_per_colo", limit)))
    selected: list[NodeResult] = []
    selected_keys: set[str] = set()
    official_count = 0
    colo_counts: dict[str, int] = {}

    def add(node: NodeResult) -> bool:
        nonlocal official_count
        if node.key in selected_keys or len(selected) >= limit:
            return False
        if node.official_only and official_count >= max_official:
            return False
        colo = node.colo or "Unknown"
        if colo_counts.get(colo, 0) >= max_per_colo:
            return False
        selected.append(node)
        selected_keys.add(node.key)
        colo_counts[colo] = colo_counts.get(colo, 0) + 1
        if node.official_only:
            official_count += 1
        return True

    for colo, minimum in minimum_per_colo.items():
        added = 0
        for node in records:
            if node.colo == colo and add(node):
                added += 1
                if added >= minimum:
                    break

    for node in records:
        add(node)
        if len(selected) >= limit:
            break
    return selected
