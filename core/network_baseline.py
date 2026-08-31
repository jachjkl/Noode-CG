from __future__ import annotations

import statistics
import time
import urllib.request
from typing import Any


def measure_network_baseline(options: dict[str, Any], *, user_agent: str) -> dict[str, Any]:
    attempts = max(1, int(options.get("attempts", 2)))
    timeout = float(options.get("timeout_seconds", 10))
    results: dict[str, Any] = {}
    successful_medians: list[float] = []
    for target in options.get("targets", []):
        name = str(target.get("name") or target.get("url") or "target")
        url = str(target.get("url") or "")
        measurements: list[float] = []
        errors: list[str] = []
        for _ in range(attempts):
            started = time.perf_counter()
            try:
                request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "*/*"})
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    response.read(1024)
                measurements.append((time.perf_counter() - started) * 1000)
            except Exception as exc:
                errors.append(str(exc)[:160])
        median = round(statistics.median(measurements), 3) if measurements else None
        if median is not None:
            successful_medians.append(median)
        results[name] = {
            "median_ms": median,
            "successes": len(measurements),
            "attempts": attempts,
            "errors": errors,
        }
    results["average_ms"] = round(statistics.fmean(successful_medians), 3) if successful_medians else None
    return results
