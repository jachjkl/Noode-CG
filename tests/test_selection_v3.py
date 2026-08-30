from __future__ import annotations

import unittest

from core.models import NodeResult
from core.selection_v3 import filter_by_average_latency, rank_final, select_latency_shortlist


def measured(
    ip: str,
    *,
    tcp: float,
    tls: float,
    http: float,
    loss: float = 0.0,
    speed: float | None = 10.0,
) -> NodeResult:
    node = NodeResult(ip=ip, tcp_ok=True, tls_ok=True, http_ok=True)
    node.tcp_latency_ms = tcp
    node.tls_latency_ms = tls
    node.http_latency_ms = http
    node.tcp_loss_rate = loss
    node.speed_mbps = speed
    return node


class SelectionV3Tests(unittest.TestCase):
    def test_average_latency_limit_is_inclusive(self) -> None:
        accepted = measured("1.1.1.1", tcp=200, tls=300, http=400)
        rejected = measured("8.8.8.8", tcp=300, tls=300, http=300.3)

        result = filter_by_average_latency([rejected, accepted], maximum_ms=300)

        self.assertEqual(result, [accepted])
        self.assertEqual(accepted.average_latency_ms, 300.0)
        self.assertGreater(rejected.average_latency_ms or 0, 300)

    def test_shortlist_keeps_best_3000_by_average_latency(self) -> None:
        records = [
            measured(f"104.16.{index // 250}.{index % 250 + 1}", tcp=1, tls=1, http=index / 10)
            for index in range(3100)
        ]

        result = select_latency_shortlist(records, count=3000)

        self.assertEqual(len(result), 3000)
        self.assertLessEqual(result[-1].average_latency_ms or 999, records[-1].average_latency_ms or 0)

    def test_final_order_is_loss_then_speed_then_latency(self) -> None:
        slow = measured("1.1.1.1", tcp=30, tls=30, http=30, loss=0.0, speed=10)
        fast = measured("8.8.8.8", tcp=50, tls=50, http=50, loss=0.0, speed=100)
        lossy = measured("9.9.9.9", tcp=10, tls=10, http=10, loss=0.1, speed=1000)

        result = rank_final([lossy, slow, fast], count=500)

        self.assertEqual(result, [fast, slow, lossy])


if __name__ == "__main__":
    unittest.main()
