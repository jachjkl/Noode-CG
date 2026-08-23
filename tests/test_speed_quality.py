from __future__ import annotations

import unittest

from core.models import NodeResult
from core.speed_test import _accepted_speed_mbps, _select_speed_targets


def node(ip: str, *, country: str, latency: float) -> NodeResult:
    value = NodeResult(ip=ip, country=country)
    value.http_latency_ms = latency
    value.tcp_latency_ms = latency
    return value


class SpeedQualityTests(unittest.TestCase):
    def test_speed_targets_reserve_japanese_candidates(self) -> None:
        records = [
            node(f"104.16.{index // 250}.{index % 250 + 1}", country="US", latency=index + 1)
            for index in range(20)
        ]
        records.extend(
            node(f"172.64.{index}.1", country="JP", latency=200 + index)
            for index in range(8)
        )

        targets = _select_speed_targets(
            records,
            20,
            {"minimum_per_country": {"JP": 6}},
        )

        self.assertEqual(len(targets), 20)
        self.assertGreaterEqual(sum(item.country == "JP" for item in targets), 6)

    def test_partial_download_does_not_receive_a_speed_score(self) -> None:
        self.assertIsNone(_accepted_speed_mbps(900, 1000, 1.0, 0.95))
        self.assertAlmostEqual(_accepted_speed_mbps(1000, 1000, 1.0, 0.95), 0.008)


if __name__ == "__main__":
    unittest.main()
