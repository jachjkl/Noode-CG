from __future__ import annotations

import unittest

from core.colo_detect import enrich_locations
from core.models import NodeResult


class ColoLocationTests(unittest.TestCase):
    def test_colo_country_overrides_runner_trace_country(self) -> None:
        node = NodeResult(ip="192.0.2.1", colo="NRT", country="US")
        locations = {"NRT": {"cca2": "JP", "region": "Asia Pacific", "city": "Tokyo"}}

        enrich_locations([node], locations)

        self.assertEqual(node.country, "JP")
        self.assertEqual(node.city, "Tokyo")


if __name__ == "__main__":
    unittest.main()
