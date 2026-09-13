import asyncio
import gzip
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from core.models import NodeResult
from core.tls_check import _probe_once
from core.http_check import _request
from core.speed_test import test_speed
from core.handoff import _write_handoff


class ProbeCleanupTests(unittest.IsolatedAsyncioTestCase):
    def test_fast_preview_preserves_all_measurements(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preview.gz"
            nodes = [NodeResult(ip="192.0.2.1", tcp_latency_ms=123.456, speed_mbps=3.789)]
            _write_handoff(path, nodes, {"status": "running"})
            normal = json.loads(gzip.decompress(path.read_bytes()))
            _write_handoff(path, nodes, {"status": "running"}, compression_level=1)
            fast = json.loads(gzip.decompress(path.read_bytes()))
            self.assertEqual(normal["nodes"], fast["nodes"])
            self.assertEqual(normal["report"], fast["report"])

    async def test_completed_probes_do_not_wait_for_slow_peer_shutdown(self):
        # All measurement bytes have arrived; only TLS close_notify is delayed.
        for mode in ("tls", "http", "speed"):
            with self.subTest(mode=mode):
                reader = AsyncMock()
                reader.readexactly.return_value = b"H"
                reader.readuntil.return_value = (
                    b"TTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\n"
                    if mode == "http" else b"HTTP/1.1 200 OK\r\n\r\n"
                )
                reader.read.return_value = b"test"
                writer = MagicMock()
                writer.drain = AsyncMock()
                writer.wait_closed = AsyncMock(side_effect=lambda: None)

                async def slow_close():
                    await asyncio.sleep(0.35)

                writer.wait_closed.side_effect = slow_close
                writer.get_extra_info.return_value.version.return_value = "TLSv1.3"
                writer.get_extra_info.return_value.cipher.return_value = ("AES",)
                node = NodeResult(ip="192.0.2.1")
                started = time.perf_counter()
                with patch("asyncio.open_connection", AsyncMock(return_value=(reader, writer))):
                    if mode == "tls":
                        await _probe_once(node, "example.com", MagicMock(), 1)
                    elif mode == "http":
                        result = await _request(node, domain="example.com", path="/", context=MagicMock(), timeout=1, user_agent="test")
                        self.assertEqual(result[2], b"test")
                    else:
                        await test_speed([node], {"bytes_per_test": 4}, user_agent="test")
                        self.assertIsNotNone(node.speed_mbps)
                elapsed = time.perf_counter() - started
                print(f"cleanup {mode}: {elapsed:.3f}s")
                self.assertLess(elapsed, 0.25, "completed measurement blocked by peer shutdown")
                writer.transport.abort.assert_called_once()
