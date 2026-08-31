from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import NodeResult


def load_locations(path: str | Path) -> dict[str, dict[str, Any]]:
    source = Path(path)
    if not source.is_file():
        return {}
    with source.open("r", encoding="utf-8-sig") as handle:
        values = json.load(handle)
    return {str(item.get("iata", "")).upper(): item for item in values if isinstance(item, dict) and item.get("iata")}


def enrich_locations(records: list[NodeResult], locations: dict[str, dict[str, Any]]) -> list[NodeResult]:
    for node in records:
        location = locations.get(node.colo, {})
        # The trace `loc` describes the request origin (for Actions, usually the
        # GitHub runner). The candidate's useful exit location is its CF colo.
        node.country = str(location.get("cca2") or node.country_hint or "").upper()
        node.region = str(location.get("region") or "")
        node.city = str(location.get("city") or "")
    return records
