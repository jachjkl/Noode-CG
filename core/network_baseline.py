from __future__ import annotations

import statistics
import time
import urllib.request
from typing import Any


def measure_network_baseline(options: dict[str, Any], *, user_agent: str) -> dict[str, Any]:
    attempts = max(1, int(options.get("attempts", 2)))
    timeout = float(options.get("timeout_seconds", 10))
    results: dict[str, Any] = {}
    successful_averages: list[float] = []
    all_targets_passed = True
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
        average = round(statistics.fmean(measurements), 3) if measurements else None
        passed = len(measurements) == attempts
        all_targets_passed = all_targets_passed and passed
        if average is not None:
            successful_averages.append(average)
        results[name] = {
            "average_ms": average,
            "successes": len(measurements),
            "attempts": attempts,
            "all_attempts_passed": passed,
            "errors": errors,
        }
    results["average_ms"] = (
        round(statistics.fmean(successful_averages), 3) if successful_averages else None
    )
    results["all_targets_passed"] = all_targets_passed
    return results
