from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from core.http_check import check_http
from core.models import NodeResult


def valid_response(latency: float) -> tuple[int, dict[str, str], bytes, float]:
    return 200, {"server": "cloudflare", "cf-ray": "test-NRT"}, b"colo=NRT\nloc=JP\n", latency


class HttpStabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_all_repeated_http_attempts(self) -> None:
        record = NodeResult(ip="104.16.1.1")
        with patch(
            "core.http_check._request",
            new=AsyncMock(side_effect=[valid_response(20), TimeoutError("reset"), valid_response(30)]),
        ):
            passed = await check_http(
                [record],
                "example.com",
                {
                    "concurrency": 1,
                    "timeout_seconds": 1,
                    "attempts": 3,
                    "minimum_success_ratio": 1.0,
                    "maximum_p95_ms": 100,
                    "path": "/cdn-cgi/trace",
                    "require_trace_fields": True,
                    "accepted_statuses": [200],
                },
            )

        self.assertEqual(passed, [])
        self.assertEqual(record.http_success_rate, 0.6667)
        self.assertEqual(record.http_p95_latency_ms, 30)


if __name__ == "__main__":
    unittest.main()
