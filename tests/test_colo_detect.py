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
        self.assertEqual(node.colo_country, "JP")
        self.assertEqual(node.city, "Tokyo")

    def test_source_country_is_preserved_separately_from_colo_country(self) -> None:
        node = NodeResult(ip="192.0.2.2", country_hint="JP", colo="LAX")
        locations = {"LAX": {"cca2": "US", "region": "North America", "city": "Los Angeles"}}

        enrich_locations([node], locations)

        self.assertEqual(node.country, "JP")
        self.assertEqual(node.colo_country, "US")


if __name__ == "__main__":
    unittest.main()
