from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from core.http_check import check_http
from core.models import NodeResult
from core.tcp_scan import scan_tcp


class PipelineStageTests(unittest.IsolatedAsyncioTestCase):
    async def test_tcp_stage_uses_fields_supported_by_node_model(self) -> None:
        record = NodeResult(ip="192.0.2.1")
        with patch("core.tcp_scan._probe_once", new=AsyncMock(return_value=12.5)):
            result = await scan_tcp(
                [record],
                {"timeout_seconds": 1, "attempts": 1, "concurrency": 1},
            )

        self.assertEqual(result, [record])
        self.assertTrue(record.tcp_ok)
        self.assertEqual(record.tcp_latency_ms, 12.5)

    async def test_http_stage_uses_fields_supported_by_node_model(self) -> None:
        record = NodeResult(ip="192.0.2.1", tcp_ok=True)
        response = (200, {"server": "cloudflare", "cf-ray": "test-NRT"}, b"colo=NRT\nloc=JP\n", 25.0)
        with patch("core.http_check._request", new=AsyncMock(return_value=response)):
            result = await check_http(
                [record],
                "worker.example.com",
                {"timeout_seconds": 1, "path": "/cdn-cgi/trace", "accepted_statuses": [200], "concurrency": 1},
            )

        self.assertEqual(result, [record])
        self.assertTrue(record.http_ok)
        self.assertEqual(record.colo, "NRT")
        self.assertEqual(record.country, "JP")


if __name__ == "__main__":
    unittest.main()
