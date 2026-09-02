from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from core.models import NodeResult
from core.pipeline import _speed_checks
from core.speed_test import _accepted_speed_mbps, _select_speed_targets, meets_minimum_speed


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

    def test_minimum_one_mbps_is_enforced(self) -> None:
        below = node("1.1.1.1", latency=10)
        below.speed_mbps = 0.999
        exact = node("8.8.8.8", latency=10)
        exact.speed_mbps = 1.0

        self.assertFalse(meets_minimum_speed(below, minimum_mbps=1.0))
        self.assertTrue(meets_minimum_speed(exact, minimum_mbps=1.0))

    def test_speed_checks_reports_each_qualified_result_immediately(self) -> None:
        fast = node("1.1.1.1", latency=10)
        slow = node("8.8.8.8", latency=10)
        observed: list[str] = []

        async def speed(records, _options, *, on_result, **_kwargs):
            records[0].speed_mbps = 5.0
            on_result(records[0])
            records[1].speed_mbps = 2.0
            on_result(records[1])
            return records

        with patch(
            "core.pipeline.test_speed",
            new=AsyncMock(side_effect=speed),
        ):
            qualified, _counts = _speed_checks(
                [fast, slow],
                pipeline={"speed": {"minimum_mbps": 3.0}},
                user_agent="test",
                probe_paths=[],
                on_qualified=lambda record: observed.append(record.ip),
            )

        self.assertEqual(observed, [fast.ip])
        self.assertEqual([record.ip for record in qualified], [fast.ip])

if __name__ == "__main__":
    unittest.main()
