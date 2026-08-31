from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.models import NodeResult
from core.rolling import load_previous_top, prepare_retest_candidates


def node(ip: str, score: float, country: str = "US") -> NodeResult:
    value = NodeResult(ip=ip, country=country, score=score)
    value.tcp_ok = value.tls_ok = value.http_ok = True
    return value


class RollingSelectionTests(unittest.TestCase):
    def test_loads_and_limits_previous_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            payload = [
                {"ip": f"104.16.{index}.1", "port": 443, "country": "US", "score": 1000 - index}
                for index in range(120)
            ]
            (output / "nodes.json").write_text(json.dumps(payload), encoding="utf-8")

            previous, warnings = load_previous_top(output, None, 100)

            self.assertEqual(len(previous), 100)
            self.assertEqual(previous[0].ip, "104.16.0.1")
            self.assertEqual(warnings, [])

    def test_prepare_retest_candidates_deduplicates_current_and_previous(self) -> None:
        current = [node("104.16.1.1", 900), node("104.17.1.1", 800)]
        previous = [node("104.16.1.1", 999), node("1.0.0.1", 998, "JP")]

        combined = prepare_retest_candidates(current, previous)

        self.assertEqual([item.ip for item in combined], ["104.16.1.1", "104.17.1.1", "1.0.0.1"])
        self.assertTrue(all(item.tcp_latency_ms is None for item in combined))
        self.assertIn("previous-top100", combined[-1].sources)


if __name__ == "__main__":
    unittest.main()
