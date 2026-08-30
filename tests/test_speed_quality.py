from __future__ import annotations

import unittest

from core.models import NodeResult
from core.speed_test import _accepted_speed_mbps, _select_speed_targets


def node(ip: str, *, latency: float) -> NodeResult:
    value = NodeResult(ip=ip)
    value.http_latency_ms = latency
    value.tcp_latency_ms = latency
    value.average_latency_ms = latency
    return value


class SpeedQualityTests(unittest.TestCase):
    def test_speed_targets_follow_average_latency(self) -> None:
        records = [node(f"104.16.{index}.1", latency=20 - index) for index in range(20)]

        targets = _select_speed_targets(
            records,
            20,
            {},
        )

        self.assertEqual(len(targets), 20)
        self.assertEqual(targets[0].average_latency_ms, 1)

    def test_partial_download_does_not_receive_a_speed_score(self) -> None:
        self.assertIsNone(_accepted_speed_mbps(900, 1000, 1.0, 0.95))
        self.assertAlmostEqual(_accepted_speed_mbps(1000, 1000, 1.0, 0.95), 0.008)

if __name__ == "__main__":
    unittest.main()
