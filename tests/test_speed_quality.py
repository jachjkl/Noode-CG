from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from core.models import NodeResult
from core.speed_test import test_speed


class SpeedQualityTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_download_is_not_qualified(self) -> None:
        complete = NodeResult(ip="104.16.1.1", http_latency_ms=10)
        partial = NodeResult(ip="104.16.1.2", http_latency_ms=20)
        wanted = 1024 * 1024
        with patch(
            "core.speed_test._probe_speed",
            new=AsyncMock(side_effect=[(wanted, 1.0), (wanted // 2, 1.0)]),
        ):
            records = await test_speed(
                [complete, partial],
                {
                    "enabled": True,
                    "domain": "speed.cloudflare.com",
                    "path": f"/__down?bytes={wanted}",
                    "bytes_per_test": wanted,
                    "candidates": 2,
                    "batch_size": 2,
                    "target_qualified": 2,
                    "concurrency": 1,
                    "timeout_seconds": 5,
                    "minimum_mbps": 1,
                    "minimum_completion_ratio": 0.95,
                },
                user_agent="test",
            )

        self.assertEqual(records, [complete, partial])
        self.assertTrue(complete.speed_ok)
        self.assertEqual(complete.speed_completion_rate, 1.0)
        self.assertFalse(partial.speed_ok)
        self.assertEqual(partial.speed_completion_rate, 0.5)


if __name__ == "__main__":
    unittest.main()
