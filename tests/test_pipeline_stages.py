from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from core.http_check import _request, check_http
from core.models import NodeResult
from core.tcp_scan import scan_tcp
from core.tls_check import check_tls


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

    async def test_tcp_accepts_one_slow_attempt_when_three_attempt_average_is_under_300ms(self) -> None:
        record = NodeResult(ip="192.0.2.2")
        probe = AsyncMock(side_effect=[80.0, 500.0, 90.0])
        with patch("core.tcp_scan._probe_once", new=probe):
            result = await scan_tcp(
                [record],
                {
                    "timeout_seconds": 1,
                    "attempts": 3,
                    "concurrency": 1,
                    "require_all_attempts": True,
                    "maximum_average_latency_ms": 300,
                },
            )

        self.assertEqual(result, [record])
        self.assertTrue(record.tcp_ok)
        self.assertEqual(record.tcp_latency_ms, 223.333)

    async def test_tcp_accepts_three_attempt_average_equal_to_300ms(self) -> None:
        record = NodeResult(ip="192.0.2.3")
        probe = AsyncMock(side_effect=[200.0, 300.0, 400.0])
        with patch("core.tcp_scan._probe_once", new=probe):
            result = await scan_tcp(
                [record],
                {
                    "timeout_seconds": 1,
                    "attempts": 3,
                    "concurrency": 1,
                    "require_all_attempts": True,
                    "maximum_average_latency_ms": 300,
                },
            )

        self.assertEqual(result, [record])
        self.assertEqual(record.tcp_loss_rate, 0.0)

    async def test_tcp_rejects_three_attempt_average_over_300ms(self) -> None:
        record = NodeResult(ip="192.0.2.8")
        probe = AsyncMock(side_effect=[300.0, 300.0, 300.1])
        with patch("core.tcp_scan._probe_once", new=probe):
            result = await scan_tcp(
                [record],
                {
                    "timeout_seconds": 1,
                    "attempts": 3,
                    "concurrency": 1,
                    "require_all_attempts": True,
                    "maximum_average_latency_ms": 300,
                },
            )

        self.assertEqual(result, [])
        self.assertFalse(record.tcp_ok)

    async def test_tcp_stops_after_first_failed_required_attempt(self) -> None:
        record = NodeResult(ip="192.0.2.11")
        probe = AsyncMock(side_effect=TimeoutError("timeout"))
        with patch("core.tcp_scan._probe_once", new=probe):
            result = await scan_tcp(
                [record],
                {
                    "timeout_seconds": 1,
                    "attempts": 3,
                    "concurrency": 1,
                    "require_all_attempts": True,
                    "maximum_average_latency_ms": 300,
                    "stop_on_failure": True,
                },
            )

        self.assertEqual(result, [])
        self.assertEqual(probe.await_count, 1)

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

    async def test_target_http_requires_all_three_attempts(self) -> None:
        record = NodeResult(ip="192.0.2.4", tcp_ok=True)
        good = (200, {"server": "cloudflare", "cf-ray": "test-NRT"}, b"colo=NRT\nloc=JP\n", 25.0)
        request = AsyncMock(side_effect=[good, TimeoutError("timeout"), good])
        with patch("core.http_check._request", new=request):
            result = await check_http(
                [record],
                "worker.example.com",
                {
                    "timeout_seconds": 1,
                    "path": "/cdn-cgi/trace",
                    "accepted_statuses": [200],
                    "concurrency": 1,
                    "attempts": 3,
                    "require_all_attempts": True,
                },
            )

        self.assertEqual(result, [])
        self.assertFalse(record.http_ok)

    async def test_tls_uses_three_attempt_average_threshold(self) -> None:
        record = NodeResult(ip="192.0.2.5", tcp_ok=True)
        probe = AsyncMock(
            side_effect=[
                (300.0, "TLSv1.3", "TLS_AES_128_GCM_SHA256"),
                (300.0, "TLSv1.3", "TLS_AES_128_GCM_SHA256"),
                (300.1, "TLSv1.3", "TLS_AES_128_GCM_SHA256"),
            ]
        )
        with patch("core.tls_check._probe_once", new=probe):
            result = await check_tls(
                [record],
                "worker.example.com",
                {
                    "timeout_seconds": 1,
                    "concurrency": 1,
                    "attempts": 3,
                    "require_all_attempts": True,
                    "maximum_average_latency_ms": 300,
                },
            )

        self.assertEqual(result, [])
        self.assertFalse(record.tls_ok)

    async def test_tls_accepts_one_slow_attempt_when_average_is_under_300ms(self) -> None:
        record = NodeResult(ip="192.0.2.9", tcp_ok=True)
        probe = AsyncMock(
            side_effect=[
                (100.0, "TLSv1.3", "TLS_AES_128_GCM_SHA256"),
                (500.0, "TLSv1.3", "TLS_AES_128_GCM_SHA256"),
                (100.0, "TLSv1.3", "TLS_AES_128_GCM_SHA256"),
            ]
        )
        with patch("core.tls_check._probe_once", new=probe):
            result = await check_tls(
                [record],
                "worker.example.com",
                {
                    "timeout_seconds": 1,
                    "concurrency": 1,
                    "attempts": 3,
                    "require_all_attempts": True,
                    "maximum_average_latency_ms": 300,
                },
            )

        self.assertEqual(result, [record])
        self.assertEqual(record.tls_latency_ms, 233.333)

    async def test_tls_stops_after_first_failed_required_attempt(self) -> None:
        record = NodeResult(ip="192.0.2.12", tcp_ok=True)
        probe = AsyncMock(side_effect=TimeoutError("timeout"))
        with patch("core.tls_check._probe_once", new=probe):
            result = await check_tls(
                [record],
                "worker.example.com",
                {
                    "timeout_seconds": 1,
                    "concurrency": 1,
                    "attempts": 3,
                    "require_all_attempts": True,
                    "maximum_average_latency_ms": 300,
                    "stop_on_failure": True,
                },
            )

        self.assertEqual(result, [])
        self.assertEqual(probe.await_count, 1)

    async def test_https_ttfb_uses_three_attempt_average_threshold(self) -> None:
        record = NodeResult(ip="192.0.2.6", tcp_ok=True, tls_ok=True)
        def response(latency: float) -> tuple[int, dict[str, str], bytes, float]:
            return (
                200,
                {"server": "cloudflare", "cf-ray": "test-NRT"},
                b"colo=NRT\nloc=JP\n",
                latency,
            )
        request = AsyncMock(side_effect=[response(300.0), response(300.0), response(300.1)])
        with patch("core.http_check._request", new=request):
            result = await check_http(
                [record],
                "worker.example.com",
                {
                    "timeout_seconds": 1,
                    "path": "/cdn-cgi/trace",
                    "accepted_statuses": [200],
                    "concurrency": 1,
                    "attempts": 3,
                    "require_all_attempts": True,
                    "maximum_average_ttfb_ms": 300,
                },
            )

        self.assertEqual(result, [])
        self.assertFalse(record.http_ok)
        self.assertEqual(record.probe_results["https_ttfb"]["successes"], 3)

    async def test_https_ttfb_accepts_one_slow_attempt_when_average_is_under_300ms(self) -> None:
        record = NodeResult(ip="192.0.2.10", tcp_ok=True, tls_ok=True)

        def response(latency: float) -> tuple[int, dict[str, str], bytes, float]:
            return (
                200,
                {"server": "cloudflare", "cf-ray": "test-NRT"},
                b"colo=NRT\nloc=JP\n",
                latency,
            )

        request = AsyncMock(side_effect=[response(100.0), response(500.0), response(100.0)])
        with patch("core.http_check._request", new=request):
            result = await check_http(
                [record],
                "worker.example.com",
                {
                    "timeout_seconds": 1,
                    "path": "/cdn-cgi/trace",
                    "accepted_statuses": [200],
                    "concurrency": 1,
                    "attempts": 3,
                    "require_all_attempts": True,
                    "maximum_average_ttfb_ms": 300,
                },
            )

        self.assertEqual(result, [record])
        self.assertEqual(record.http_latency_ms, 233.333)

    async def test_https_stops_after_first_failed_required_attempt(self) -> None:
        record = NodeResult(ip="192.0.2.13", tcp_ok=True, tls_ok=True)
        request = AsyncMock(side_effect=TimeoutError("timeout"))
        with patch("core.http_check._request", new=request):
            result = await check_http(
                [record],
                "worker.example.com",
                {
                    "timeout_seconds": 1,
                    "path": "/cdn-cgi/trace",
                    "accepted_statuses": [200],
                    "concurrency": 1,
                    "attempts": 3,
                    "require_all_attempts": True,
                    "maximum_average_ttfb_ms": 300,
                    "stop_on_failure": True,
                },
            )

        self.assertEqual(result, [])
        self.assertEqual(request.await_count, 1)

    async def test_https_ttfb_stops_at_first_response_byte(self) -> None:
        record = NodeResult(ip="192.0.2.7", tcp_ok=True, tls_ok=True)
        reader = MagicMock()
        reader.readexactly = AsyncMock(return_value=b"H")
        reader.readuntil = AsyncMock(
            return_value=b"TTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
        )
        writer = MagicMock()
        writer.drain = AsyncMock()
        writer.wait_closed = AsyncMock()
        with (
            patch("core.http_check.asyncio.open_connection", new=AsyncMock(return_value=(reader, writer))),
            patch("core.http_check.time.perf_counter", side_effect=[10.0, 10.025]),
        ):
            _, _, _, ttfb = await _request(
                record,
                domain="worker.example.com",
                path="/cdn-cgi/trace",
                context=MagicMock(),
                timeout=1,
                user_agent="test",
            )

        self.assertAlmostEqual(ttfb, 25.0)
        reader.readexactly.assert_awaited_once_with(1)


if __name__ == "__main__":
    unittest.main()
