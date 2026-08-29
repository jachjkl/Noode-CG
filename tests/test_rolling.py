from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.models import NodeResult
from core.rolling import load_previous_top, merge_with_previous


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

    def test_second_selection_uses_only_revalidated_previous_nodes(self) -> None:
        current = [node("104.16.1.1", 900), node("104.17.1.1", 800)]
        revalidated_previous = node("1.1.1.1", 950, "JP")
        ranked = [revalidated_previous, *current]
        previous = [node("1.1.1.1", 999, "JP"), node("1.0.0.1", 998, "JP")]
        options = {
            "top_nodes": 2,
            "minimum_per_country": {},
            "max_per_country": 80,
            "max_per_ipv4_24": 4,
            "max_per_ipv6_48": 4,
        }

        final, stats = merge_with_previous(current, ranked, previous, options)

        self.assertEqual([item.ip for item in final], ["1.1.1.1", "104.16.1.1"])
        self.assertEqual(stats["previous_loaded"], 2)
        self.assertEqual(stats["previous_reverified"], 1)
        self.assertEqual(stats["previous_in_final"], 1)


if __name__ == "__main__":
    unittest.main()
