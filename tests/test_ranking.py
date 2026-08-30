from __future__ import annotations

import unittest

from core.models import NodeResult
from core.ranking import calculate_average_latency, rank_final


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


class RankingTests(unittest.TestCase):
    def test_three_stage_average_is_calculated(self) -> None:
        node = measured("1.1.1.1", tcp=100, tls=200, http=300)
        self.assertEqual(calculate_average_latency(node), 200.0)

    def test_final_order_is_loss_then_speed_then_latency(self) -> None:
        slow = measured("1.1.1.1", tcp=30, tls=30, http=30, loss=0.0, speed=10)
        fast = measured("8.8.8.8", tcp=50, tls=50, http=50, loss=0.0, speed=100)
        lossy = measured("9.9.9.9", tcp=10, tls=10, http=10, loss=0.1, speed=1000)

        result = rank_final([lossy, slow, fast], count=500)

        self.assertEqual(result, [fast, slow, lossy])

    def test_final_count_uses_unique_ips_even_when_ports_differ(self) -> None:
        first = measured("1.1.1.1", tcp=10, tls=10, http=10, speed=100)
        duplicate = measured("1.1.1.1", tcp=20, tls=20, http=20, speed=90)
        duplicate.port = 8443
        other = measured("8.8.8.8", tcp=30, tls=30, http=30, speed=80)

        result = rank_final([duplicate, other, first], count=3)

        self.assertEqual(result, [first, other])


if __name__ == "__main__":
    unittest.main()
