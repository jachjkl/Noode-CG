from __future__ import annotations

import unittest

from core.models import NodeResult
from core.selection import reserve_candidates


class SelectionTests(unittest.TestCase):
    def test_precision_batch_reserves_asia_and_limits_generated_anycast(self) -> None:
        official = [NodeResult(ip=f"104.16.1.{index}", colo="LAX", sources=["cloudflare-official-ipv4"]) for index in range(1, 21)]
        nrt = [NodeResult(ip=f"198.51.100.{index}", colo="NRT", sources=["required-source"]) for index in range(1, 6)]
        icn = [NodeResult(ip=f"203.0.113.{index}", colo="ICN", sources=["required-source"]) for index in range(1, 6)]

        selected = reserve_candidates(
            official + nrt + icn,
            12,
            {"minimum_per_colo": {"NRT": 3, "ICN": 3}, "max_official_generated": 2},
        )

        self.assertGreaterEqual(sum(node.colo == "NRT" for node in selected), 3)
        self.assertGreaterEqual(sum(node.colo == "ICN" for node in selected), 3)
        self.assertLessEqual(sum(node.official_only for node in selected), 2)

    def test_precision_batch_does_not_concentrate_one_colo(self) -> None:
        lax = [
            NodeResult(ip=f"104.16.2.{index}", colo="LAX", sources=["required-source"])
            for index in range(1, 11)
        ]
        nrt = [
            NodeResult(ip=f"198.51.100.{index}", colo="NRT", sources=["required-source"])
            for index in range(1, 6)
        ]
        selected = reserve_candidates(lax + nrt, 8, {"max_per_colo": 4})
        self.assertEqual(sum(node.colo == "LAX" for node in selected), 4)
        self.assertEqual(sum(node.colo == "NRT" for node in selected), 4)


if __name__ == "__main__":
    unittest.main()
