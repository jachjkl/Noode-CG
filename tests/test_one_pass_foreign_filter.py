from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from core.models import NodeResult
from core.pipeline import _three_metric_checks


class OnePassForeignFilterTests(unittest.TestCase):
    def test_combined_average_and_colo_country_are_both_required(self) -> None:
        records = [
            NodeResult(ip="192.0.2.1", tcp_ok=True, tcp_latency_ms=100.0),
            NodeResult(ip="192.0.2.2", tcp_ok=True, tcp_latency_ms=100.0),
            NodeResult(ip="192.0.2.3", tcp_ok=True, tcp_latency_ms=100.0),
        ]

        async def tls_once(nodes, _domain, _options):
            for node in nodes:
                node.tls_ok = True
                node.tls_latency_ms = 100.0
            return list(nodes)

        async def http_once(nodes, _domain, _http, _websocket, *, user_agent):
            self.assertEqual(user_agent, "test-agent")
            for node in nodes:
                node.http_ok = True
                node.http_status = 200
                if node.ip == "192.0.2.1":
                    node.http_latency_ms = 700.0
                    node.colo = "NRT"
                elif node.ip == "192.0.2.2":
                    node.http_latency_ms = 701.0
                    node.colo = "NRT"
                else:
                    node.http_latency_ms = 100.0
                    node.colo = "CAN"
            return list(nodes)

        tls_mock = AsyncMock(side_effect=tls_once)
        http_mock = AsyncMock(side_effect=http_once)
        pipeline = {
            "tls": {"attempts": 1},
            "http": {"attempts": 1},
            "websocket": {},
            "maximum_combined_latency_ms": 300,
            "location_filter": {
                "require_known_colo_country": True,
                "excluded_countries": ["CN"],
            },
        }
        locations = {
            "NRT": {"cca2": "JP", "region": "Asia Pacific", "city": "Tokyo"},
            "CAN": {"cca2": "CN", "region": "Asia Pacific", "city": "Guangzhou"},
        }

        with (
            patch("core.pipeline.check_tls", new=tls_mock),
            patch("core.pipeline.check_http", new=http_mock),
        ):
            qualified, counts = _three_metric_checks(
                records,
                domain="worker.example.com",
                pipeline=pipeline,
                user_agent="test-agent",
                locations=locations,
            )

        self.assertEqual([node.ip for node in qualified], ["192.0.2.1"])
        self.assertEqual(qualified[0].average_latency_ms, 300.0)
        self.assertEqual(qualified[0].country, "JP")
        self.assertEqual(counts["foreign_combined_latency_qualified"], 1)
        self.assertEqual(tls_mock.await_count, 1)
        self.assertEqual(http_mock.await_count, 1)


if __name__ == "__main__":
    unittest.main()
