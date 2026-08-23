from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from core.models import NodeResult
from core.tcp_scan import scan_tcp


class TcpStabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_probe_records_real_loss_and_p95(self) -> None:
        record = NodeResult(ip="104.16.1.1")
        measurements = [10.0, 12.0, TimeoutError("timeout"), 18.0, 30.0]
        with patch("core.tcp_scan._probe_once", new=AsyncMock(side_effect=measurements)):
            passed = await scan_tcp(
                [record],
                {
                    "concurrency": 1,
                    "timeout_seconds": 1,
                    "attempts": 5,
                    "minimum_success_ratio": 0.8,
                    "maximum_p95_ms": 100,
                },
            )

        self.assertEqual(passed, [record])
        self.assertEqual(record.tcp_loss_rate, 0.2)
        self.assertEqual(record.tcp_latency_ms, 15.0)
        self.assertEqual(record.tcp_p95_latency_ms, 30.0)
        self.assertGreater(record.tcp_jitter_ms or 0, 0)

    async def test_unstable_node_is_rejected(self) -> None:
        record = NodeResult(ip="104.16.1.2")
        measurements = [10.0, TimeoutError("timeout"), 20.0, TimeoutError("timeout"), 30.0]
        with patch("core.tcp_scan._probe_once", new=AsyncMock(side_effect=measurements)):
            passed = await scan_tcp(
                [record],
                {
                    "concurrency": 1,
                    "timeout_seconds": 1,
                    "attempts": 5,
                    "minimum_success_ratio": 0.8,
                    "maximum_p95_ms": 100,
                },
            )

        self.assertEqual(passed, [])
        self.assertFalse(record.tcp_ok)
        self.assertEqual(record.tcp_loss_rate, 0.4)


if __name__ == "__main__":
    unittest.main()
